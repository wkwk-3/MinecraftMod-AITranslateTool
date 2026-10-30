"""翻訳のまとめ方: Mod ごと / Mod に関係なく N 行ずつ。"""
import json
import re
import zipfile

import pytest

from mctrans import app as A, jobs
from test_app import LIB, MAIN, client, configure, jar_bytes, upload, wait_job  # noqa: F401


@pytest.fixture(autouse=True)
def reset_batch():
    A.S.batch_cfg.update(mode="per_mod", lines=40)
    yield
    A.S.batch_cfg.update(mode="per_mod", lines=40)


def sent_keys(fake, start=0):
    """偽 LLM に送られた翻訳依頼ごとのキー一覧。"""
    out = []
    for c in fake.calls[start:]:
        if "# 翻訳対象" in c["user"]:
            body = c["user"].split("# 翻訳対象")[1]
            out.append(list(json.loads(re.findall(r"```json\n(.*?)\n```", body, re.S)[-1])))
    return out


def pack(j, ns):
    return json.loads(zipfile.ZipFile(A.OUT_DIR / j["result"]).read(f"assets/{ns}/lang/ja_jp.json"))


def test_batch_api_validation(client):
    assert client.get("/api/batch").json() == {"mode": "per_mod", "lines": 40}
    for bad in ({"lines": 0}, {"lines": 1001}, {"lines": "5"}, {"lines": 2.5}, {"lines": True}, {"mode": "x"}):
        assert client.post("/api/batch", json=bad).status_code == 400, bad
    assert client.post("/api/batch", json={"mode": "mixed", "lines": 1000}).json() == {"mode": "mixed", "lines": 1000}
    assert client.post("/api/batch", json={"lines": 1}).json()["lines"] == 1
    saved = json.loads((A.WORK / "settings.json").read_text("utf-8"))
    assert saved["batch"] == {"mode": "mixed", "lines": 1}
    assert A.State().batch_cfg == {"mode": "mixed", "lines": 1}               # 再起動後も保持


def test_mixed_mode_crosses_mods(client, fake_llm):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=False)
    client.post("/api/batch", json={"mode": "mixed", "lines": 4})
    client.post("/api/translate", json={"uids": ["lib.jar", "main.jar"], "use_existing_ja": False})
    j = wait_job(client)
    assert j["error"] is None, A.S.logs[-10:]
    batches = sent_keys(fake_llm)
    assert batches[0] == ["item.corelib.arcane_crystal", "corelib.msg", "item.magic.wand",
                          "item.magic.wand.tooltip"]                         # 2 つの Mod が 1 回の依頼に入る
    assert all(len(b) <= 4 for b in batches)
    assert any("複数の Mod (Core Lib, Magic Mod)" in c["user"] for c in fake_llm.calls)
    assert pack(j, "corelib")["corelib.msg"] == "訳「Found %s crystals」"
    assert pack(j, "magic")["item.magic.wand"] == "訳「Magic Wand」"
    assert any("Mod に関係なく 4 行ずつ" in l for l in A.S.logs)
    job = A.JOBS.load(client.get("/api/jobs").json()[0]["id"])
    assert all(u["status"] == "done" for u in job["units"].values())


def test_same_key_in_two_mods(client, fake_llm):
    a = jar_bytes({"fabric.mod.json": {"id": "a", "name": "A"}, "assets/a/lang/en_us.json": {"gui.title": "Alpha Menu"}})
    b = jar_bytes({"fabric.mod.json": {"id": "b", "name": "B"}, "assets/b/lang/en_us.json": {"gui.title": "Beta Menu"}})
    upload(client, ("a.jar", a), ("b.jar", b))
    configure(client, fake_llm.url, helper=False)
    client.post("/api/batch", json={"mode": "mixed", "lines": 10})
    client.post("/api/translate", json={"uids": ["a.jar", "b.jar"]})
    j = wait_job(client)
    assert sent_keys(fake_llm)[0] == ["gui.title", "gui.title#2"]           # 重複キーは番号を付けて区別
    assert pack(j, "a")["gui.title"] == "訳「Alpha Menu」"
    assert pack(j, "b")["gui.title"] == "訳「Beta Menu」"


def test_lines_one_and_context_limit(client, fake_llm):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=False)
    client.post("/api/batch", json={"mode": "mixed", "lines": 1})
    client.post("/api/translate", json={"uids": ["lib.jar"], "include_deps": False})
    wait_job(client)
    assert all(len(b) == 1 for b in sent_keys(fake_llm))                   # 1 行ずつ

    n = len(fake_llm.calls)
    long = jar_bytes({"fabric.mod.json": {"id": "lg", "name": "Long"},
                      "assets/lg/lang/en_us.json": {f"lg.k{i}": "Long sentence text " * 60 for i in range(4)}})
    upload(client, ("long.jar", long))
    A.S.model_cfg["translator"]["ctx"] = 100                                 # 文字数の上限 = 2000
    client.post("/api/batch", json={"mode": "mixed", "lines": 1000})
    client.post("/api/translate", json={"uids": ["long.jar"]})
    wait_job(client)
    A.S.model_cfg["translator"]["ctx"] = 16384
    assert [len(b) for b in sent_keys(fake_llm, n)] == [1, 1, 1, 1]          # 文脈長に収まるよう分割
    assert any("行未満にしました" in l for l in A.S.logs)


def test_resume_uses_current_mode(client, fake_llm, monkeypatch):
    from test_resume import crash_after, run_until_crash
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=False)
    A.S.memory.tm.clear()
    crash_after(monkeypatch, "_translate_chunk", 2)                          # Mod ごとで 2 回訳して強制終了
    job = run_until_crash({"uids": ["lib.jar", "main.jar"], "chunk_items": 1, "use_existing_ja": False})
    monkeypatch.undo()
    A.JOBS.mark_interrupted_on_startup()
    done_before = [k for b in sent_keys(fake_llm) for k in b]
    assert len(done_before) == 2

    client.post("/api/batch", json={"mode": "mixed", "lines": 50})          # 再開前に切り替える
    n = len(fake_llm.calls)
    client.post(f"/api/jobs/{job['id']}/resume")
    j = wait_job(client)
    assert j["error"] is None
    after = sent_keys(fake_llm, n)
    assert len(after[0]) > 1                                                 # まとめて訳している
    flat = [k for b in after for k in b]
    assert not set(flat) & set(done_before)                                  # 訳し終えた行は送らない
    assert pack(j, "corelib")["corelib.msg"].startswith("訳「")
    hist = [e for e in client.get(f"/api/jobs/{job['id']}/history").json() if e["event"] == "batch_mode"]
    assert [e["mode"] for e in hist] == ["per_mod", "mixed"]                 # 開始時と再開時のまとめ方
