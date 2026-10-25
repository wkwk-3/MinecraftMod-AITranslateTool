"""同梱 ja_jp からの用語集作成 / URL からのモデル追加。"""
import io
import json
import time
import zipfile

import pytest

from mctrans import app as A, llm, translator as tr
from test_app import LIB, client, configure, jar_bytes, upload, wait_job  # noqa: F401

JA_MOD = jar_bytes({
    "fabric.mod.json": {"id": "arcane", "name": "Arcane Arts", "depends": {"corelib": "*"}},
    "assets/arcane/lang/en_us.json": {
        "item.arcane.dust": "Arcane Dust",
        "block.arcane.altar": "Arcane Altar",
        "arcane.tip": "Grind Arcane Dust at the altar",
        "arcane.count": "Holds %s dust",
        "arcane.new": "Brand new line with Arcane Dust",
    },
    "assets/arcane/lang/ja_jp.json": {
        "item.arcane.dust": "秘術の粉",
        "block.arcane.altar": "秘術の祭壇",
        "arcane.tip": "祭壇で秘術の粉を挽く",
        "arcane.count": "%s 個の粉を保持",
    },
})


# ---------------------------------------------------------------- 用語集 (単体)
def test_existing_pairs_and_rule_glossary():
    lang = {"entries": {"item.a.b": "Fire Rod", "x": "Same", "y": "%s", "z": "Text", "item.a.c": "Line"},
            "existing_ja": {"item.a.b": "炎の杖", "x": "Same", "y": "%s", "z": " ", "item.a.c": "改\n行"}}
    pairs = tr.existing_pairs(lang)
    assert [p[0] for p in pairs] == ["item.a.b", "item.a.c"]          # 未翻訳・空・英字なしは除外
    assert tr.rule_glossary(pairs) == {"Fire Rod": "炎の杖"}            # 改行入りは用語にしない


def test_verify_terms_rejects_hallucination():
    pairs = [("k", "Grind Arcane Dust at the altar", "祭壇で秘術の粉を挽く")]
    got = tr.verify_terms({"Arcane Dust": "秘術の粉", "altar": "祭壇", "Dragon": "ドラゴン",
                           "Dust": "ほこり", "": "x", "Grind": 3}, pairs)
    assert got == {"Arcane Dust": "秘術の粉", "altar": "祭壇"}


def test_official_priority(tmp_path):
    m = tr.Memory(tmp_path / "m.json")
    m.glossary["Altar"] = "祭壇(推測)"
    m.official["Altar"] = "秘術の祭壇"
    assert m.all_terms()["Altar"] == "秘術の祭壇"
    m.user_glossary["Altar"] = "アルター"
    assert m.all_terms()["Altar"] == "アルター"
    m.save()
    assert tr.Memory(tmp_path / "m.json").official == {"Altar": "秘術の祭壇"}


# ---------------------------------------------------------------- 用語集 (API / ジョブ)
def test_glossary_from_ja_endpoint(client, fake_llm):
    upload(client, ("arcane.jar", JA_MOD), ("lib.jar", LIB))
    configure(client, fake_llm.url, helper=True)
    r = client.post("/api/glossary/from_ja", json={"uids": ["arcane.jar"]}).json()
    assert r["mods"] == 1                                              # ja_jp を含むのは arcane だけ
    wait_job(client)
    g = client.get("/api/glossary").json()
    assert g["official"] == {"Arcane Dust": "秘術の粉", "Arcane Altar": "秘術の祭壇"}   # 捏造 Dragon は不採用
    assert A.S.memory.tm["Grind Arcane Dust at the altar"] == "祭壇で秘術の粉を挽く"
    prompt = next(c["user"] for c in fake_llm.calls if "公式の英日対訳" in c["system"])
    assert "| Grind Arcane Dust at the altar | 祭壇で秘術の粉を挽く |" in prompt
    assert client.post("/api/glossary/from_ja", json={"uids": ["lib.jar"]}).json()["mods"] == 0


def test_glossary_from_ja_without_helper(client, fake_llm):
    upload(client, ("arcane.jar", JA_MOD))
    configure(client, fake_llm.url, helper=False)
    client.post("/api/glossary/from_ja", json={"uids": ["arcane.jar"]})
    wait_job(client)
    assert client.get("/api/glossary").json()["official"] == {"Arcane Dust": "秘術の粉", "Arcane Altar": "秘術の祭壇"}
    assert not [c for c in fake_llm.calls if "公式の英日対訳" in c["system"]]


def test_translation_job_uses_ja_glossary(client, fake_llm):
    upload(client, ("arcane.jar", JA_MOD), ("lib.jar", LIB))
    configure(client, fake_llm.url, helper=True)
    client.post("/api/glossary", json={"user": {"Arcane Altar": "アルケイン祭壇"}})
    client.post("/api/translate", json={"uids": ["arcane.jar"]})
    j = wait_job(client)
    assert j["error"] is None
    g = client.get("/api/glossary").json()
    assert g["official"]["Arcane Dust"] == "秘術の粉" and "Arcane Altar" not in g["official"]  # ユーザー指定を上書きしない
    sent = [c["user"] for c in fake_llm.calls if "# 翻訳対象" in c["user"]]
    new_line = next(u for u in sent if "arcane.new" in u)
    assert "| Arcane Dust | 秘術の粉 |" in new_line                       # 公式訳の用語が翻訳プロンプトに入る
    # 用語抽出 (推測) には ja_jp で訳済みの名前を送らない
    terms_prompt = [c["user"] for c in fake_llm.calls if "用語集の候補" in c["system"]]
    assert all("Arcane Dust" not in t for t in terms_prompt)
    pack = zipfile.ZipFile(A.OUT_DIR / j["result"])
    ja = json.loads(pack.read("assets/arcane/lang/ja_jp.json"))
    assert ja["item.arcane.dust"] == "秘術の粉" and ja["arcane.new"].startswith("訳「")
    hist = [e for e in client.get(f"/api/jobs/{client.get('/api/jobs').json()[0]['id']}/history").json()
            if e["event"] == "helper"]
    assert any(e["uid"] == "arcane.jar" and e["ja_terms"] == 2 for e in hist)


# ---------------------------------------------------------------- URL からのモデル
@pytest.mark.parametrize("url,expected", [
    ("https://huggingface.co/IndexTeam/Index-Translate-9B-GGUF/resolve/main/Index-Translate-9B.Q8_0.gguf?download=true",
     {"kind": "hf", "repo": "IndexTeam/Index-Translate-9B-GGUF", "revision": "main", "file": "Index-Translate-9B.Q8_0.gguf"}),
    ("https://huggingface.co/a/b/blob/v1.0/sub%20dir/m.gguf",
     {"kind": "hf", "repo": "a/b", "revision": "v1.0", "file": "sub dir/m.gguf"}),
    ("https://hf.co/a/b/resolve/main/x-00001-of-00002.gguf",
     {"kind": "hf", "repo": "a/b", "revision": "main", "file": "x-00001-of-00002.gguf"}),
    ("https://example.com/models/My%20Model.Q4.gguf?sig=1",
     {"kind": "http", "url": "https://example.com/models/My%20Model.Q4.gguf?sig=1", "file": "My_Model.Q4.gguf"}),
])
def test_parse_model_url(url, expected):
    assert llm.parse_model_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://example.com/a.gguf", "https://example.com/a.bin", "ftp://x/a.gguf", "not a url",
    "https://huggingface.co/a/b/resolve/main/../../etc.gguf",
])
def test_parse_model_url_rejects(url):
    with pytest.raises(ValueError):
        llm.parse_model_url(url)


def wait_dl(client, timeout=20):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ds = client.get("/api/status").json()["downloads"]
        if ds and all(d["status"] != "downloading" for d in ds):
            return ds
        time.sleep(0.05)
    raise TimeoutError


def test_add_hf_url_selects_model(client, monkeypatch):
    A.S.downloads.clear()
    A.S.custom_presets = {"translator": [], "helper": []}
    calls = []
    monkeypatch.setattr("huggingface_hub.hf_hub_download",
                        lambda repo, f, revision, cache_dir: calls.append((repo, f, revision)) or "/cache/x.gguf")
    url = "https://huggingface.co/IndexTeam/Index-Translate-9B-GGUF/resolve/main/Index-Translate-9B.Q8_0.gguf?download=true"
    r = client.post("/api/models/from_url", json={"url": url})
    assert r.status_code == 200
    d = wait_dl(client)[0]
    assert d["status"] == "done", d
    assert calls == [("IndexTeam/Index-Translate-9B-GGUF", "Index-Translate-9B.Q8_0.gguf", "main")]
    p = client.get("/api/models/presets").json()
    first = p["presets"]["translator"][0]
    assert first["label"] == "Index-Translate-9B.Q8_0.gguf (URL)" and first["src"] == url
    assert p["presets"]["translator"][1]["repo"] == llm.DEFAULT_TRANSLATE_REPO          # 既存候補も残る
    cfg = p["config"]["translator"]
    assert (cfg["repo"], cfg["file"], cfg["path"]) == ("IndexTeam/Index-Translate-9B-GGUF", "Index-Translate-9B.Q8_0.gguf", "")
    saved = json.loads((A.WORK / "settings.json").read_text("utf-8"))
    assert saved["custom_presets"]["translator"][0]["src"] == url and saved["models"]["translator"]["file"].endswith("Q8_0.gguf")
    # 同じ URL をもう一度 → 候補は重複しない
    client.post("/api/models/from_url", json={"url": url})
    wait_dl(client)
    assert sum(x.get("src") == url for x in client.get("/api/models/presets").json()["presets"]["translator"]) == 1
    client.post("/api/models/custom/delete", json={"role": "translator", "src": url})
    assert all(x.get("src") != url for x in client.get("/api/models/presets").json()["presets"]["translator"])


def test_add_direct_url_with_resume(client, fake_llm, monkeypatch):
    A.S.downloads.clear()
    A.S.custom_presets = {"translator": [], "helper": []}
    data = b"GGUF" + bytes(range(256)) * 400
    fake_llm.files["m.gguf"] = data
    url = fake_llm.url.replace("http://", "https://") + "/files/m.gguf"
    # テスト用: https → ローカルの http サーバーに向ける
    real_get = llm.requests.get
    monkeypatch.setattr(llm.requests, "get", lambda u, **k: real_get(u.replace("https://", "http://"), **k))
    part = A.CACHE_DIR / "url_models" / "m.gguf.part"
    final = A.CACHE_DIR / "url_models" / "m.gguf"
    final.unlink(missing_ok=True)
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(data[:1000])                     # 前回途中で切れた状態
    client.post("/api/models/from_url", json={"url": url, "role": "helper"})
    d = wait_dl(client)[0]
    assert d["status"] == "done" and d["done"] == len(data) and d["total"] == len(data)
    assert final.read_bytes() == data and not part.exists()
    assert fake_llm.file_requests[-1] == ("/files/m.gguf", 1000)        # 続きから取得
    cfg = client.get("/api/models/presets").json()["config"]["helper"]
    assert cfg["path"] == str(final) and cfg["repo"] == ""
    # ローカルパスのモデルは HF を使わずに起動に回る
    started = {}
    monkeypatch.setattr(A.llm, "find_server_binary", lambda: "llama-server")
    monkeypatch.setattr(A.llm, "memory_budget", lambda: (0, 0))
    monkeypatch.setattr(A.llm, "auto_cpu_moe", lambda p, v: 0)
    monkeypatch.setattr(A.S.servers["helper"], "start", lambda path, **k: started.update(path=path))
    assert A._ensure_model("helper") and started["path"] == str(final)


def test_no_download_without_server_binary(client, monkeypatch):
    calls = []
    monkeypatch.setattr(A.llm, "find_server_binary", lambda: None)
    monkeypatch.setattr(A.llm, "download", lambda *a, **k: calls.append(a) or "/x.gguf")
    monkeypatch.setattr(A.llm, "list_gguf", lambda repo: calls.append(repo) or [])
    A.S.model_cfg["helper"].update(repo="Qwen/Qwen2.5-3B-Instruct-GGUF", file="", path="", url="")
    A.S.servers["helper"].stop()
    assert A._ensure_model("helper") is False
    assert calls == []                                                        # 一覧取得もダウンロードもしない
    assert any("llama-server が見つかりません" in l for l in A.S.logs)


def test_add_url_errors(client, monkeypatch):
    monkeypatch.setattr(A.llm, "find_server_binary", lambda: "llama-server")
    A.S.downloads.clear()
    assert client.post("/api/models/from_url", json={"url": "https://x.com/a.txt"}).status_code == 400
    assert client.post("/api/models/from_url", json={"url": "https://x.com/a.gguf", "role": "bad"}).status_code == 400
    A.S.model_cfg["translator"].update(repo="", path="/nope/missing.gguf", url="")
    assert A._ensure_model("translator") is False
    assert any("モデルファイルがありません" in l for l in A.S.logs)
