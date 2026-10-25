"""lang 以外でリソースパックから翻訳できるファイル (Patchouli の本・マニュアル・テキスト等) の探索・抽出・再構築。

リソースパックでの出力先はモデルに決めさせず、必ずこのモジュールのルールで決める:
  - パスに言語名 (en_us 等) を含む → その部分を ja_jp に置換 (例: patchouli_books/x/en_us/... → .../ja_jp/...)
  - 言語名を含まない → 同じパスで上書き (日本語以外の表示も置き換わる点を画面に表示)
  - data/ 以下 → リソースパックでは変更不可 (データパックの範囲) なので選択不可
"""
from __future__ import annotations

import json
import re
import zipfile

TEXT_EXT = {".json", ".txt", ".md", ".html", ".htm", ".xml"}
# 翻訳対象になり得ないアセットの種類 (assets/<ns>/<dir>/...)
SKIP_DIRS = {"lang", "models", "blockstates", "textures", "sounds", "shaders", "particles", "atlases", "font",
             "equipment", "items", "post_effect", "animations", "geo", "geckolib", "optifine", "emissive",
             "lithium", "sodium", "iris", "materials", "waypoints", "regional_compliancies", "gpu_warnlist.json"}
SKIP_FILES = {"sounds.json", "pack.mcmeta", "regional_compliancies.json", "gpu_warnlist.json"}
LOCALE_RE = re.compile(r"(?<![a-z])(en_us|en_US|en-us|en-US|en_gb|en)(?![a-z])")
LOCALE_STRICT_RE = re.compile(r"(?<![A-Za-z])(en_us|en_US|en-us|en-US)(?![A-Za-z])")
ID_LIKE_RE = re.compile(r"[a-z0-9_.\-/:#]+")
MAX_SIZE = 512 * 1024

# JSON で翻訳する既定のキー (補助モデルが追加・削除できる)
DEFAULT_JSON_KEYS = ["name", "title", "text", "description", "subtitle", "landing_text", "header", "tooltip",
                     "lines", "body", "message", "hint", "label", "display_name"]


def target_path(path: str) -> str | None:
    """リソースパック内での出力パス。変更不可なら None。"""
    if not path.startswith("assets/"):
        return None
    parts = path.split("/")
    for i, seg in enumerate(parts[2:], 2):
        stem, dot, ext = seg.partition(".")
        if LOCALE_STRICT_RE.fullmatch(stem):
            parts[i] = "ja_jp" + dot + ext
            return "/".join(parts)
        if LOCALE_STRICT_RE.fullmatch(seg):
            parts[i] = "ja_jp"
            return "/".join(parts)
    m = LOCALE_STRICT_RE.search(parts[-1])
    if m:                                        # 例: guide_en_us.md → guide_ja_jp.md
        parts[-1] = parts[-1][:m.start()] + "ja_jp" + parts[-1][m.end():]
        return "/".join(parts)
    return path                                  # 言語名なし → 同じパスで上書き


def has_locale(path: str) -> bool:
    return target_path(path) not in (None, path)


def _read(zf: zipfile.ZipFile, name: str) -> str:
    raw = zf.read(name)
    for enc in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            pass
    return raw.decode("cp1252", "replace")


def is_natural(s: str) -> bool:
    s = s.strip()
    return bool(s) and bool(re.search(r"[A-Za-z]{2}", s)) and not ID_LIKE_RE.fullmatch(s) \
        and not re.fullmatch(r"\S+\.(png|ogg|json|nbt)", s)


def guess_mode(path: str, text: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "json":
        return "json"
    lines = [l for l in text.splitlines() if l.strip()]
    paragraphs = [p for p in re.split(r"\n[ \t]*\n", text.strip()) if p.strip()]
    if lines and ext == "txt" and sum(len(l) for l in lines) / len(lines) < 120 \
            and (len(paragraphs) == 1 or all("\n" not in p.strip() for p in paragraphs)):
        return "lines"                            # splashes.txt のような 1 行 1 項目 (空行があっても)
    return "blocks"


def scan_candidates(jar_path) -> list[dict]:
    """jar 内の翻訳候補ファイルを列挙する (ルールベースの一次絞り込み)。"""
    out = []
    with zipfile.ZipFile(jar_path) as zf:
        names = set(zf.namelist())
        for info in zf.infolist():
            p = info.filename
            if info.is_dir() or info.file_size > MAX_SIZE:
                continue
            ext = "." + p.rsplit(".", 1)[-1].lower() if "." in p.rsplit("/", 1)[-1] else ""
            if ext not in TEXT_EXT or p.rsplit("/", 1)[-1] in SKIP_FILES:
                continue
            parts = p.split("/")
            if parts[0] not in ("assets", "data") or len(parts) < 3:
                continue
            if parts[0] == "assets" and (parts[2] in SKIP_DIRS or parts[2].endswith(".json") and len(parts) == 3):
                continue
            if parts[0] == "data" and not LOCALE_STRICT_RE.search(p):
                continue                           # data/ はレシピ等が大半。言語別のものだけ情報として出す
            text = _read(zf, p)
            mode = guess_mode(p, text)
            samples = natural_samples(text, mode)
            if not samples:
                continue
            tgt = target_path(p)
            # 既に日本語版が同梱されているものは不要
            if tgt and tgt != p and tgt in names:
                continue
            out.append({"path": p, "size": info.file_size, "mode": mode, "target": tgt,
                        "overridable": tgt is not None, "locale": bool(tgt and tgt != p),
                        "strings": len(samples), "snippet": text[:600]})
    return out


def natural_samples(text: str, mode: str, keys: list[str] | None = None) -> list[str]:
    if mode == "json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [v for _, v in _walk(data, "", None, keys or DEFAULT_JSON_KEYS) if is_natural(v)]
    return [v for _, v in _split(text, mode)[1] if is_natural(v)]


# ---------------------------------------------------------------- JSON
def _esc(k) -> str:
    return str(k).replace("~", "~0").replace("/", "~1")


def _walk(node, ptr: str, key, keys):
    """翻訳対象キー直下の文字列 (リストの中も含む) を (JSON pointer, 値) で返す。"""
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{ptr}/{_esc(k)}", k, keys)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{ptr}/{i}", key, keys)
    elif isinstance(node, str) and key in keys:
        yield ptr, node


def _set(data, ptr: str, value):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in ptr.split("/")[1:]]
    cur = data
    for p in parts[:-1]:
        cur = cur[int(p)] if isinstance(cur, list) else cur[p]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


# ---------------------------------------------------------------- テキスト
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _split(text: str, mode: str):
    """テキストを (区切りを保った断片リスト, [(キー, 翻訳対象文字列)]) に分ける。"""
    if mode == "lines":
        pieces = re.split(r"(\r?\n)", text)
    else:
        pieces = re.split(r"((?:\r?\n)[ \t]*(?:\r?\n)+)", text)   # 空行で段落分け (区切りは保持)
    entries, in_fence = [], False
    for i, piece in enumerate(pieces):
        if i % 2 == 1 or not piece.strip():
            continue
        if mode == "blocks":
            fences = sum(1 for l in piece.splitlines() if FENCE_RE.match(l))
            starts_fence = FENCE_RE.match(piece) is not None
            if in_fence or starts_fence:            # コードブロックは翻訳しない
                if fences % 2 == 1:
                    in_fence = not in_fence
                continue
        prefix = "L" if mode == "lines" else "B"
        entries.append((f"{prefix}{i:05d}", piece))
    return pieces, entries


# ---------------------------------------------------------------- 抽出・再構築
def extract(text: str, mode: str, keys: list[str] | None = None) -> dict[str, str]:
    """翻訳エントリ {キー: 原文} を取り出す。"""
    if mode == "json":
        data = json.loads(text)
        return {p: v for p, v in _walk(data, "", None, keys or DEFAULT_JSON_KEYS) if is_natural(v)}
    return {k: v for k, v in _split(text, mode)[1] if is_natural(v)}


def rebuild(text: str, mode: str, translations: dict[str, str], keys: list[str] | None = None) -> str:
    """原文ファイルに訳を当てはめた内容を返す (訳がないキーは原文のまま)。"""
    if mode == "json":
        data = json.loads(text)
        for ptr, v in translations.items():
            try:
                _set(data, ptr, v)
            except (KeyError, IndexError, ValueError, TypeError):
                continue
        return json.dumps(data, ensure_ascii=False, indent=2)
    pieces, _ = _split(text, mode)
    for k, v in translations.items():
        i = int(k[1:])
        if 0 <= i < len(pieces) and i % 2 == 0:
            # 段落の前後の空白・インデントは原文のまま残す
            src = pieces[i]
            lead = src[:len(src) - len(src.lstrip())]
            trail = src[len(src.rstrip()):]
            pieces[i] = lead + v.strip() + trail
    return "".join(pieces)


def read_source(jar_path, path: str) -> str:
    with zipfile.ZipFile(jar_path) as zf:
        return _read(zf, path)


# ---------------------------------------------------------------- 補助モデル
HELPER_CLASSIFY_PROMPT = """# タスク
Minecraft MOD の jar に含まれる **lang 以外のファイル** について、日本語化のために翻訳すべきか判定してください。

# 判定基準
- **translate = true**: プレイヤーが読む文章 (ガイドブック・マニュアル・Patchouli の本・説明文・ヒント・スプラッシュ等) を含み、リソースパックで差し替えれば表示が日本語になるもの。
- **translate = false**: 設定・内部データ・ID の一覧・コード・ログ用・開発者向け・クレジットの人名だけ、など。
- `overridable` が false のファイル (data/ 以下) はリソースパックで変更できないので必ず false。
- JSON の場合は、プレイヤーに表示される文章が入っている **キー名** を `keys` に挙げる (例: "name", "text", "title", "description")。ID・型名・パス・色などのキーは含めない。

# 出力形式
`{"files": [{"path": "入力と同じパス", "translate": true, "reason": "日本語で短く", "keys": ["text"]}]}` の JSON のみ。
入力の全ファイルについて 1 件ずつ返す。
"""


def rule_decision(c: dict) -> dict:
    """補助モデルがない場合の判定。"""
    locale = c.get("locale", has_locale(c["path"]))
    ok = c["overridable"] and (locale or c["mode"] != "json")
    # 判定方法 (ルール / 補助モデル) は画面のタグで示すので、理由には書かない
    reason = ("言語別フォルダの文章" if locale else "文章を含むファイル") if ok else \
        ("data/ はリソースパックで変更できません" if not c["overridable"] else "言語の指定がない JSON")
    return {"translate": ok, "reason": reason, "keys": DEFAULT_JSON_KEYS if c["mode"] == "json" else []}


def apply_helper_decision(c: dict, d: dict | None) -> dict:
    """補助モデルの判定を検証して採用する。出力先パスは常にルールで決めたものを使う。"""
    if not isinstance(d, dict):
        return rule_decision(c)
    keys = [k for k in (d.get("keys") or []) if isinstance(k, str)] if c["mode"] == "json" else []
    if c["mode"] == "json":
        try:
            if not extract(c.get("_text", c["snippet"]), "json", keys or None):
                keys = DEFAULT_JSON_KEYS
        except json.JSONDecodeError:
            keys = keys or DEFAULT_JSON_KEYS
    return {"translate": bool(d.get("translate")) and c["overridable"],
            "reason": str(d.get("reason") or "")[:200] or "理由の記載なし",
            "keys": keys or (DEFAULT_JSON_KEYS if c["mode"] == "json" else [])}
