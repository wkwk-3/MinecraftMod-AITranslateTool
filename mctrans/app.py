"""MC MOD Translator - Web ダッシュボード (FastAPI)。Colab / ローカル両対応。"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import traceback
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import ftbquests as ftbq, jobs, llm, modscan, resources as res, translator as tr

# 作業フォルダ: MOD・翻訳結果・途中経過・実行履歴を保存 (Colab では Google ドライブ上を指定)
WORK = Path(os.environ.get("MCT_WORKDIR", "/content/mct_work" if Path("/content").exists() else "./mct_work")).resolve()
# モデルは大きいのでドライブではなくローカルディスクにキャッシュ
CACHE_DIR = Path(os.environ.get("MCT_CACHEDIR", "/content/hf_cache" if Path("/content").exists() else WORK / "hf_cache"))
MODS_DIR, OUT_DIR = WORK / "mods", WORK / "output"
for d in (MODS_DIR, OUT_DIR, CACHE_DIR):
    d.mkdir(parents=True, exist_ok=True)
JOBS = jobs.JobStore(WORK)

TOKEN = os.environ.get("MCT_TOKEN") or secrets.token_urlsafe(24)
STATIC = Path(__file__).parent / "static"

# API ドキュメント (/docs 等) は使わないので無効にして公開面を減らす
app = FastAPI(title="MC MOD Translator", docs_url=None, redoc_url=None, openapi_url=None)

SECURITY_HEADERS = {
    "X-Frame-Options": "DENY",                 # 他サイトへの埋め込み (クリックジャッキング) を防ぐ
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",          # URL (トークン) を外部サイトに送らない
    "Cache-Control": "no-store",
}


class State:
    def __init__(self):
        self.mods: dict[str, modscan.ModInfo] = {}
        self.servers = {"translator": llm.LlamaServer("translator", 8081),
                        "helper": llm.LlamaServer("helper", 8082)}
        self.model_cfg = {
            "translator": {"repo": llm.DEFAULT_TRANSLATE_REPO, "file": "", "ctx": 16384, "n_cpu_moe": -1, "url": ""},
            "helper": {"repo": "", "file": "", "ctx": 8192, "n_cpu_moe": 0, "url": ""},   # 既定は使わない
        }
        for cfg in self.model_cfg.values():
            cfg["path"] = ""          # URL から取得したモデルのローカルパス
        saved = jobs.read_json(WORK / "settings.json", {}) or {}
        for role, cfg in (saved.get("models") or {}).items():
            if role in self.model_cfg:
                self.model_cfg[role].update(cfg)
        # URL から追加したモデル候補 (role -> [{label, repo, file, path, src}])
        self.custom_presets: dict[str, list] = {"translator": [], "helper": [], **(saved.get("custom_presets") or {})}
        self.downloads: dict[str, dict] = {}
        # 翻訳プロンプトの設定: 固有名詞の方針・ユーザーの追加指示
        self.prompt_cfg = {"proper_nouns": "katakana", "custom": "", **(saved.get("prompt") or {})}
        # 翻訳のまとめ方: per_mod = Mod (言語ファイル) ごと / mixed = Mod に関係なく lines 行ずつ
        self.batch_cfg = {"mode": "per_mod", "lines": 40, **(saved.get("batch") or {})}
        self.memory = tr.Memory(WORK / "memory.json")
        self.logs: list[str] = []
        self.job = {"running": False, "kind": "", "phase": "待機中", "done": 0, "total": 0, "current": "",
                    "result": None, "error": None, "started": 0}
        self.cancel = threading.Event()
        self.lock = threading.Lock()

    def log(self, msg: str):
        line = time.strftime("%H:%M:%S ") + msg
        print(line, flush=True)
        self.logs.append(line)
        del self.logs[:-500]


S = State()
for _jid in JOBS.mark_interrupted_on_startup():
    S.log(f"中断されたジョブを検出: {_jid} (ダッシュボードから再開できます)")


# ------------------------------------------------------------------ auth
@app.middleware("http")
async def auth(request: Request, call_next):
    """全リクエストにアクセストークンを要求 (ngrok 等で公開しても第三者が操作できない)。"""
    resp = await _auth(request, call_next)
    for k, v in SECURITY_HEADERS.items():
        resp.headers.setdefault(k, v)
    return resp


async def _auth(request: Request, call_next):
    # トークンは URL (?token=)・ヘッダー (x-mct-token)・Cookie のどれでも受け付ける。
    # Colab の画面などのリンクから開くと、ブラウザの設定によっては Cookie が送られないため、
    # 画面側はトークンを保持してヘッダーで送る (Cookie は補助)。
    q = request.query_params.get("token") or ""
    supplied = q or request.headers.get("x-mct-token") or request.cookies.get("mct_token") or ""
    if not hmac.compare_digest(supplied, TOKEN):
        if request.url.path.startswith("/api"):
            return JSONResponse({"detail": "unauthorized"}, 401)
        return HTMLResponse("<h3>アクセストークンが必要です</h3><p>Colab またはターミナルに表示された URL から開いてください。</p>", 401)
    resp = await call_next(request)
    if q and not request.url.path.startswith("/api"):
        # ngrok / cloudflared は HTTPS をここで HTTP に変換するので、転送元のプロトコルも見る
        https = request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"
        resp.set_cookie("mct_token", TOKEN, httponly=True, samesite="lax", secure=https)
    return resp


@app.get("/")
def index():
    # 更新後に古い画面が残らないようキャッシュさせない
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ mods
def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-+() ]", "_", Path(name).name)[:180]


def _rescan():
    for p in MODS_DIR.glob("*.jar"):
        if p.name not in S.mods:
            try:
                S.mods[p.name] = modscan.scan_jar(p, p.name)
            except Exception as e:
                S.log(f"解析失敗: {p.name}: {e}")


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)):
    added = []
    for f in files:
        name = _safe_name(f.filename or "mod.jar")
        if not name.lower().endswith((".jar", ".zip")):
            continue
        if name.lower().endswith(".zip"):
            name = name[:-4] + ".jar"
        dest = MODS_DIR / name
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        try:
            S.mods[name] = modscan.scan_jar(dest, name)
            added.append(name)
        except Exception as e:
            dest.unlink(missing_ok=True)
            S.log(f"不正な MOD ファイル: {name}: {e}")
    S.log(f"{len(added)} 件アップロード")
    return {"added": added}


@app.get("/api/mods")
def list_mods(q: str = ""):
    _rescan()
    terms = [t for t in q.lower().split() if t]
    out = []
    for m in S.mods.values():
        hay = " ".join([m.name, m.filename, m.loader, m.version, m.description, *m.mod_ids, *m.lang_files]).lower()
        if all(t in hay for t in terms):
            out.append(m.to_dict())
    return sorted(out, key=lambda x: x["name"].lower())


class UidList(BaseModel):
    uids: list[str]


@app.post("/api/mods/delete")
def delete_mods(body: UidList):
    for uid in body.uids:
        if uid in S.mods:
            (MODS_DIR / uid).unlink(missing_ok=True)
            del S.mods[uid]
    return {"ok": True}


@app.post("/api/analyze")
def analyze(body: UidList):
    order, missing = modscan.resolve_dependencies(body.uids, S.mods)
    return {"order": order, "added_deps": [u for u in order if u not in body.uids], "missing": missing}


# ------------------------------------------------------------------ models
@app.get("/api/models/presets")
def presets():
    merged = {r: S.custom_presets.get(r, []) + [{**p, "file": "", "path": ""} for p in lst]
              for r, lst in llm.PRESETS.items()}
    return {"presets": merged, "config": S.model_cfg}


def _save_settings():
    jobs.atomic_write_json(WORK / "settings.json", {"models": S.model_cfg, "custom_presets": S.custom_presets,
                                                    "prompt": S.prompt_cfg, "batch": S.batch_cfg})


class ModelUrlReq(BaseModel):
    url: str
    role: str = "translator"


@app.post("/api/models/from_url")
def model_from_url(req: ModelUrlReq):
    """GGUF の URL を受け取り、ダウンロード → モデル候補に追加 → 自動で選択する。"""
    if req.role not in S.model_cfg:
        raise HTTPException(400, "role")
    try:
        info = llm.parse_model_url(req.url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if req.role == "translator" and not tr.is_insttrans_model(info.get("repo", ""), info["file"]):
        raise HTTPException(400, "翻訳モデルは Index-Translate だけを使えます")
    label = f"{info['file'].rsplit('/', 1)[-1]} (URL)"
    did = re.sub(r"\W", "_", info["file"])[-60:] + "_" + req.role
    if S.downloads.get(did, {}).get("status") == "downloading":
        raise HTTPException(409, "ダウンロードしています")
    d = S.downloads[did] = {"id": did, "role": req.role, "label": label, "status": "downloading",
                            "done": 0, "total": 0, "error": None}

    def work():
        try:
            path = llm.download_from_url(info, str(CACHE_DIR), log=S.log,
                                         progress=lambda done, total: d.update(done=done, total=total))
            preset = {"label": label, "repo": info.get("repo", ""), "file": info["file"],
                      "path": "" if info["kind"] == "hf" else path, "src": req.url}
            lst = S.custom_presets.setdefault(req.role, [])
            lst[:] = [p for p in lst if p["src"] != req.url and p["file"] != info["file"]]
            lst.insert(0, preset)
            S.model_cfg[req.role].update(repo=preset["repo"], file=preset["file"], path=preset["path"], url="")
            _save_settings()
            S.servers[req.role].stop()
            d["status"] = "done"
            S.log(f"[{req.role}] {label} を候補に追加して選択しました")
        except Exception as e:
            d.update(status="error", error=str(e))
            S.log(f"[{req.role}] URL からのダウンロード失敗: {e}")
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "id": did, "file": info["file"]}


@app.post("/api/models/custom/delete")
def delete_custom_preset(body: dict):
    role, src = body.get("role", ""), body.get("src", "")
    S.custom_presets[role] = [p for p in S.custom_presets.get(role, []) if p["src"] != src]
    _save_settings()
    return {"ok": True}


@app.get("/api/models/files")
def model_files(repo: str):
    try:
        files = llm.list_gguf(repo)
    except Exception as e:
        raise HTTPException(400, f"ファイル一覧を取得できませんでした: {e}")
    return {"files": files, "default": llm.pick_default_file(files, *llm.memory_budget())}


class ModelCfg(BaseModel):
    role: str
    repo: str = ""
    file: str = ""
    ctx: int = 16384
    n_cpu_moe: int = -1    # -1 = VRAM に合わせて自動
    url: str = ""          # 既存の OpenAI 互換サーバー (ローカル実行時の Ollama/LM Studio 等)
    path: str = ""         # URL から取得したモデルのローカルパス
    prompt_style: str = "insttrans"   # 翻訳の依頼形式。"generic" は自動テスト専用
    load_now: bool = False


@app.post("/api/models/config")
def set_model(cfg: ModelCfg):
    if cfg.role not in S.model_cfg:
        raise HTTPException(400, "role")
    if cfg.role == "translator" and not cfg.url and not tr.is_insttrans_model(cfg.repo, cfg.file, cfg.path):
        raise HTTPException(400, "翻訳モデルは Index-Translate だけを使えます")
    S.model_cfg[cfg.role] = cfg.model_dump(exclude={"role", "load_now"})
    _save_settings()   # 再起動後も同じモデルで再開
    S.servers[cfg.role].stop()
    if cfg.load_now:
        threading.Thread(target=_ensure_model, args=(cfg.role,), daemon=True).start()
    return {"ok": True}


@app.post("/api/models/unload")
def unload(body: dict):
    S.servers[body.get("role", "")].stop()
    return {"ok": True}


def _ensure_model(role: str) -> bool:
    cfg, srv = S.model_cfg[role], S.servers[role]
    srv.external_url = cfg.get("url", "").rstrip("/")
    if srv.external_url:
        return True
    local = cfg.get("path", "")
    if not cfg["repo"] and not local:
        return False
    key = local or cfg["repo"] + "/" + cfg["file"]
    if srv.running() and srv.repo == key:
        return True
    try:
        # 起動できない環境で数 GB のモデルを先にダウンロードしないよう、先に llama-server を確認する
        if not llm.find_server_binary():
            raise RuntimeError("llama-server が見つかりません。セットアップを実行するか、"
                               "OpenAI 互換サーバーの URL を設定してください。")
        vram, ram = llm.memory_budget()
        if local:
            if not Path(local).exists():
                raise RuntimeError(f"モデルファイルがありません (URL から再取得してください): {local}")
            file, path = Path(local).name, local
        else:
            file = cfg["file"] or llm.pick_default_file(llm.list_gguf(cfg["repo"]), vram, ram)
            path = llm.download(cfg["repo"], file, str(CACHE_DIR), log=S.log)
        n_cpu_moe = cfg["n_cpu_moe"]
        if n_cpu_moe < 0:
            n_cpu_moe = llm.auto_cpu_moe(path, vram)
            S.log(f"[{role}] {file}: --n-cpu-moe 自動設定 = {n_cpu_moe}")
        srv.start(path, ctx=cfg["ctx"], n_cpu_moe=n_cpu_moe, log=S.log)
        srv.repo = key
        return True
    except Exception as e:
        S.log(f"[{role}] モデル読み込み失敗: {e}")
        return False


def _gpu() -> dict:
    try:
        o = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=5).stdout
        n, used, total, util = [x.strip() for x in o.splitlines()[0].split(",")]
        return {"name": n, "mem_used": int(used), "mem_total": int(total), "util": int(util)}
    except Exception:
        return {}


@app.get("/api/status")
def status():
    return {"job": S.job, "logs": S.logs[-200:], "gpu": _gpu(), "workdir": str(WORK),
            "resumable": [jobs.summarize(j) for j in JOBS.resumable()],
            "models": {r: {"running": s.running(), "ready": s.running() and s.ready(), "repo": s.repo or s.external_url}
                       for r, s in S.servers.items()},
            "outputs": sorted(p.name for p in OUT_DIR.glob("*.zip")),
            "downloads": list(S.downloads.values())}


# ------------------------------------------------------------------ glossary
@app.get("/api/glossary")
def get_glossary():
    m = S.memory
    return {"user": m.user_glossary, "meta": m.user_meta, "official": m.official, "auto": m.glossary,
            "base": tr.BASE_GLOSSARY, "tm_size": len(m.tm)}


@app.post("/api/glossary")
def set_glossary(body: dict):
    S.memory.user_glossary = {k.strip(): v.strip() for k, v in body.get("user", {}).items() if k.strip() and v.strip()}
    meta = body.get("meta") or {}
    S.memory.user_meta = {k.strip(): {"note": str(m.get("note", "")).strip()[:200], "case": bool(m.get("case"))}
                          for k, m in meta.items()
                          if k.strip() in S.memory.user_glossary and isinstance(m, dict) and (m.get("note") or m.get("case"))}
    S.memory.save()
    return {"ok": True}


BATCH_MODES = ("per_mod", "mixed")
BATCH_LINES_MAX = 1000


@app.get("/api/batch")
def get_batch():
    return S.batch_cfg


@app.post("/api/batch")
def set_batch(body: dict):
    """翻訳のまとめ方。実行中に変えた場合は、次に翻訳を始めたとき (再開を含む) から使う。"""
    mode = body.get("mode", S.batch_cfg["mode"])
    lines = body.get("lines", S.batch_cfg["lines"])
    if mode not in BATCH_MODES:
        raise HTTPException(400, "まとめ方は per_mod か mixed を指定してください")
    if not isinstance(lines, int) or isinstance(lines, bool) or not 1 <= lines <= BATCH_LINES_MAX:
        raise HTTPException(400, f"行数は 1〜{BATCH_LINES_MAX} の整数で指定してください")
    S.batch_cfg.update(mode=mode, lines=lines)
    _save_settings()
    return S.batch_cfg


@app.get("/api/prompt")
def get_prompt():
    return {**S.prompt_cfg, "options": {"proper_nouns": {"katakana": "カタカナにする (Mod名・人名は原文)",
                                                          "original": "原文のまま"}}}


@app.post("/api/prompt")
def set_prompt(body: dict):
    if body.get("proper_nouns") in tr.PROPER_NOUN_RULES:
        S.prompt_cfg["proper_nouns"] = body["proper_nouns"]
    if isinstance(body.get("custom"), str):
        S.prompt_cfg["custom"] = body["custom"][:2000]
    _save_settings()
    return {"ok": True}


@app.get("/api/prompt/preview")
def preview_prompt(profiles: str = ""):
    """実際に使われるシステムプロンプトの確認用。"""
    return {"system": tr.build_system_prompt([p for p in profiles.split(",") if p], S.prompt_cfg["proper_nouns"],
                                             S.prompt_cfg["custom"])}


@app.post("/api/memory/clear")
def clear_memory():
    S.memory.tm.clear()
    S.memory.glossary.clear()
    S.memory.official.clear()
    S.memory.save()
    return {"ok": True}


# ------------------------------------------------------------------ 追加リソース探索
RES_FILE = WORK / "resources.json"   # uid -> {"scanned", "helper", "files": [...]} (選択状態も保存)


def _res_scan() -> dict:
    return jobs.read_json(RES_FILE, {}) or {}


def _res_file(uid: str, path: str) -> dict | None:
    return next((f for f in _res_scan().get(uid, {}).get("files", []) if f["path"] == path), None)


def _unit_source(u: dict, langs: dict) -> dict[str, str]:
    """ユニットの原文エントリ。lang は抽出済みのもの、追加リソースは jar から読み直して抽出。"""
    if u.get("kind", "lang") == "lang":
        return langs[u["uid"]][u["ns"]]["entries"]
    return res.extract(res.read_source(MODS_DIR / u["uid"], u["path"]), u["mode"], u["keys"])


class ResScanReq(BaseModel):
    uids: list[str]


@app.post("/api/resources/scan")
def scan_resources(req: ResScanReq):
    """選択 MOD の lang 以外の翻訳候補を探し、補助モデルで翻訳要否・抽出キーを判定する。"""
    uids = [u for u in req.uids if u in S.mods]
    if not uids:
        raise HTTPException(400, "Mod を選んでください")
    with S.lock:
        if S.job["running"]:
            raise HTTPException(409, "ほかの処理を実行しています")
        S.job.update(running=True, kind="scan", phase="ファイルを調べています", current="", error=None, result=None, done=0, total=len(uids))

    def work():
        try:
            use_helper = bool(S.model_cfg["helper"]["repo"] or S.model_cfg["helper"]["url"]) and _ensure_model("helper")
            for uid in uids:
                S.job["current"] = S.mods[uid].name
                _scan_one(uid, use_helper)
                S.job["done"] += 1
            S.log(f"リソース探索完了: {len(uids)} MOD")
        except Exception as e:
            S.job["error"] = str(e)
            S.log(f"リソース探索エラー: {e}")
            traceback.print_exc()
        finally:
            S.job["running"] = False
            S.job["phase"] = "完了" if not S.job["error"] else "失敗"
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


def _scan_one(uid: str, use_helper: bool):
    jar = MODS_DIR / uid
    cands = res.scan_candidates(jar)
    decisions: dict[str, dict] = {}
    if use_helper and cands:
        for i in range(0, len(cands), 15):
            batch = cands[i:i + 15]
            body = "\n\n".join(
                f"## {c['path']}\n- overridable: {str(c['overridable']).lower()}\n- 文字列数: {c['strings']}\n"
                f"```\n{c['snippet'][:400]}\n```" for c in batch)
            r = _helper_chat(res.HELPER_CLASSIFY_PROMPT, f"# MOD: {S.mods[uid].name}\n\n{body}") or {}
            for d in r.get("files") or []:
                if isinstance(d, dict) and isinstance(d.get("path"), str):
                    decisions[d["path"]] = d
    files = []
    for c in cands:
        c["_text"] = res.read_source(jar, c["path"])
        d = res.apply_helper_decision(c, decisions.get(c["path"])) if use_helper else res.rule_decision(c)
        del c["_text"]
        n = len(res.extract(res.read_source(jar, c["path"]), c["mode"], d["keys"] or None)) if c["overridable"] else 0
        files.append({**{k: v for k, v in c.items() if k != "snippet"}, **d, "entries": n,
                      "selected": d["translate"] and n > 0, "by_helper": use_helper and c["path"] in decisions})
    data = _res_scan()
    data[uid] = {"scanned": time.time(), "helper": use_helper, "files": files}
    jobs.atomic_write_json(RES_FILE, data)
    S.log(f"{S.mods[uid].name}: 候補 {len(files)} / 翻訳推奨 {sum(f['selected'] for f in files)}")


@app.get("/api/resources")
def get_resources():
    data = _res_scan()
    return {uid: {**v, "name": S.mods[uid].name} for uid, v in data.items() if uid in S.mods}


class ResSelectReq(BaseModel):
    uid: str
    paths: list[str]


@app.post("/api/resources/select")
def select_resources(req: ResSelectReq):
    data = _res_scan()
    if req.uid not in data:
        raise HTTPException(404, "この Mod はまだ調べていません")
    for f in data[req.uid]["files"]:
        f["selected"] = f["path"] in req.paths and f["overridable"] and f["entries"] > 0
    jobs.atomic_write_json(RES_FILE, data)
    return {"ok": True, "selected": sum(f["selected"] for f in data[req.uid]["files"])}


@app.get("/api/resources/preview")
def preview_resource(uid: str, path: str):
    f = _res_file(uid, path)
    if not f or uid not in S.mods:
        raise HTTPException(404)
    text = res.read_source(MODS_DIR / uid, path)
    entries = res.extract(text, f["mode"], f["keys"]) if f["overridable"] else {}
    return {"path": path, "target": f["target"], "mode": f["mode"], "keys": f["keys"],
            "entries": dict(list(entries.items())[:30]), "total": len(entries)}


def selected_resources(uids: list[str] | None = None) -> dict[str, list[str]]:
    data = _res_scan()
    return {uid: [f["path"] for f in v["files"] if f["selected"]] for uid, v in data.items()
            if (uids is None or uid in uids) and uid in S.mods and any(f["selected"] for f in v["files"])}


@app.get("/api/resources/selected")
def get_selected_resources():
    return selected_resources()


# ------------------------------------------------------------------ FTB Quests
FTBQ_DIR = WORK / "ftbquests"           # <id>/src/... (アップロードした lang), <id>/project.json
PACKS_DIR = FTBQ_DIR / "_packs"         # 名前辞書用にアップロードしたリソースパック
FTBQ_OUT_PREFIX = "FTBQuests_"


def _safe_rel(p: str) -> str | None:
    p = p.replace("\\", "/").lstrip("/")
    parts = [x for x in p.split("/") if x not in ("", ".")]
    if not parts or any(x == ".." or ":" in x for x in parts):
        return None
    return "/".join(parts)


def _ftbq_project(pid: str) -> dict:
    if not re.fullmatch(r"[\w\-]+", pid):
        raise HTTPException(404)
    meta = jobs.read_json(FTBQ_DIR / pid / "project.json")
    if not meta:
        raise HTTPException(404)
    return meta


def _ftbq_analyze(pid: str, name: str) -> dict:
    src = FTBQ_DIR / pid / "src"
    rels = [p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()]
    docs = []
    for d in ftbq.find_documents(rels):
        try:
            data = ftbq.parse_lang_snbt((src / d["path"]).read_text("utf-8"))
        except (ftbq.SnbtError, UnicodeDecodeError) as e:
            S.log(f"[FTB Quests] 読み込めないファイルを除外: {d['path']} ({e})")
            continue
        existing = 0
        if d["ja"]:
            try:
                existing = len(ftbq.existing_entries(data, ftbq.parse_lang_snbt((src / d["ja"]).read_text("utf-8"))))
            except (ftbq.SnbtError, UnicodeDecodeError):
                pass
        docs.append({**d, "entries": len(ftbq.extract_entries(data)), "existing_ja": existing,
                     "title": ftbq.chapter_title(data) or d["label"]})
    meta = {"id": pid, "name": name, "created": time.time(), "docs": docs, "results": []}
    jobs.atomic_write_json(FTBQ_DIR / pid / "project.json", meta)
    return meta


@app.post("/api/ftbq/upload")
async def ftbq_upload(request: Request):
    """ftbquests フォルダ (zip / フォルダ選択) をアップロード。lang の .snbt / .snbt_merged だけ保存する。"""
    form = await request.form(max_files=20000, max_fields=40000)   # 大型パックはフォルダ選択で数千ファイルになる
    files = form.getlist("files")
    paths = form.getlist("paths")
    name = str(form.get("name") or "").strip()
    pid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    src = FTBQ_DIR / pid / "src"
    saved = 0

    def keep(rel: str | None) -> bool:
        # 必要なのは英語 (原文) と既存の日本語訳だけ
        return bool(rel) and re.search(r"(^|/)lang/(en_us|ja_jp)(\.snbt$|/)", rel) is not None \
            and rel.endswith((".snbt", ".snbt_merged"))

    for i, f in enumerate(files):
        fname = (paths[i] if i < len(paths) else None) or f.filename or ""
        body = await f.read()
        if fname.lower().endswith(".zip"):
            import io
            import zipfile
            try:
                zf = zipfile.ZipFile(io.BytesIO(body))
            except zipfile.BadZipFile:
                raise HTTPException(400, f"ZIP を開けませんでした: {fname}")
            for info in zf.infolist():
                rel = _safe_rel(info.filename)
                if not info.is_dir() and keep(rel) and info.file_size < 50 * 2**20:
                    (src / rel).parent.mkdir(parents=True, exist_ok=True)
                    (src / rel).write_bytes(zf.read(info))
                    saved += 1
            name = name or Path(fname).stem
        else:
            rel = _safe_rel(fname)
            if keep(rel):
                (src / rel).parent.mkdir(parents=True, exist_ok=True)
                (src / rel).write_bytes(body)
                saved += 1
                name = name or rel.split("/")[0]
    meta = _ftbq_analyze(pid, name or pid)
    if not meta["docs"]:
        shutil.rmtree(FTBQ_DIR / pid, ignore_errors=True)
        raise HTTPException(400, "言語ファイルが見つかりません。ftbquests フォルダを選んでください。")
    S.log(f"[FTB Quests] {meta['name']}: 言語ファイル {len(meta['docs'])} 件を読み込み")
    return meta


@app.get("/api/ftbq")
def ftbq_list():
    out = [m for p in FTBQ_DIR.glob("*/project.json") if (m := jobs.read_json(p))]
    return sorted(out, key=lambda m: m["created"], reverse=True)   # 新しい順


@app.post("/api/ftbq/{pid}/delete")
def ftbq_delete(pid: str):
    _ftbq_project(pid)
    shutil.rmtree(FTBQ_DIR / pid, ignore_errors=True)
    return {"ok": True}


@app.post("/api/ftbq/packs")
async def ftbq_upload_packs(files: list[UploadFile] = File(...)):
    """名前辞書に使う他のリソースパック (zip) をアップロード。"""
    PACKS_DIR.mkdir(parents=True, exist_ok=True)
    added = []
    for f in files:
        n = _safe_name(f.filename or "pack.zip")
        if not n.lower().endswith(".zip"):
            continue
        with (PACKS_DIR / n).open("wb") as out:
            shutil.copyfileobj(f.file, out)
        added.append(n)
    return {"added": added}


@app.get("/api/ftbq/packs")
def ftbq_packs():
    up = [{"src": "uploaded", "name": p.name} for p in sorted(PACKS_DIR.glob("*.zip"))] if PACKS_DIR.exists() else []
    ours = [{"src": "output", "name": p.name} for p in sorted(OUT_DIR.glob("*.zip"), reverse=True)
            if not p.name.startswith(FTBQ_OUT_PREFIX)]
    return up + ours


def _pack_ja_names(path: Path) -> dict[tuple[str, str], str]:
    """リソースパック zip の ja_jp から (namespace, キー) -> 日本語。"""
    import zipfile
    out = {}
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, FileNotFoundError):
        return out
    for n in zf.namelist():
        m = re.match(r"^assets/([^/]+)/lang/ja_jp\.(json|lang)$", n, re.I)
        if m:
            for k, v in modscan.parse_lang(modscan.read_text(zf, n), m.group(2).lower()).items():
                out[(m.group(1), k)] = v
    return out


def build_name_dict(packs: list[dict]) -> tuple[dict[str, str], dict]:
    """MOD の en_us (キー → 英語名) と、リソースパック / MOD 同梱の ja_jp (同じキー → 日本語名) を
    突き合わせて「英語のアイテム・ブロック名 → 日本語名」の辞書を作る。
    優先度: 後に指定したパック > 先に指定したパック > MOD 同梱 ja_jp。"""
    en: dict[tuple[str, str], str] = {}
    ja: dict[tuple[str, str], str] = {}
    for uid, m in S.mods.items():
        try:
            langs = modscan.load_lang_entries(MODS_DIR / uid, m)
        except Exception:
            continue
        for ns, d in langs.items():
            for k, v in d["entries"].items():
                if tr.NAME_KEY_RE.match(k):
                    en[(ns, k)] = v
                    if k in d["existing_ja"]:
                        ja[(ns, k)] = d["existing_ja"][k]
    for p in packs:
        base = PACKS_DIR if p.get("src") == "uploaded" else OUT_DIR
        ja.update(_pack_ja_names(base / _safe_name(p.get("name", ""))))
    names: dict[str, str] = {}
    conflicts = 0
    for key, en_name in en.items():
        j = ja.get(key)
        if not j or j == en_name or len(en_name) > 60 or tr.protect(en_name)[1]:
            continue
        if en_name in names and names[en_name] != j:
            conflicts += 1          # 同じ英語名で別の訳 → 先に見つかった方 (一貫性のため 1 つに固定)
            continue
        names[en_name] = j
    for k, v in S.memory.user_glossary.items():
        names[k] = v               # ユーザー指定は常に最優先
    return names, {"mods": len(S.mods), "names": len(names), "conflicts": conflicts}


class FtbqReq(BaseModel):
    use_existing_ja: bool = True
    packs: list[dict] = []          # [{"src": "uploaded"|"output", "name": "..."}]
    chunk_items: int = 30
    chunk_chars: int = 3500


@app.post("/api/ftbq/{pid}/translate")
def ftbq_translate(pid: str, req: FtbqReq):
    meta = _ftbq_project(pid)
    if S.job["running"]:
        raise HTTPException(409, "ほかの処理を実行しています")
    job = JOBS.create({"kind": "ftbq", "project": pid, "pack_name": f"FTB Quests: {meta['name']}",
                       "uids": [], **req.model_dump()})
    _start_thread(job)
    return {"ok": True, "job_id": job["id"]}


@app.get("/api/ftbq/names")
def ftbq_names(packs: str = "[]"):
    """名前辞書のプレビュー (件数と先頭 50 件)。"""
    names, stats = build_name_dict(json.loads(packs))
    return {**stats, "sample": dict(list(names.items())[:50])}


def _ftbq_job(job: dict):
    req = FtbqReq(**{k: v for k, v in job["request"].items() if k in FtbqReq.model_fields})
    pid = job["request"]["project"]
    meta = _ftbq_project(pid)
    src = FTBQ_DIR / pid / "src"
    mem, steps = S.memory, job["steps"]
    pseudo = lambda title: modscan.ModInfo(
        uid="ftbquests", filename="", name=f"FTB Quests クエストブック「{title}」",
        description="Minecraft の MOD パックのクエストブック (FTB Quests)。クエスト名・サブタイトル・説明文。"
                    "説明文はリストの 1 要素が 1 行。アイテム・ブロック名は用語集の訳に必ず合わせる。")

    S.job["phase"] = "アイテム名の辞書を作成しています"
    names, stats = build_name_dict(req.packs)
    S.log(f"[FTB Quests] アイテム・ブロック名辞書: {stats['names']} 語 (MOD {stats['mods']} 個"
          + (f", 表記の衝突 {stats['conflicts']} 件は先勝ち" if stats["conflicts"] else "") + ")")
    steps["deps"] = True

    datas = {d["path"]: ftbq.parse_lang_snbt((src / d["path"]).read_text("utf-8")) for d in meta["docs"]}
    if not steps["plan"]:
        by_name = 0
        for d in meta["docs"]:
            data = datas[d["path"]]
            entries = ftbq.extract_entries(data)
            existing = {}
            if req.use_existing_ja and d["ja"] and (src / d["ja"]).exists():
                try:
                    existing = ftbq.existing_entries(data, ftbq.parse_lang_snbt((src / d["ja"]).read_text("utf-8")))
                except ftbq.SnbtError:
                    pass
            pre = {}
            for k, v in entries.items():
                hit = ftbq.name_match(v, names)       # アイテム名そのもの → 辞書の日本語名 (表記揺れ防止)
                if hit is not None:
                    pre[k] = hit
                    by_name += 1
                elif k in existing:
                    pre[k] = existing[k]
                elif v in mem.tm:
                    pre[k] = mem.tm[v]
                elif tr.should_skip(v):
                    pre[k] = v
            unit = jobs.unit_id("ftbq", d["path"])
            JOBS.save_part(job, unit, pre)
            total = len(entries) - len(pre)
            job["units"][unit] = {"kind": "ftbq", "uid": "ftbquests", "ns": d["path"], "path": d["path"],
                                  "out": d["out"], "title": d["title"], "format": "snbt",
                                  "total": total, "done": 0, "status": "done" if total == 0 else "pending"}
        steps["plan"] = True
        JOBS.save(job)
        JOBS.event(job, "plan", units=len(job["units"]), by_name=by_name)
        S.log(f"[FTB Quests] アイテム名と一致して辞書で確定: {by_name} 件")

    units = job["units"]
    S.job["total"] = sum(u["total"] for u in units.values())
    S.job["done"] = sum(u["done"] for u in units.values())
    remaining = [k for k, u in units.items() if u["status"] != "done"]
    S.log(f"[FTB Quests] 翻訳対象 {S.job['total']} 件 / 残り {S.job['total'] - S.job['done']} 件")
    if remaining:
        S.job["phase"] = "翻訳モデルを読み込んでいます"
        if not _ensure_model("translator"):
            raise RuntimeError("翻訳モデルを起動できませんでした。ログを確認してください。")
        srv = S.servers["translator"]
        for unit in remaining:
            u = units[unit]
            S.job["phase"], S.job["current"] = "翻訳しています", f"FTB Quests: {u['title']}"
            out = JOBS.load_part(job, unit)
            entries = ftbq.extract_entries(datas[u["path"]])
            pending = []
            for k, v in entries.items():
                if k in out:
                    continue
                if v in mem.tm:                       # 章ファイルで訳した文は全体ファイルでも同じ訳
                    out[k] = mem.tm[v]
                else:
                    pending.append((k, v))
            prev = dict(list(out.items())[-8:])
            mod = pseudo(u["title"])
            for chunk in tr.chunk_entries(pending, _chunk_items(req.chunk_items), req.chunk_chars):
                _check_cancel()
                got = _translate_chunk(srv, mod, chunk, prev, extra_terms=names, namespace="ftbquests")
                out.update(got)
                prev = dict(list(got.items())[-8:])
                JOBS.save_part(job, unit, out)
                u["done"] = min(u["total"], u["done"] + len(chunk))
                S.job["done"] = sum(x["done"] for x in units.values())
                mem.save()
                JOBS.save(job)
            JOBS.save_part(job, unit, out)
            u["status"], u["done"] = "done", u["total"]
            JOBS.save(job)
            JOBS.event(job, "unit_done", unit=unit)

    # ---- 出力: en_us と同じ構成で ja_jp を作り zip にまとめる
    _check_cancel()
    S.job["phase"] = "書き出しています"
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for unit, u in units.items():
            translated = ftbq.apply_entries(datas[u["path"]], JOBS.load_part(job, unit))
            zf.writestr(ftbq.normalize_out_path(u["out"]), ftbq.write_lang_snbt(translated))
        zf.writestr("README_日本語化.txt",
                    "config フォルダを Minecraft のインスタンスフォルダに配置してください。\n"
                    "既存の config/ftbquests/quests/lang/ja_jp/ と ja_jp.snbt は、配置前にバックアップしてください。\n")
    name = f"{FTBQ_OUT_PREFIX}{_safe_name(meta['name'])}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    (OUT_DIR / name).write_bytes(buf.getvalue())
    meta["results"] = [name] + meta.get("results", [])
    jobs.atomic_write_json(FTBQ_DIR / pid / "project.json", meta)
    job["result"], steps["pack"] = name, True
    S.job["result"] = name
    S.log(f"[FTB Quests] 完成: {name}")


# ------------------------------------------------------------------ translate job
class TranslateReq(BaseModel):
    uids: list[str]
    include_deps: bool = True
    use_existing_ja: bool = True
    swap_models: bool = True      # VRAM 節約: 補助モデル→解放→翻訳モデル
    pack_format: int = 34
    pack_name: str = "MOD日本語化パック"
    chunk_items: int = 40
    chunk_chars: int = 3500
    include_lang: bool = True
    resources: dict[str, list[str]] = {}   # uid -> 翻訳する追加リソースのパス (探索タブで選択)


class Cancelled(Exception):
    pass


def _start_thread(job: dict):
    with S.lock:
        if S.job["running"]:
            raise HTTPException(409, "ほかの処理を実行しています")
        S.cancel.clear()
        S.job.update(running=True, id=job["id"], kind=job["request"].get("kind", "mod"), phase="準備中…", done=0, total=0, current="", result=None,
                     error=None, started=time.time())
    threading.Thread(target=_run_job, args=(job,), daemon=True).start()


@app.post("/api/translate")
def start_translate(req: TranslateReq):
    if S.job["running"]:
        raise HTTPException(409, "ほかの処理を実行しています")
    # 追加リソースだけ選んだ MOD も翻訳対象に含める
    req.uids = list(dict.fromkeys(req.uids + [u for u, ps in req.resources.items() if ps and u in S.mods]))
    if not req.uids:
        raise HTTPException(400, "翻訳する Mod がありません")
    job = JOBS.create(req.model_dump())
    _start_thread(job)
    return {"ok": True, "job_id": job["id"]}


@app.get("/api/jobs")
def list_jobs():
    return [jobs.summarize(j) for j in JOBS.list()]


@app.get("/api/jobs/{job_id}/history")
def job_history(job_id: str):
    return JOBS.read_history(job_id)


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: str):
    try:
        job = JOBS.load(job_id)
    except ValueError:
        job = None
    if not job:
        raise HTTPException(404)
    if job["status"] not in jobs.RESUMABLE:
        raise HTTPException(400, f"このジョブは再開できません: {job['status']}")
    if S.job["running"]:
        raise HTTPException(409, "ほかの処理を実行しています")
    _rescan()
    lost = [u for u in job["request"].get("uids", []) + job["order"] if u not in S.mods]
    if lost:
        raise HTTPException(400, "Mod が見つかりません: " + ", ".join(dict.fromkeys(lost)))
    job["status"], job["error"] = jobs.RUNNING, None
    job["resumes"] += 1
    JOBS.save(job)
    JOBS.event(job, "resume", count=job["resumes"])
    _start_thread(job)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/delete")
def delete_job(job_id: str):
    if S.job["running"] and S.job.get("id") == job_id:
        raise HTTPException(409, "ほかの処理を実行しています")
    try:
        shutil.rmtree(JOBS.path(job_id), ignore_errors=True)
    except ValueError:
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/cancel")
def cancel():
    S.cancel.set()
    return {"ok": True}


@app.get("/api/download/{name}")
def download(name: str):
    p = OUT_DIR / _safe_name(name)
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p, filename=p.name, media_type="application/zip")


@app.post("/api/heartbeat")
def heartbeat():
    return {"t": time.time()}


HELPER_TIMEOUT = 240        # 補助モデル 1 回の待ち時間 (秒)。GPU なら数十秒で返る
HELPER_MAX_FAILURES = 2     # 連続でこの回数失敗したら補助モデルを止め、以降はルールだけで処理する


def _helper_chat(system: str, user: str) -> dict | None:
    srv = S.servers["helper"]
    if not srv.running():
        return None
    t0 = time.time()
    try:
        r = tr.parse_json_obj(srv.chat(system + tr.DATA_GUARD, user, temperature=0.1, max_tokens=1024,
                                       json_mode=True, timeout=HELPER_TIMEOUT))
        S.helper_failures = 0
        if time.time() - t0 > 60:
            S.log(f"[helper] 応答に {time.time() - t0:.0f} 秒かかりました")
        return r
    except Exception as e:
        S.helper_failures = getattr(S, "helper_failures", 0) + 1
        S.log(f"[helper] エラー ({time.time() - t0:.0f} 秒): {e}")
        if S.helper_failures >= HELPER_MAX_FAILURES:
            S.log("[helper] 応答がないため補助モデルを止め、以降はルールだけで処理します。llama-server の最近のログ:\n"
                  + srv.recent_log())
            srv.stop()      # 生成中の処理も止める (-np 1 なので次の依頼が詰まらないように)
            S.helper_failures = 0
        return None


def _glossary_from_ja(uid: str, lang_by_ns: dict) -> int:
    """MOD 同梱の ja_jp と en_us の対訳から用語集を作る。
    アイテム名などはルールで直接、文中の用語は補助モデルで抽出 (対訳に実在するものだけ採用)。"""
    mem = S.memory
    pairs = [p for d in lang_by_ns.values() for p in tr.existing_pairs(d)]
    if not pairs:
        return 0
    terms = tr.rule_glossary(pairs)
    if S.servers["helper"].running():
        # 1 回に渡す量を抑える (長い MOD は分割し、最大 200 件まで)
        for i in range(0, min(len(pairs), 200), 40):
            if not S.servers["helper"].running():
                break
            body = "\n".join(f"| {en[:200]} | {ja[:200]} |" for _, en, ja in pairs[i:i + 40])
            r = _helper_chat(tr.HELPER_JA_GLOSSARY_PROMPT,
                             f"## MOD: {S.mods[uid].name}\n| 英語 | 日本語 |\n|---|---|\n{body}") or {}
            terms.update({k: v for k, v in tr.verify_terms(r.get("terms") or {}, pairs).items() if k not in terms})
    for en, ja in terms.items():
        if en not in mem.user_glossary:
            mem.official[en] = ja
    for _, en, ja in pairs:
        mem.tm.setdefault(en, ja)      # 同じ原文が他 MOD に出たら公式訳を再利用
    return len(terms)


class GlossaryFromJaReq(BaseModel):
    uids: list[str]
    include_deps: bool = True


@app.post("/api/glossary/from_ja")
def glossary_from_ja(req: GlossaryFromJaReq):
    """選択 MOD (+前提MOD) の同梱 ja_jp から用語集を作る (翻訳を始める前に確認したい場合用)。"""
    if S.job["running"]:
        raise HTTPException(409, "ほかの処理を実行しています")
    order = modscan.resolve_dependencies(req.uids, S.mods)[0] if req.include_deps else \
        [u for u in req.uids if u in S.mods]
    targets = [u for u in order if S.mods[u].has_ja]
    if not targets:
        return {"ok": True, "mods": 0, "message": "日本語訳を含む Mod がありません"}

    def work():
        S.job.update(running=True, kind="glossary", phase="用語を取り込んでいます", current="", error=None, result=None, done=0, total=len(targets))
        try:
            if S.model_cfg["helper"]["repo"] or S.model_cfg["helper"]["url"]:
                _ensure_model("helper")
            total = 0
            for uid in targets:
                S.job["current"] = S.mods[uid].name
                n = _glossary_from_ja(uid, modscan.load_lang_entries(MODS_DIR / uid, S.mods[uid]))
                S.log(f"{S.mods[uid].name}: 同梱 ja_jp から用語 {n} 件")
                total += n
                S.job["done"] += 1
            S.memory.save()
            S.log(f"用語集作成完了: {len(targets)} MOD / {total} 語")
        except Exception as e:
            S.job["error"] = str(e)
            S.log(f"用語集作成エラー: {e}")
        finally:
            S.job["running"] = False
            S.job["phase"] = "完了" if not S.job["error"] else "失敗"
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True, "mods": len(targets)}


def _run_job(job: dict):
    try:
        if job["request"].get("kind") == "ftbq":
            _ftbq_job(job)
        else:
            _job(job)
        job["status"] = jobs.DONE
        JOBS.event(job, "done", result=job["result"])
    except Cancelled:
        job["status"] = jobs.CANCELLED
        S.job["error"] = "中止しました。続きから再開できます。"
        JOBS.event(job, "cancelled")
        S.log(S.job["error"])
    except Exception as e:
        job["status"], job["error"] = jobs.FAILED, str(e)
        S.job["error"] = str(e) + " 続きから再開できます。"
        JOBS.event(job, "failed", error=str(e))
        S.log("エラー: " + "".join(traceback.format_exception_only(type(e), e)).strip())
        traceback.print_exc()
    finally:
        JOBS.save(job)
        S.memory.save()
        S.job["running"] = False
        S.job["phase"] = {jobs.DONE: "完了", jobs.CANCELLED: "中断"}.get(job["status"], "失敗")


def _check_cancel():
    if S.cancel.is_set():
        raise Cancelled()


def _job(job: dict):
    """段階ごとに job.json へ記録しながら進める。再開時は完了済みの段階・チャンクを飛ばす。"""
    req = TranslateReq(**job["request"])
    mem, steps = S.memory, job["steps"]
    use_helper = bool(S.model_cfg["helper"]["repo"] or S.model_cfg["helper"]["url"])
    if job["resumes"]:
        S.log(f"ジョブ {job['id']} を再開 ({job['resumes']} 回目)")

    # ---- 1. 依存解決 (完了済みなら記録を使う)
    if not steps["deps"]:
        S.job["phase"] = "前提 Mod を調べています"
        order, missing = modscan.resolve_dependencies(req.uids, S.mods)
        if not req.include_deps:
            order = [u for u in order if u in req.uids]
        if missing and req.include_deps and use_helper and _ensure_model("helper"):
            listing = {u: {"ids": m.mod_ids, "name": m.name} for u, m in S.mods.items() if u not in order}
            if listing:
                r = _helper_chat(tr.HELPER_DEPS_PROMPT, "## 依存ID\n" + json.dumps(missing) +
                                 "\n## アップロード済み\n" + json.dumps(listing, ensure_ascii=False)) or {}
                for dep, uid in (r.get("matches") or {}).items():
                    if uid in S.mods and uid not in order:
                        order.insert(0, uid)
                        missing = [m for m in missing if m != dep]
                        S.log(f"[helper] 依存 {dep} → {uid}")
        job["order"], job["missing"], steps["deps"] = order, missing, True
        JOBS.save(job)
        JOBS.event(job, "deps", order=order, missing=missing)
    order = job["order"]
    if job["missing"]:
        S.log("未アップロードの前提MOD: " + ", ".join(job["missing"]))
    S.log("翻訳順: " + ", ".join(S.mods[u].name for u in order))
    langs = {uid: modscan.load_lang_entries(MODS_DIR / uid, S.mods[uid]) for uid in order}

    # ---- 2. 補助モデル: まだ用語抽出していない & 翻訳が終わっていない MOD だけ
    def mod_finished(uid):
        units = [u for u in job["units"].values() if u["uid"] == uid]
        return steps["plan"] and all(u["status"] == "done" for u in units)
    need_helper = [u for u in order if u not in steps["helper_done"] and not mod_finished(u)]
    if need_helper:
        if use_helper:
            S.log("補助モデルで処理する MOD: " + ", ".join(S.mods[u].name for u in need_helper))
            _ensure_model("helper")
        for uid in need_helper:
            _check_cancel()
            S.job["phase"], S.job["current"] = "用語を取り込んでいます", S.mods[uid].name
            # MOD 同梱の ja_jp (公式訳) から用語集 → 抽出より先に行い、公式の表記を優先させる
            n = _glossary_from_ja(uid, langs[uid])
            if n:
                S.log(f"{S.mods[uid].name}: 同梱 ja_jp から用語 {n} 件")
            if S.servers["helper"].running():
                S.job["phase"] = "用語を抽出しています"
                names = [v for ns in langs[uid].values() for k, v in ns["entries"].items()
                         if tr.NAME_KEY_RE.match(k) and k not in ns["existing_ja"]][:150]
                if names:
                    r = _helper_chat(tr.HELPER_TERMS_PROMPT, f"## MOD: {S.mods[uid].name}\n" + "\n".join(f"- {n}" for n in names))
                    for k, v in ((r or {}).get("terms") or {}).items():
                        if isinstance(v, str) and k not in mem.user_glossary and k not in tr.BASE_GLOSSARY \
                                and k not in mem.official:
                            mem.glossary.setdefault(k, v)
            mem.save()
            steps["helper_done"].append(uid)
            JOBS.save(job)
            JOBS.event(job, "helper", uid=uid, ja_terms=n)
    if req.swap_models:
        S.servers["helper"].stop()

    # ---- 3. 翻訳計画 (lang ファイル + 追加リソースのファイル単位)
    if not steps["plan"]:
        def add_unit(unit: str, entries: dict, existing: dict, info: dict):
            prefilled = {}
            for k, v in entries.items():
                if req.use_existing_ja and k in existing:
                    prefilled[k] = existing[k]
                elif v in mem.tm:
                    prefilled[k] = mem.tm[v]          # 過去訳を再利用 → 表記揺れ防止
                elif tr.should_skip(v):
                    prefilled[k] = v
            JOBS.save_part(job, unit, prefilled)
            total = len(entries) - len(prefilled)
            job["units"][unit] = {**info, "total": total, "done": 0, "status": "done" if total == 0 else "pending"}

        for uid in order:
            if req.include_lang:
                for ns, d in langs[uid].items():
                    add_unit(jobs.unit_id(uid, ns), d["entries"], d["existing_ja"],
                             {"kind": "lang", "uid": uid, "ns": ns, "format": d["format"]})
            for path in req.resources.get(uid, []):
                f = _res_file(uid, path)
                if not f or not f["overridable"]:
                    S.log(f"追加リソースをスキップ (探索結果なし/変更不可): {uid} {path}")
                    continue
                entries = res.extract(res.read_source(MODS_DIR / uid, path), f["mode"], f["keys"])
                # 出力先・抽出方法は計画時点で固定 (再開時に探索結果が変わっても同じファイルを作る)
                add_unit(jobs.res_unit_id(uid, path), entries, {},
                         {"kind": "res", "uid": uid, "ns": path, "path": path, "target": f["target"],
                          "mode": f["mode"], "keys": f["keys"], "format": "res"})
        steps["plan"] = True
        JOBS.save(job)
        JOBS.event(job, "plan", units=len(job["units"]),
                   resources=sum(u.get("kind") == "res" for u in job["units"].values()))

    units = job["units"]
    S.job["total"] = sum(u["total"] for u in units.values())
    S.job["done"] = sum(u["done"] for u in units.values())
    remaining = [k for k, u in units.items() if u["status"] != "done"]
    S.log(f"翻訳対象 {S.job['total']} 件 / 残り {S.job['total'] - S.job['done']} 件 ({len(remaining)} ファイル)")

    # ---- 4. 翻訳 (チャンクごとに保存)
    if remaining:
        S.job["phase"] = "翻訳モデルを読み込んでいます"
        if not _ensure_model("translator"):
            raise RuntimeError("翻訳モデルを起動できませんでした。ログを確認してください。")
        srv = S.servers["translator"]
        # まとめ方は「今」の設定を使う (再開時もダッシュボードで選ばれている方で続ける)
        mode, lines = S.batch_cfg["mode"], S.batch_cfg["lines"]
        S.log("翻訳のまとめ方: " + ("Mod に関係なく " + str(lines) + " 行ずつ" if mode == "mixed" else "Mod ごと"))
        JOBS.event(job, "batch_mode", mode=mode, lines=lines)
        if mode == "mixed":
            _translate_mixed(job, units, remaining, langs, srv, lines)
            remaining = []
        for unit in remaining:
            u = units[unit]
            mod = S.mods[u["uid"]]
            S.job["phase"], S.job["current"] = "翻訳しています", f"{mod.name} ({u['ns']})"
            out = JOBS.load_part(job, unit)
            src = _unit_source(u, langs)
            pending = [(k, v) for k, v in src.items() if k not in out]
            prev = dict(list(out.items())[-8:])          # 再開時も直前の訳を文脈に使う
            for chunk in tr.chunk_entries(pending, _chunk_items(req.chunk_items), req.chunk_chars):
                _check_cancel()
                got = _translate_chunk(srv, mod, chunk, prev, namespace=u["ns"])
                out.update(got)
                prev = dict(list(got.items())[-8:])
                JOBS.save_part(job, unit, out)            # ← ここで落ちても次回はこの続きから
                u["done"] = min(u["total"], u["done"] + len(chunk))
                S.job["done"] += len(chunk)
                mem.save()
                JOBS.save(job)
            u["status"], u["done"] = "done", u["total"]
            JOBS.save(job)
            JOBS.event(job, "unit_done", unit=unit)

    # ---- 5. すべての lang と追加リソースをまとめて 1 つのリソースパック
    _check_cancel()
    S.job["phase"] = "書き出しています"
    results: dict[str, dict] = {}
    extra: dict[str, str] = {}
    owner: dict[str, str] = {}
    for unit, u in units.items():
        src = _unit_source(u, langs)
        out = JOBS.load_part(job, unit)
        if u.get("kind", "lang") == "lang":
            entry = results.setdefault(u["ns"], {"format": u["format"], "entries": {}})
            entry["entries"].update({k: out.get(k, src[k]) for k in src})
            continue
        # 追加リソース: MOD ごとの出力先パスに配置。別 MOD と同じパスになる場合は先勝ちで警告
        tgt = u["target"]
        if tgt in owner:
            S.log(f"出力先が重複しています: {tgt} ({S.mods[owner[tgt]].name} / {S.mods[u['uid']].name})。"
                  f"後者は書き出しません")
            JOBS.event(job, "conflict", target=tgt, kept=owner[tgt], dropped=u["uid"])
            continue
        owner[tgt] = u["uid"]
        text = res.read_source(MODS_DIR / u["uid"], u["path"])
        extra[tgt] = res.rebuild(text, u["mode"], {k: out[k] for k in src if k in out}, u["keys"])
    name = f"{_safe_name(req.pack_name.replace('/', '_').replace(chr(92), '_'))}_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    tr.build_resource_pack(results, OUT_DIR / name, req.pack_format,
                           f"{req.pack_name} ({len(order)} MOD) - MC MOD Translator", extra_files=extra)
    if extra:
        S.log(f"追加リソース {len(extra)} ファイルをパックに追加")
    job["result"], steps["pack"] = name, True
    S.job["result"] = name
    S.log(f"完成: {name}")


def _translate_chunk(srv: llm.LlamaServer, mod: modscan.ModInfo, chunk: list[tuple[str, str]],
                     prev: dict[str, str], depth: int = 0, extra_terms: dict[str, str] | None = None,
                     namespace: str = "") -> dict[str, str]:
    mem = S.memory
    protected = {k: tr.protect(v) for k, v in chunk}
    payload = {k: p[0] for k, p in protected.items()}
    srcs = [v for _, v in chunk]
    terms = mem.relevant_terms(srcs, extra=extra_terms)
    max_tokens = max(1024, sum(len(v) for v in srcs) * 3)
    insttrans = _uses_insttrans()
    if insttrans:
        # Index-Translate: system を使わず、公式の instTrans 形式で 1 つの依頼文にする
        system = ""
        prompt = tr.build_insttrans_prompt(mod.name, payload, terms, prev, mem.notes(),
                                           S.prompt_cfg["proper_nouns"], S.prompt_cfg["custom"])
        extra = {"chat_template_kwargs": {"enable_thinking": False}}
    else:
        examples = {s: mem.tm[s] for s in mem.tm if any(len(s) > 3 and s.lower() in v.lower() for v in srcs)}
        prompt = tr.build_user_prompt(mod.name, mod.description, payload, terms, prev,
                                      dict(list(examples.items())[:40]), namespace=namespace, notes=mem.notes())
        system = tr.build_system_prompt(tr.detect_profiles(srcs, namespace), S.prompt_cfg["proper_nouns"],
                                        S.prompt_cfg["custom"])
        extra = None
    data = None
    for attempt in range(2):
        try:
            temp = (0.0 if insttrans else 0.2) + 0.1 * attempt
            data = tr.parse_json_obj(srv.chat(system, prompt, temperature=temp, max_tokens=max_tokens,
                                              json_mode=True, extra=extra))
        except Exception as e:
            S.log(f"[translator] 呼び出し失敗: {e}")
        if data:
            break
    data = data or {}
    good, bad = {}, []
    for k, v in chunk:
        t = data.get(k)
        if isinstance(t, str) and insttrans:
            t = tr.from_v_markers(t)
        restored = tr.restore(t, protected[k][1]) if isinstance(t, str) else None
        if restored is not None and restored.strip() and tr.structure_ok(v, restored) \
                and not tr.looks_untranslated(v, restored) \
                and not (insttrans and tr.glossary_violations(v, restored, terms)):
            _accept(k, v, restored, protected[k][1])
            good[k] = restored
        else:
            bad.append((k, v))
    if bad:
        if len(chunk) > 1 and depth < 3:
            # 失敗分は小さく分割して再翻訳
            half = max(1, len(bad) // 2)
            for sub in (bad[:half], bad[half:]):
                if sub:
                    good.update(_translate_chunk(srv, mod, sub, {**prev, **dict(list(good.items())[-4:])}, depth + 1,
                                                 extra_terms, namespace))
        else:
            for k, v in bad:
                t = _translate_single_plain(srv, v, terms) if insttrans else None
                if t is not None:
                    missing = tr.glossary_violations(v, t, terms)
                    if missing:
                        S.log(f"用語集どおりに訳せませんでした: {k} ({', '.join(missing)})")
                    _accept(k, v, t, tr.protect(v)[1])
                    good[k] = t
                    continue
                S.log(f"翻訳できなかったため原文を維持: {k}")
                good[k] = v
    return good


def _translate_mixed(job: dict, units: dict, remaining: list[str], langs: dict, srv: llm.LlamaServer,
                     lines: int):
    """Mod に関係なく、未訳の行を lines 行ずつまとめて訳す。
    進み具合は Mod ごと翻訳と同じく言語ファイル (unit) ごとの parts に保存するので、
    途中でまとめ方を切り替えて再開しても、訳し終えた行は二度と訳さない。"""
    parts = {unit: JOBS.load_part(job, unit) for unit in remaining}
    sources = {unit: _unit_source(units[unit], langs) for unit in remaining}
    items = [(unit, k, v) for unit in remaining for k, v in sources[unit].items() if k not in parts[unit]]
    # 行数が多いとモデルの文脈長を超えるので、文字数でも区切る (英語は 1 トークン ≒ 4 文字、出力の分も見込む)
    max_chars = max(2000, int(S.model_cfg["translator"].get("ctx", 16384) * 1.2))
    batches, cur, size = [], [], 0
    for it in items:
        if cur and (len(cur) >= lines or size + len(it[2]) > max_chars):
            batches.append(cur)
            cur, size = [], 0
        cur.append(it)
        size += len(it[1]) + len(it[2])
    if cur:
        batches.append(cur)
    if any(len(b) < lines for b in batches[:-1]):
        S.log(f"モデルの文脈長 ({S.model_cfg['translator'].get('ctx')}) に収まるよう、一部のまとまりを {lines} 行未満にしました")

    prev: dict[str, str] = {}
    for unit in remaining:                      # 再開時は、訳し終えた最後の数行を文脈に使う
        prev.update(dict(list(parts[unit].items())[-8:]))
    prev = dict(list(prev.items())[-8:])
    for batch in batches:
        _check_cancel()
        # 別の Mod に同じキーがあっても区別できるよう、重複したキーには番号を付けて送る
        chunk, mapping = [], {}
        for unit, k, v in batch:
            ck, n = k, 2
            while ck in mapping:
                ck, n = f"{k}#{n}", n + 1
            mapping[ck] = (unit, k)
            chunk.append((ck, v))
        uids = list(dict.fromkeys(units[unit]["uid"] for unit, _, _ in batch))
        if len(uids) == 1:
            mod = S.mods[uids[0]]
        else:
            names = [S.mods[u].name for u in uids]
            mod = modscan.ModInfo(uid="mixed", filename="",
                                  name="複数の Mod (" + ", ".join(names[:5]) + (" ほか" if len(names) > 5 else "") + ")")
        nss = {units[unit]["ns"] for unit, _, _ in batch}
        S.job["phase"], S.job["current"] = "翻訳しています", f"{len(batch)} 行 ({mod.name})"
        got = _translate_chunk(srv, mod, chunk, prev, namespace=nss.pop() if len(nss) == 1 else "")
        touched = set()
        for ck, t in got.items():
            unit, k = mapping[ck]
            parts[unit][k] = t
            touched.add(unit)
        for unit in touched:
            JOBS.save_part(job, unit, parts[unit])   # ← ここで落ちても次回はこの続きから
        for unit in {u for u, _, _ in batch}:
            u = units[unit]
            u["done"] = min(u["total"], u["done"] + sum(1 for x, _, _ in batch if x == unit))
            if all(k in parts[unit] for k in sources[unit]):
                u["status"], u["done"] = "done", u["total"]
                JOBS.event(job, "unit_done", unit=unit)
        S.job["done"] = sum(x["done"] for x in units.values())
        prev = dict(list({ck: got[ck] for ck in got}.items())[-8:])
        S.memory.save()
        JOBS.save(job)


def _uses_insttrans() -> bool:
    """翻訳は Index-Translate だけを使うので、常に instTrans 形式で依頼する。
    prompt_style="generic" は自動テスト用 (汎用の偽モデルで流れを確認するため)。"""
    return S.model_cfg["translator"].get("prompt_style", "insttrans") != "generic"


INSTTRANS_CHUNK_ITEMS = 8   # Index-Translate は 1 回の量が多いと名前と説明文が混ざりやすい


def _chunk_items(requested: int) -> int:
    return min(requested, INSTTRANS_CHUNK_ITEMS) if _uses_insttrans() else requested


SHORT_NAME_RE = re.compile(r"[A-Za-z][A-Za-z'\- ]{0,28}[A-Za-z]")


def _accept(key: str, src: str, ja: str, placeholders: list):
    mem = S.memory
    mem.tm[src] = ja
    if placeholders or src in mem.user_glossary or len(src) >= 40:
        return
    # アイテム名などのキーに加え、2 語以内の短い名前 (例: "Pocket") も用語集へ → 以降の訳で表記を統一
    short_name = SHORT_NAME_RE.fullmatch(src.strip()) and len(src.split()) <= 2
    if tr.NAME_KEY_RE.match(key) or short_name:
        mem.glossary.setdefault(src.strip(), ja.strip())


def _translate_single_plain(srv: llm.LlamaServer, src: str, terms: dict[str, str]) -> str | None:
    """1 項目だけを Index-Translate の基本形式 (JSON なし) で訳す。最後の手段。"""
    text, found = tr.protect(src)
    req = "请将以下文本翻译为日语，直接输出翻译结果，不要进行任何解释"
    hints = [f"保留占位符 {', '.join(tr.to_v_markers(f'⟦{i}⟧') for i in range(len(found)))}"] if found else []
    used = {k: v for k, v in terms.items() if k.lower() in src.lower()}
    if used:
        hints.append("术语映射：" + ", ".join(f"{k} -> {v}" for k, v in used.items()))
    prompt = req + ("（" + "；".join(hints) + "）" if hints else "") + "：\n\n" + tr.to_v_markers(text)
    try:
        out = srv.chat("", prompt, temperature=0.0, max_tokens=max(256, len(src) * 3),
                       extra={"chat_template_kwargs": {"enable_thinking": False}}).strip()
    except Exception as e:
        S.log(f"[translator] 呼び出し失敗: {e}")
        return None
    restored = tr.restore(tr.from_v_markers(out), found)
    if restored and tr.structure_ok(src, restored) and not tr.looks_untranslated(src, restored):
        return restored
    return None
