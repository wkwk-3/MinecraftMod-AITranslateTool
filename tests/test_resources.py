"""lang 以外の追加リソース (Patchouli の本・マニュアル・テキスト) の探索と翻訳。"""
import io
import json
import zipfile

import pytest

from mctrans import app as A, jobs, resources as res
from test_app import client, configure, jar_bytes, upload, wait_job  # noqa: F401

INTRO = {"name": "Introduction", "category": "res:basics", "icon": "minecraft:book",
         "pages": [{"type": "patchouli:text", "text": "Welcome to $(item)Res$(), %s players.$(br)Enjoy the magic."},
                   {"type": "patchouli:spotlight", "item": "res:wand", "title": "The Wand", "text": "Use the wand wisely."}]}
MANUAL = "# Getting Started\n\nPlace the altar on stone.\n\n```\n/give @p res:wand\n\nmore code\n```\n\n| Item | Use |\n|---|---|\n| Wand | Cast spells |\n"
RES_MOD = jar_bytes({
    "fabric.mod.json": {"id": "res", "name": "Res Mod"},
    "assets/res/lang/en_us.json": {"item.res.wand": "Wand"},
    "assets/res/patchouli_books/guide/en_us/entries/intro.json": INTRO,
    "assets/res/patchouli_books/guide/en_us/categories/basics.json":
        {"name": "Basics", "description": "The basics of the mod.", "icon": "minecraft:book"},
    "assets/res/patchouli_books/guide/en_us/entries/already.json": {"name": "Already translated"},
    "assets/res/patchouli_books/guide/ja_jp/entries/already.json": {"name": "翻訳済み"},
    "assets/res/manual/en_us/start.md": MANUAL,
    "assets/res/texts/splashes.txt": "Now with magic!\nAlso try Res!\n",
    "assets/res/texts/credits.txt": "Made by Some Person and Another Person\n",
    "assets/res/tables/ids.json": {"ids": ["res:a", "res:b"]},
    "assets/res/models/item/wand.json": {"parent": "item/generated", "name": "Not a candidate here"},
    "data/res/patchouli_books/guide/en_us/entries/data_entry.json": {"name": "Data entry", "pages": [{"text": "Some text here"}]},
})
OTHER_SAME_TARGET = jar_bytes({
    "fabric.mod.json": {"id": "other", "name": "Other Mod"},
    "assets/res/texts/splashes.txt": "Different splash text!\nAnother line here\n",
})


# ---------------------------------------------------------------- 単体
@pytest.mark.parametrize("path,expected", [
    ("assets/x/patchouli_books/b/en_us/entries/a.json", "assets/x/patchouli_books/b/ja_jp/entries/a.json"),
    ("assets/x/manual/en_US/a.md", "assets/x/manual/ja_jp/a.md"),
    ("assets/x/guide/en_us.json", "assets/x/guide/ja_jp.json"),
    ("assets/x/docs/guide_en_us.md", "assets/x/docs/guide_ja_jp.md"),
    ("assets/x/texts/splashes.txt", "assets/x/texts/splashes.txt"),     # 言語なし → 上書き
    ("assets/x/lens/menu.json", "assets/x/lens/menu.json"),             # "en" を含む単語を誤検出しない
    ("data/x/patchouli_books/b/en_us/a.json", None),                     # data/ は不可
])
def test_target_path(path, expected):
    assert res.target_path(path) == expected


def test_json_extract_and_rebuild_only_text_keys():
    text = json.dumps(INTRO)
    e = res.extract(text, "json", ["name", "title", "text", "type"])
    assert e == {"/name": "Introduction", "/pages/0/text": INTRO["pages"][0]["text"],
                 "/pages/1/title": "The Wand", "/pages/1/text": "Use the wand wisely."}   # type/ID は自然文でないので除外
    out = json.loads(res.rebuild(text, "json", {"/name": "はじめに", "/pages/1/text": "杖は賢く使おう。"}, ["name"]))
    assert out["name"] == "はじめに" and out["pages"][1]["text"] == "杖は賢く使おう。"
    assert out["pages"][0]["type"] == "patchouli:text" and out["category"] == "res:basics"
    assert out["pages"][0]["text"] == INTRO["pages"][0]["text"]                         # 未訳は原文


def test_json_pointer_escaping():
    text = json.dumps({"a/b": {"text": "Slash key text"}, "t~x": {"text": "Tilde key text"}})
    e = res.extract(text, "json", ["text"])
    out = json.loads(res.rebuild(text, "json", {k: "訳" for k in e}, ["text"]))
    assert out == {"a/b": {"text": "訳"}, "t~x": {"text": "訳"}}


def test_blocks_keep_code_and_spacing():
    e = res.extract(MANUAL, "blocks")
    assert list(e.values()) == ["# Getting Started", "Place the altar on stone.",
                                "| Item | Use |\n|---|---|\n| Wand | Cast spells |\n"]
    out = res.rebuild(MANUAL, "blocks", {k: "【" + v.strip() + "】" for k, v in e.items()})
    assert "```\n/give @p res:wand\n\nmore code\n```" in out                              # コードブロックは不変
    assert out.startswith("【# Getting Started】\n\n【Place the altar on stone.】\n\n```")
    assert out.endswith("【| Item | Use |\n|---|---|\n| Wand | Cast spells |】\n")


def test_lines_mode():
    text = "Now with magic!\n\nAlso try Res!\n"
    assert res.guess_mode("a/splashes.txt", text) == "lines"
    e = res.extract(text, "lines")
    assert list(e.values()) == ["Now with magic!", "Also try Res!"]
    assert res.rebuild(text, "lines", {k: "訳" + str(i) for i, k in enumerate(e)}) == "訳0\n\n訳1\n"


def test_scan_candidates(tmp_path):
    p = tmp_path / "r.jar"
    p.write_bytes(RES_MOD)
    c = {x["path"]: x for x in res.scan_candidates(p)}
    assert set(c) == {
        "assets/res/patchouli_books/guide/en_us/entries/intro.json",
        "assets/res/patchouli_books/guide/en_us/categories/basics.json",
        "assets/res/manual/en_us/start.md", "assets/res/texts/splashes.txt", "assets/res/texts/credits.txt",
        "data/res/patchouli_books/guide/en_us/entries/data_entry.json"}
    # lang / models / ID だけの JSON / 日本語版が同梱済みのものは候補外
    d = c["data/res/patchouli_books/guide/en_us/entries/data_entry.json"]
    assert not d["overridable"] and d["target"] is None
    assert c["assets/res/texts/splashes.txt"]["mode"] == "lines" and not c["assets/res/texts/splashes.txt"]["locale"]
    assert c["assets/res/manual/en_us/start.md"]["target"] == "assets/res/manual/ja_jp/start.md"


def test_apply_helper_decision_guards():
    c = {"path": "data/x/a.json", "mode": "json", "overridable": False, "snippet": "{}", "_text": '{"text":"Hello there"}'}
    assert res.apply_helper_decision(c, {"translate": True, "keys": ["text"]})["translate"] is False
    c2 = {**c, "path": "assets/x/a.json", "overridable": True}
    d = res.apply_helper_decision(c2, {"translate": True, "keys": ["nonexistent"]})
    assert d["translate"] and "text" in d["keys"]                          # 実在しないキー → 既定キーに戻す
    assert res.apply_helper_decision(c2, None) == res.rule_decision(c2)        # 判定なし → ルール判定


# ---------------------------------------------------------------- API
def scan(client, uids):
    client.post("/api/resources/scan", json={"uids": uids})
    j = wait_job(client)
    assert j["error"] is None
    return client.get("/api/resources").json()


def test_scan_api_with_helper(client, fake_llm):
    if A.RES_FILE.exists():
        A.RES_FILE.unlink()
    upload(client, ("res.jar", RES_MOD))
    configure(client, fake_llm.url, helper=True)
    data = scan(client, ["res.jar"])["res.jar"]
    assert data["helper"] and data["name"] == "Res Mod"
    files = {f["path"]: f for f in data["files"]}
    assert "assets/evil/../../x.json" not in files
    intro = files["assets/res/patchouli_books/guide/en_us/entries/intro.json"]
    assert intro["selected"] and intro["by_helper"] and intro["entries"] == 4
    assert intro["target"] == "assets/res/patchouli_books/guide/ja_jp/entries/intro.json"
    assert not files["assets/res/texts/credits.txt"]["selected"]               # 補助モデルが不要と判定
    assert not files["data/res/patchouli_books/guide/en_us/entries/data_entry.json"]["selected"]
    prompt = next(c["user"] for c in fake_llm.calls if "lang 以外のファイル" in c["system"])
    assert "## assets/res/manual/en_us/start.md" in prompt and "overridable: false" in prompt
    # 選択の変更 (変更不可のものは選べない)
    r = client.post("/api/resources/select", json={"uid": "res.jar", "paths": [
        "assets/res/manual/en_us/start.md", "data/res/patchouli_books/guide/en_us/entries/data_entry.json"]}).json()
    assert r["selected"] == 1
    assert client.get("/api/resources/selected").json() == {"res.jar": ["assets/res/manual/en_us/start.md"]}
    pv = client.get("/api/resources/preview", params={"uid": "res.jar", "path": "assets/res/manual/en_us/start.md"}).json()
    assert pv["target"] == "assets/res/manual/ja_jp/start.md" and pv["total"] == 3
    assert client.post("/api/resources/select", json={"uid": "nope.jar", "paths": []}).status_code == 404
    assert client.post("/api/resources/scan", json={"uids": []}).status_code == 400


def test_scan_without_helper_uses_rules(client, fake_llm):
    upload(client, ("res.jar", RES_MOD))
    configure(client, fake_llm.url, helper=False)
    files = {f["path"]: f for f in scan(client, ["res.jar"])["res.jar"]["files"]}
    assert not any(f["by_helper"] for f in files.values())
    assert files["assets/res/patchouli_books/guide/en_us/entries/intro.json"]["selected"]
    assert files["assets/res/texts/splashes.txt"]["selected"]
    assert not any("lang 以外のファイル" in c["system"] for c in fake_llm.calls)


def pack_of(j):
    return zipfile.ZipFile(A.OUT_DIR / j["result"])


def test_translate_resources_into_one_pack(client, fake_llm):
    upload(client, ("res.jar", RES_MOD))
    configure(client, fake_llm.url, helper=True)
    scan(client, ["res.jar"])
    sel = client.get("/api/resources/selected").json()
    client.post("/api/translate", json={"uids": [], "resources": sel})       # 探索タブからの開始 (uids 空でも OK)
    j = wait_job(client)
    assert j["error"] is None, A.S.logs[-10:]
    z = pack_of(j)
    names = set(z.namelist())
    assert {"assets/res/lang/ja_jp.json", "assets/res/patchouli_books/guide/ja_jp/entries/intro.json",
            "assets/res/patchouli_books/guide/ja_jp/categories/basics.json", "assets/res/manual/ja_jp/start.md",
            "assets/res/texts/splashes.txt", "pack.mcmeta"} <= names
    assert not any("en_us" in n for n in names) and not any(n.startswith("data/") for n in names)
    intro = json.loads(z.read("assets/res/patchouli_books/guide/ja_jp/entries/intro.json"))
    assert intro["name"] == "訳「Introduction」" and intro["category"] == "res:basics"
    assert intro["pages"][0]["type"] == "patchouli:text"
    assert intro["pages"][0]["text"] == "訳「Welcome to $(item)Res$(), %s players.$(br)Enjoy the magic.」"  # マクロ保持
    md = z.read("assets/res/manual/ja_jp/start.md").decode()
    assert "```\n/give @p res:wand\n\nmore code\n```" in md and "訳「Place the altar on stone.」" in md
    assert z.read("assets/res/texts/splashes.txt").decode() == "訳「Now with magic!」\n訳「Also try Res!」\n"
    # モデルに送ったのは自然文だけ (ID・コードは送らない)
    sent = "\n".join(c["user"].split("# 翻訳対象")[1] for c in fake_llm.calls if "# 翻訳対象" in c["user"])
    assert "res:wand" not in sent and "patchouli:text" not in sent
    job = A.JOBS.load(client.get("/api/jobs").json()[0]["id"])
    kinds = sorted(u["kind"] for u in job["units"].values())
    assert kinds.count("res") == 4 and kinds.count("lang") == 1


def test_resources_only_without_lang(client, fake_llm):
    upload(client, ("res.jar", RES_MOD))
    configure(client, fake_llm.url, helper=False)
    scan(client, ["res.jar"])
    client.post("/api/translate", json={"uids": ["res.jar"], "include_lang": False,
                                        "resources": {"res.jar": ["assets/res/manual/en_us/start.md"]}})
    z = pack_of(wait_job(client))
    files = [n for n in z.namelist() if n.startswith("assets/")]
    assert files == ["assets/res/manual/ja_jp/start.md"]


def test_same_target_from_two_mods_is_reported(client, fake_llm):
    upload(client, ("res.jar", RES_MOD), ("other.jar", OTHER_SAME_TARGET))
    configure(client, fake_llm.url, helper=False)
    scan(client, ["res.jar", "other.jar"])
    t = "assets/res/texts/splashes.txt"
    client.post("/api/translate", json={"uids": [], "include_lang": False,
                                        "resources": {"res.jar": [t], "other.jar": [t]}})
    j = wait_job(client)
    z = pack_of(j)
    assert z.namelist().count(t) == 1
    assert any("出力先が重複" in l for l in A.S.logs)
    hist = client.get(f"/api/jobs/{client.get('/api/jobs').json()[0]['id']}/history").json()
    assert any(e["event"] == "conflict" and e["target"] == t for e in hist)


def test_resource_translation_resumes(client, fake_llm, monkeypatch):
    from test_resume import Crash, crash_after, run_until_crash
    upload(client, ("res.jar", RES_MOD))
    configure(client, fake_llm.url, helper=False)
    scan(client, ["res.jar"])
    sel = {"res.jar": ["assets/res/manual/en_us/start.md", "assets/res/texts/splashes.txt"]}
    crash_after(monkeypatch, "_translate_chunk", 2)
    job = run_until_crash({"uids": ["res.jar"], "include_lang": False, "resources": sel, "chunk_items": 1})
    monkeypatch.undo()
    A.JOBS.mark_interrupted_on_startup()
    # 探索結果を消しても、計画時に固定した出力先・抽出方法で再開できる
    A.RES_FILE.unlink()
    n = len(fake_llm.calls)
    client.post(f"/api/jobs/{job['id']}/resume")
    j = wait_job(client)
    assert j["error"] is None
    z = pack_of(j)
    assert "訳「Place the altar on stone.」" in z.read("assets/res/manual/ja_jp/start.md").decode()
    assert len([c for c in fake_llm.calls[n:] if "# 翻訳対象" in c["user"]]) == 5 - 2   # 残りだけ
