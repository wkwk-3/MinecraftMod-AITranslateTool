import io
import json
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from mctrans import app as A


def jar_bytes(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, b in files.items():
            z.writestr(n, b if isinstance(b, str) else json.dumps(b))
    return buf.getvalue()


LIB = jar_bytes({
    "fabric.mod.json": {"id": "corelib", "name": "Core Lib", "version": "1.0"},
    "assets/corelib/lang/en_us.json": {"item.corelib.arcane_crystal": "Arcane Crystal",
                                       "corelib.msg": "Found %s crystals"},
})
MAIN = jar_bytes({
    "fabric.mod.json": {"id": "magic", "name": "Magic Mod", "version": "2.0", "description": "Spells",
                        "depends": {"corelib-api": "*", "fabricloader": "*", "ghostmod": "*"}},
    "assets/magic/lang/en_us.json": {
        "item.magic.wand": "Magic Wand",
        "item.magic.wand.tooltip": "Channel {mana} mana into an Arcane Crystal",
        "magic.dup": "Found %s crystals",          # 前提 MOD と同じ原文 → 翻訳メモリで同訳
        "magic.icon": "textures/gui/icon.png",     # スキップ対象
        "magic.prebuilt": "Already done",
        "magic.broken": "Keep [123456] safe",
        "magic.md": "| a | b |",
    },
    "assets/magic/lang/ja_jp.json": {"magic.prebuilt": "翻訳済み"},
})
OTHER = jar_bytes({"META-INF/mods.toml": '[[mods]]\nmodId="other"\ndisplayName="Other Thing"\n',
                   "assets/other/lang/en_us.lang": "item.other.x=Other X\n"})


@pytest.fixture
def client():
    A.S.mods.clear()
    for p in A.MODS_DIR.glob("*"):
        p.unlink()
    import shutil
    for p in A.JOBS.root.glob("*"):
        shutil.rmtree(p)
    A.S.cancel.clear()
    A.S.memory.tm.clear(); A.S.memory.glossary.clear(); A.S.memory.user_glossary.clear(); A.S.memory.official.clear()
    A.S.logs.clear()
    c = TestClient(A.app)
    c.cookies.set("mct_token", "test-token")
    return c


def upload(c, *items):
    return c.post("/api/upload", files=[("files", (n, b, "application/java-archive")) for n, b in items]).json()


def test_auth_required():
    c = TestClient(A.app)
    assert c.get("/api/mods").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/api/mods", headers={"x-mct-token": "wrong"}).status_code == 401
    assert c.get("/api/mods", headers={"x-mct-token": "test-token"}).status_code == 200
    # ?token= 付きなら転送せずにそのまま画面を返す (別サイトのリンクから開いても Cookie に頼らない)
    r = c.get("/?token=test-token", follow_redirects=False)
    assert r.status_code == 200 and "MC MOD Translator" in r.text
    ck = r.headers["set-cookie"].lower()
    assert "mct_token=test-token" in ck and "httponly" in ck and "samesite=lax" in ck
    assert c.get("/api/download/none.zip?token=test-token").status_code == 404   # ダウンロードは URL のトークンでも可
    assert "set-cookie" not in c.get("/api/mods?token=test-token").headers        # API では Cookie を発行しない
    assert TestClient(A.app).get("/?token=bad", follow_redirects=False).status_code == 401


def test_upload_search_delete(client):
    r = upload(client, ("lib.jar", LIB), ("Magic Mod.zip", MAIN), ("../../evil.jar", OTHER),
               ("notes.txt", b"x"), ("corrupt.jar", b"not a zip"))
    assert sorted(r["added"]) == ["Magic Mod.jar", "evil.jar", "lib.jar"]
    assert not (A.WORK / "evil.jar").exists() and (A.MODS_DIR / "evil.jar").exists()   # パストラバーサル防止
    assert not (A.MODS_DIR / "corrupt.jar").exists()
    names = lambda q: [m["name"] for m in client.get("/api/mods", params={"q": q}).json()]
    assert names("") == ["Core Lib", "Magic Mod", "Other Thing"]
    assert names("magic") == ["Magic Mod"]
    assert names("FORGE") == ["Other Thing"]
    assert names("fabric core") == ["Core Lib"]       # AND 検索
    assert names("spells") == ["Magic Mod"]           # 説明文も対象
    assert names("zzz") == []
    client.post("/api/mods/delete", json={"uids": ["evil.jar", "missing.jar"]})
    assert names("") == ["Core Lib", "Magic Mod"] and not (A.MODS_DIR / "evil.jar").exists()


def test_analyze(client):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN))
    r = client.post("/api/analyze", json={"uids": ["main.jar"]}).json()
    # corelib-api は ID 不一致なのでルールベースでは未解決 (補助モデルで解決するケースは e2e で確認)
    assert r["order"] == ["main.jar"] and r["missing"] == ["corelib-api", "ghostmod"]


def test_glossary_roundtrip(client):
    client.post("/api/glossary", json={"user": {" Mana ": " マナ ", "": "x"}})
    g = client.get("/api/glossary").json()
    assert g["user"] == {"Mana": "マナ"} and g["base"]["Pickaxe"] == "ツルハシ"
    client.post("/api/memory/clear")
    assert client.get("/api/glossary").json()["user"] == {"Mana": "マナ"}  # ユーザー用語は残す


def test_status_and_download_404(client):
    s = client.get("/api/status").json()
    assert set(s) >= {"job", "logs", "gpu", "models", "outputs"}
    assert client.get("/api/download/nope.zip").status_code == 404
    assert client.get("/api/download/..%2F..%2Fmemory.json").status_code == 404


def wait_job(c, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get("/api/status").json()["job"]
        if not j["running"]:
            return j
        time.sleep(0.1)
    raise TimeoutError


def configure(c, url, helper=True):
    # 偽の LLM は汎用形式の依頼を解釈するので、テストでは generic を使う
    c.post("/api/models/config", json={"role": "translator", "url": url, "prompt_style": "generic"})
    c.post("/api/models/config", json={"role": "helper", "url": url if helper else "", "repo": ""})


def test_end_to_end_translation(client, fake_llm):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN), ("other.jar", OTHER))
    configure(client, fake_llm.url)
    fake_llm.break_once.add("item.magic.wand.tooltip")   # 1 回目は {mana} を落とす → 再翻訳
    fake_llm.always_break.add("magic.broken")            # 常に [123456] を落とす → 原文維持
    client.post("/api/glossary", json={"user": {"Wand": "杖"}})
    r = client.post("/api/translate", json={"uids": ["main.jar"], "pack_format": 48, "pack_name": "テスト/パック"})
    assert r.status_code == 200
    j = wait_job(client)
    assert j["error"] is None, client.get("/api/status").json()["logs"]
    assert j["result"].startswith("テスト_パック_") and j["result"].endswith(".zip")

    z = zipfile.ZipFile(A.OUT_DIR / j["result"])
    assert json.loads(z.read("pack.mcmeta"))["pack"]["pack_format"] == 48
    lib = json.loads(z.read("assets/corelib/lang/ja_jp.json"))
    magic = json.loads(z.read("assets/magic/lang/ja_jp.json"))
    assert "assets/other/lang/ja_jp.json" not in z.namelist()      # 選択外は含めない

    assert lib["corelib.msg"] == "訳「Found %s crystals」"
    assert magic["magic.dup"] == lib["corelib.msg"]                # 翻訳メモリ再利用 (表記揺れなし)
    assert magic["item.magic.wand.tooltip"] == "訳「Channel {mana} mana into an Arcane Crystal」"
    assert magic["magic.broken"] == "Keep [123456] safe"           # 検証失敗 → 原文
    assert magic["magic.icon"] == "textures/gui/icon.png"
    assert magic["magic.prebuilt"] == "翻訳済み"                    # 内蔵 ja_jp を優先
    assert magic["magic.md"] == "訳「| a | b |」"
    assert list(magic) == list(json.loads(MAIN_LANG))              # キー順を保持

    users = [c["user"] for c in fake_llm.calls if "# 翻訳対象" in c["user"]]
    # 前提 MOD が先に翻訳される
    assert "corelib" in users[0] and "magic" not in users[0].split("# 翻訳対象")[1]
    # 同じ MOD のキーは 1 回にまとめて送る
    first_magic = next(u for u in users if "item.magic.wand" in u)
    assert "magic.md" in first_magic and "magic.broken" in first_magic
    # 用語集が適用されている (ユーザー指定 + 補助モデル抽出 + 前提 MOD の名前)
    assert "| Wand | 杖 |" in first_magic
    assert "| Arcane Crystal |" in first_magic
    # 直前の翻訳が渡されている (再翻訳時)
    assert any("# 直前の翻訳" in u for u in users)
    # 生のプレースホルダはモデルに渡らない
    assert all("{mana}" not in u.split("# 翻訳対象")[1] for u in users)
    # 補助モデルで corelib-api → lib.jar を解決
    assert any("依存 corelib-api → lib.jar" in l for l in client.get("/api/status").json()["logs"])

    # 2 回目: 翻訳メモリから再利用 → 翻訳モデルには検証失敗した項目だけ送る
    n = len(fake_llm.calls)
    client.post("/api/translate", json={"uids": ["main.jar"]})
    assert wait_job(client)["error"] is None
    again = [c["user"].split("# 翻訳対象")[1] for c in fake_llm.calls[n:] if "# 翻訳対象" in c["user"]]
    assert again and all("magic.broken" in u and "item.magic.wand" not in u for u in again)


MAIN_LANG = zipfile.ZipFile(io.BytesIO(MAIN)).read("assets/magic/lang/en_us.json")


def test_without_deps_and_helper(client, fake_llm):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN), ("other.jar", OTHER))
    configure(client, fake_llm.url, helper=False)
    client.post("/api/translate", json={"uids": ["other.jar"], "include_deps": False, "use_existing_ja": False})
    j = wait_job(client)
    assert j["error"] is None
    z = zipfile.ZipFile(A.OUT_DIR / j["result"])
    assert z.read("assets/other/lang/ja_JP.lang").decode() == "item.other.x=訳「Other X」"
    assert not any("用語集の候補" in c["system"] for c in fake_llm.calls)


def test_garbage_response_recovers(client, fake_llm):
    upload(client, ("lib.jar", LIB))
    configure(client, fake_llm.url, helper=False)
    fake_llm.garbage_calls = 1
    client.post("/api/translate", json={"uids": ["lib.jar"]})
    j = wait_job(client)
    z = zipfile.ZipFile(A.OUT_DIR / j["result"])
    assert json.loads(z.read("assets/corelib/lang/ja_jp.json"))["corelib.msg"] == "訳「Found %s crystals」"


def test_cancel_and_conflict(client, fake_llm):
    upload(client, ("lib.jar", LIB))
    configure(client, "http://127.0.0.1:9", helper=False)   # 接続できない → エラーで終了
    client.post("/api/translate", json={"uids": ["lib.jar"]})
    j = wait_job(client)
    assert j["phase"] in ("完了", "失敗")
    A.S.job["running"] = True
    try:
        assert client.post("/api/translate", json={"uids": ["lib.jar"]}).status_code == 409
    finally:
        A.S.job["running"] = False


def test_model_without_binary_fails_cleanly(client, monkeypatch):
    upload(client, ("lib.jar", LIB))
    monkeypatch.setattr(A.llm, "list_gguf", lambda repo: [{"file": "m-Q4_K_M.gguf", "size": 1}])
    monkeypatch.setattr(A.llm, "download", lambda *a, **k: "/nonexistent.gguf")
    monkeypatch.setattr(A.llm, "find_server_binary", lambda: None)
    client.post("/api/models/config", json={"role": "translator", "repo": "IndexTeam/Index-Translate-2B-GGUF"})
    client.post("/api/models/config", json={"role": "helper", "repo": ""})
    client.post("/api/translate", json={"uids": ["lib.jar"]})
    j = wait_job(client)
    assert j["phase"] == "失敗" and "翻訳モデルを起動できませんでした" in j["error"]
    assert any("llama-server が見つかりません" in l for l in client.get("/api/status").json()["logs"])


def test_security_headers_and_cookie():
    c = TestClient(A.app)
    for p in ("/docs", "/redoc", "/openapi.json"):
        c.cookies.set("mct_token", "test-token")
        assert c.get(p).status_code == 404                     # API ドキュメントは無効
    r = TestClient(A.app).get("/api/status")
    assert r.status_code == 401 and r.headers["x-frame-options"] == "DENY"
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["cache-control"] == "no-store"
    r = TestClient(A.app).get("/?token=test-token", follow_redirects=False, headers={"x-forwarded-proto": "https"})
    assert "secure" in r.headers["set-cookie"].lower() and r.status_code == 200
    r = TestClient(A.app).get("/?token=test-token", follow_redirects=False)
    assert "secure" not in r.headers["set-cookie"].lower()      # ローカル (http) では Secure を付けない
