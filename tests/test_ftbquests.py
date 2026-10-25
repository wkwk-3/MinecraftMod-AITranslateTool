"""FTB Quests の言語ファイル翻訳。"""
import io
import json
import zipfile

import pytest

from mctrans import app as A, ftbquests as F, translator as tr
from test_app import client, configure, jar_bytes, upload, wait_job  # noqa: F401

# 実データ (ATM10) と同じ書式: タブインデント・1 行リスト・複数行リスト・エスケープ・JSON コンポーネント
CHAPTER = r'''{
	quest.0A.title: "&aSwivel Bearings"
	quest.0A.quest_desc: ["Connect a &aSwivel Bearing&r to blocks.\\n\\nUse &bGlue&r."]
	quest.0B.quest_desc: [
		"This is &2&lMinecraft&r, say \"hi\"..."
		"{@pagebreak}"
		"But I can give you tips! Logs \\& Saplings"
		"{image:atm:textures/questpics/a.png width:150 height:150 align:center}"
		""
		"{ \"text\": \"CREATE QUESTLINE\", \"color\": \"#55FF55\", \"clickEvent\": { \"action\": \"change_page\", \"value\": \"100C477F4E63F20A\" } }"
	]
	quest.0B.title: "Copper Wire"
	task.0C.title: "Any #c:ingots"
}
'''
WHOLE = CHAPTER.replace("}\n", "\tchapter.100C477F4E63F20A.title: \"Create\"\n}\n", 1) \
    if False else CHAPTER[:-2] + '\tchapter.100C477F4E63F20A.title: "Create"\n}\n'
JA_OLD = '{\n\tchapter.100C477F4E63F20A.title: "クリエイト"\n\tquest.0B.title: "Copper Wire"\n}\n'


def ftb_zip(root="ftbquests"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(f"{root}/quests/lang/en_us.snbt", WHOLE)
        z.writestr(f"{root}/quests/lang/en_us/chapters/create.snbt_merged", CHAPTER)
        z.writestr(f"{root}/quests/lang/ja_jp.snbt", JA_OLD)
        z.writestr(f"{root}/quests/lang/recovery/en_us_123.snbt", "not snbt {")
        z.writestr(f"{root}/quests/chapters/create.snbt", "{ id: \"100C\" }")
        z.writestr(f"{root}/quests/lang/fr_fr.snbt", "{}")
    return buf.getvalue()


ITEM_MOD = jar_bytes({
    "fabric.mod.json": {"id": "aero", "name": "Aero"},
    "assets/aero/lang/en_us.json": {"block.aero.swivel_bearing": "Swivel Bearings", "item.aero.glue": "Glue",
                                   "item.aero.wire": "Copper Wire"},
    "assets/aero/lang/ja_jp.json": {"item.aero.glue": "接着剤(同梱)"},
})


def pack_zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pack.mcmeta", "{}")
        z.writestr("assets/aero/lang/ja_jp.json", json.dumps(entries, ensure_ascii=False))
    return buf.getvalue()


# ---------------------------------------------------------------- 単体
def test_snbt_roundtrip_and_escapes():
    d = F.parse_lang_snbt(CHAPTER)
    assert d["quest.0A.quest_desc"] == ["Connect a &aSwivel Bearing&r to blocks.\\n\\nUse &bGlue&r."]
    assert d["quest.0B.quest_desc"][0] == 'This is &2&lMinecraft&r, say "hi"...'
    assert d["quest.0B.quest_desc"][2].endswith("Logs \\& Saplings")
    out = F.write_lang_snbt(d)
    assert F.parse_lang_snbt(out) == d
    assert '\tquest.0A.quest_desc: ["Connect a &aSwivel Bearing&r to blocks.\\\\n\\\\nUse &bGlue&r."]' in out
    assert '\tquest.0B.quest_desc: [\n\t\t"This is &2&lMinecraft&r, say \\"hi\\"..."' in out


@pytest.mark.parametrize("bad", ["", "[]", '{ a: 1 }', '{ a: "x"', '{ a "x" }', '{ a: ["x", 2] }'])
def test_snbt_errors(bad):
    with pytest.raises(F.SnbtError):
        F.parse_lang_snbt(bad)


def test_snbt_quoted_keys_and_commas():
    assert F.parse_lang_snbt('{"a.b": "x", c: ["y", "z"],}') == {"a.b": "x", "c": ["y", "z"]}


def test_extract_and_apply():
    d = F.parse_lang_snbt(CHAPTER)
    e = F.extract_entries(d)
    assert list(e) == ["quest.0A.title", "quest.0A.quest_desc[0]", "quest.0B.quest_desc[0]",
                       "quest.0B.quest_desc[2]", "quest.0B.quest_desc[5].text", "quest.0B.title", "task.0C.title"]
    out = F.apply_entries(d, {"quest.0B.quest_desc[5].text": "クリエイトのクエスト", "quest.0B.title": "銅線"})
    comp = json.loads(out["quest.0B.quest_desc"][5])
    assert comp["text"] == "クリエイトのクエスト" and comp["clickEvent"]["value"] == "100C477F4E63F20A"
    assert out["quest.0B.quest_desc"][1] == "{@pagebreak}" and out["quest.0B.title"] == "銅線"
    assert out["quest.0A.title"] == "&aSwivel Bearings"


def test_ftb_placeholders_protected():
    s = "&aHi\\n\\nLogs \\& Saplings {@pagebreak} {image:atm:a.png width:1 height:2} %s"
    p, found = tr.protect(s)
    assert "\\n" not in p and "{image" not in p and "\\&" not in p and "&a" not in p
    assert tr.restore(p, found) == s


def test_name_match_and_paths():
    names = {"Swivel Bearings": "旋回ベアリング"}
    assert F.name_match("&aSwivel Bearings", names) == "&a旋回ベアリング"
    assert F.name_match("  &lSwivel Bearings&r", names) == "  &l旋回ベアリング&r"
    assert F.name_match("Swivel Bearings!", names) is None
    docs = F.find_documents(["x/quests/lang/en_us.snbt", "x/quests/lang/en_us/chapters/a.snbt_merged",
                             "x/quests/lang/en_us/file.snbt_merged", "x/quests/lang/ja_jp.snbt",
                             "x/quests/lang/fr_fr.snbt", "x/quests/lang/recovery/en_us_1.snbt"])
    assert [d["path"].split("lang/")[1] for d in docs] == ["en_us/chapters/a.snbt_merged", "en_us/file.snbt_merged", "en_us.snbt"]
    assert docs[-1]["out"] == "x/quests/lang/ja_jp.snbt" and docs[-1]["ja"] == "x/quests/lang/ja_jp.snbt"
    assert docs[0]["out"] == "x/quests/lang/ja_jp/chapters/a.snbt_merged" and docs[0]["ja"] is None
    assert F.normalize_out_path("x/quests/lang/ja_jp/chapters/a.snbt_merged") == \
        "config/ftbquests/quests/lang/ja_jp/chapters/a.snbt_merged"


# ---------------------------------------------------------------- API
def ftb_upload_zip(client, data=None, name="atm.zip"):
    r = client.post("/api/ftbq/upload", files=[("files", (name, data or ftb_zip(), "application/zip"))])
    assert r.status_code == 200, r.text
    return r.json()


def test_upload_zip_and_folder(client):
    m = ftb_upload_zip(client)
    assert m["name"] == "atm" and [d["label"] for d in m["docs"]] == ["create", "en_us"]
    whole = m["docs"][-1]
    assert whole["existing_ja"] == 1 and whole["entries"] == 8           # 既存訳は Create → クリエイト のみ (未翻訳は除外)
    src = A.FTBQ_DIR / m["id"] / "src"
    assert not (src / "ftbquests/quests/chapters/create.snbt").exists()    # lang 以外は取り込まない
    assert not list(src.rglob("recovery/*"))
    # フォルダ選択 (相対パス付き)
    r = client.post("/api/ftbq/upload", files=[
        ("files", ("en_us.snbt", WHOLE.encode(), "text/plain")),
        ("files", ("evil.snbt", b"{}", "text/plain"))],
        data={"paths": ["ftbquests/quests/lang/en_us.snbt", "../../evil/lang/x.snbt"]})
    assert r.status_code == 200 and len(r.json()["docs"]) == 1
    assert not (A.WORK.parent / "evil").exists()
    assert client.post("/api/ftbq/upload", files=[("files", ("x.zip", b"nope", "application/zip"))]).status_code == 400
    empty = io.BytesIO()
    zipfile.ZipFile(empty, "w").writestr("readme.txt", "x")
    assert client.post("/api/ftbq/upload", files=[("files", ("e.zip", empty.getvalue(), "application/zip"))]).status_code == 400
    assert len(client.get("/api/ftbq").json()) == 2


def test_name_dict_priority(client):
    import shutil
    shutil.rmtree(A.PACKS_DIR, ignore_errors=True)
    upload(client, ("aero.jar", ITEM_MOD))
    client.post("/api/ftbq/packs", files=[("files", ("other.zip", pack_zip({"block.aero.swivel_bearing": "スイベルベアリング",
                                                                          "item.aero.glue": "のり"}), "application/zip"))])
    (A.OUT_DIR / "ours_test.zip").write_bytes(pack_zip({"block.aero.swivel_bearing": "旋回ベアリング"}))
    try:
        names, st = A.build_name_dict([])
        assert names == {"Glue": "接着剤(同梱)"}                              # MOD 同梱の ja_jp のみ
        names, _ = A.build_name_dict([{"src": "uploaded", "name": "other.zip"}, {"src": "output", "name": "ours_test.zip"}])
        assert names == {"Swivel Bearings": "旋回ベアリング", "Glue": "のり"}    # 後に指定したパックが優先
        client.post("/api/glossary", json={"user": {"Glue": "グルー"}})
        assert A.build_name_dict([])[0]["Glue"] == "グルー"                    # ユーザー指定は最優先
        packs = client.get("/api/ftbq/packs").json()
        assert {"src": "uploaded", "name": "other.zip"} in packs and {"src": "output", "name": "ours_test.zip"} in packs
    finally:
        (A.OUT_DIR / "ours_test.zip").unlink()


def test_translate_ftbquests(client, fake_llm):
    import shutil
    shutil.rmtree(A.PACKS_DIR, ignore_errors=True)
    upload(client, ("aero.jar", ITEM_MOD))
    client.post("/api/ftbq/packs", files=[("files", ("other.zip", pack_zip({"block.aero.swivel_bearing": "旋回ベアリング",
                                                                          "item.aero.wire": "銅線"}), "application/zip"))])
    configure(client, fake_llm.url, helper=False)
    m = ftb_upload_zip(client)
    r = client.post(f"/api/ftbq/{m['id']}/translate", json={"packs": [{"src": "uploaded", "name": "other.zip"}]})
    assert r.status_code == 200
    j = wait_job(client)
    assert j["error"] is None, A.S.logs[-10:]
    assert j["result"].startswith("FTBQuests_atm_")
    z = zipfile.ZipFile(A.OUT_DIR / j["result"])
    assert sorted(n for n in z.namelist() if n.endswith(("snbt", "_merged"))) == [
        "config/ftbquests/quests/lang/ja_jp.snbt", "config/ftbquests/quests/lang/ja_jp/chapters/create.snbt_merged"]
    whole = F.parse_lang_snbt(z.read("config/ftbquests/quests/lang/ja_jp.snbt").decode())
    chap = F.parse_lang_snbt(z.read("config/ftbquests/quests/lang/ja_jp/chapters/create.snbt_merged").decode())
    # アイテム名そのもの → 辞書の訳 (書式コードは保持、AI を通さない)
    assert whole["quest.0A.title"] == chap["quest.0A.title"] == "&a旋回ベアリング"
    assert whole["quest.0B.title"] == "銅線"
    assert whole["chapter.100C477F4E63F20A.title"] == "クリエイト"           # 既存 ja_jp を再利用
    desc = whole["quest.0B.quest_desc"]
    assert desc[1] == "{@pagebreak}" and desc[3].startswith("{image:") and desc[4] == ""
    assert desc[2] == "訳「But I can give you tips! Logs \\& Saplings」"
    assert json.loads(desc[5])["text"] == "訳「CREATE QUESTLINE」" and json.loads(desc[5])["clickEvent"]["action"] == "change_page"
    assert whole["quest.0A.quest_desc"][0] == "訳「Connect a &aSwivel Bearing&r to blocks.\\n\\nUse &bGlue&r.」"
    assert chap == {k: v for k, v in whole.items() if k in chap}              # 章ファイルと全体ファイルで同じ訳
    # 章ファイルで訳した文は全体ファイルでは AI に再送しない
    sent = [c["user"].split("# 翻訳対象")[1] for c in fake_llm.calls if "# 翻訳対象" in c["user"]]
    assert sum(s.count('"quest.0A.quest_desc[0]"') for s in sent) == 1
    assert all("quest.0A.title" not in s for s in sent)
    # 文中のアイテム名は用語集として強制
    assert any("| Swivel Bearing" not in c["user"] and "# 用語集" in c["user"] for c in fake_llm.calls)
    p = next(c["user"] for c in fake_llm.calls if "Copper Wire" in c["user"] or "Swivel" in c["user"])
    assert "FTB Quests クエストブック" in p
    meta = client.get("/api/ftbq").json()[0]
    assert meta["results"][0] == j["result"]
    hist = [e for e in client.get(f"/api/jobs/{client.get('/api/jobs').json()[0]['id']}/history").json() if e["event"] == "plan"]
    assert hist[0]["by_name"] >= 4


def test_name_glossary_in_prompt(client, fake_llm):
    upload(client, ("aero.jar", ITEM_MOD))
    configure(client, fake_llm.url, helper=False)
    m = ftb_upload_zip(client)
    client.post(f"/api/ftbq/{m['id']}/translate", json={"packs": []})
    wait_job(client)
    p = next(c["user"] for c in fake_llm.calls if "quest.0A.quest_desc[0]" in c["user"])
    assert "| Glue | 接着剤(同梱) |" in p


def test_ftbq_resume(client, fake_llm, monkeypatch):
    from test_resume import crash_after
    configure(client, fake_llm.url, helper=False)
    m = ftb_upload_zip(client)
    crash_after(monkeypatch, "_translate_chunk", 1)
    job = A.JOBS.create({"kind": "ftbq", "project": m["id"], "pack_name": "FTB", "uids": [], "packs": [],
                         "use_existing_ja": True, "chunk_items": 1, "chunk_chars": 3500})
    A.S.job.update(running=True, id=job["id"])
    from test_resume import Crash
    with pytest.raises(Crash):
        A._run_job(job)
    A.S.job["running"] = False
    monkeypatch.undo()
    A.JOBS.mark_interrupted_on_startup()
    n = len(fake_llm.calls)
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 200
    j = wait_job(client)
    assert j["error"] is None and j["result"].startswith("FTBQuests_")
    resent = [c["user"] for c in fake_llm.calls[n:] if "# 翻訳対象" in c["user"]]
    first = [c["user"] for c in fake_llm.calls[:n] if "# 翻訳対象" in c["user"]][0]
    key = json.loads(first.split("```json\n")[-1].split("\n```")[0]).popitem()[0]
    assert all(key not in r.split("# 翻訳対象")[1] for r in resent)          # 訳済みは再送しない


def test_ftbq_delete_and_404(client):
    m = ftb_upload_zip(client)
    assert client.post(f"/api/ftbq/{m['id']}/delete").json()["ok"]
    assert client.post(f"/api/ftbq/{m['id']}/translate", json={}).status_code == 404
    assert client.post("/api/ftbq/..%2Fx/delete").status_code == 404
