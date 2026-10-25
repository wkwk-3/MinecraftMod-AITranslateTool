"""強制終了・セッション切れからの途中再開のテスト。"""
import json
import zipfile

import pytest

from mctrans import app as A, jobs
from test_app import LIB, MAIN, OTHER, client, configure, upload, wait_job  # noqa: F401


class Crash(BaseException):
    """プロセスの強制終了を模擬 (except Exception では捕まらない)。"""


def crash_after(monkeypatch, fn_name, n):
    real = getattr(A, fn_name)
    count = {"n": 0}

    def wrapper(*a, **k):
        if count["n"] >= n:
            raise Crash()
        count["n"] += 1
        return real(*a, **k)
    monkeypatch.setattr(A, fn_name, wrapper)
    return count


def run_until_crash(req: dict) -> dict:
    """スレッドを使わずに実行し、Crash で止まった時点のジョブを返す (状態は RUNNING のまま)。"""
    job = A.JOBS.create(A.TranslateReq(**req).model_dump())
    A.S.job.update(running=True, id=job["id"], done=0, total=0, result=None, error=None)
    with pytest.raises(Crash):
        A._run_job(job)
    A.S.job["running"] = False
    return A.JOBS.load(job["id"])


def translated_keys(fake):
    keys = []
    for c in fake.calls:
        if "# 翻訳対象" in c["user"]:
            body = c["user"].split("# 翻訳対象")[1]
            keys += list(json.loads(body[body.index("{"):body.rindex("}") + 1]))
    return keys


def test_crash_during_translation_then_resume(client, fake_llm, monkeypatch):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=False)
    crash_after(monkeypatch, "_translate_chunk", 3)
    job = run_until_crash({"uids": ["main.jar"], "include_deps": False, "chunk_items": 1})
    assert job["status"] == jobs.RUNNING                     # 落ちた直後は RUNNING のまま
    unit = jobs.unit_id("main.jar", "magic")
    part = A.JOBS.load_part(job, unit)
    assert job["units"][unit]["done"] == 3 and job["units"][unit]["status"] == "pending"
    before = translated_keys(fake_llm)
    assert len(before) == 3 and all(k in part for k in before)   # 3 チャンク分は保存済み

    # ---- 再起動を模擬
    monkeypatch.undo()
    assert A.JOBS.mark_interrupted_on_startup() == [job["id"]]
    st = client.get("/api/status").json()
    assert [j["id"] for j in st["resumable"]] == [job["id"]]
    assert st["resumable"][0]["done"] == 3

    n = len(fake_llm.calls)
    assert client.post(f"/api/jobs/{job['id']}/resume").status_code == 200
    j = wait_job(client)
    assert j["error"] is None and j["result"]
    after = translated_keys(type("F", (), {"calls": fake_llm.calls[n:]}))
    assert not set(after) & set(before)                      # 翻訳済みは再送しない
    assert "magic.broken" in after                           # 残りは翻訳

    final = A.JOBS.load(job["id"])
    assert final["status"] == jobs.DONE and final["resumes"] == 1 and final["steps"]["pack"]
    magic = json.loads(zipfile.ZipFile(A.OUT_DIR / j["result"]).read("assets/magic/lang/ja_jp.json"))
    src = json.loads(zipfile.ZipFile(__import__("io").BytesIO(MAIN)).read("assets/magic/lang/en_us.json"))
    assert list(magic) == list(src)                          # 全キーそろって元の順序
    assert magic["item.magic.wand"] == "訳「Magic Wand」"
    events = [e["event"] for e in client.get(f"/api/jobs/{job['id']}/history").json()]
    assert events[:4] == ["start", "deps", "helper", "plan"]   # helper = 同梱 ja_jp / 用語抽出の段階
    assert "interrupted" in events and "resume" in events and events[-1] == "done"


def test_crash_during_helper_resumes_only_unprocessed_mods(client, fake_llm, monkeypatch):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=True)
    crash_after(monkeypatch, "_helper_chat", 2)      # 依存照合 1 回 + 用語抽出 1 MOD 目まで
    job = run_until_crash({"uids": ["main.jar"]})
    assert job["steps"]["deps"] and job["order"] == ["lib.jar", "main.jar"]
    assert job["steps"]["helper_done"] == ["lib.jar"] and not job["steps"]["plan"]
    monkeypatch.undo()
    A.JOBS.mark_interrupted_on_startup()
    assert client.get("/api/jobs").json()[0]["helper_pending"] == ["main.jar"]

    n = len(fake_llm.calls)
    client.post(f"/api/jobs/{job['id']}/resume")
    assert wait_job(client)["error"] is None
    new = fake_llm.calls[n:]
    assert not [c for c in new if "依存関係 ID" in c["system"]]          # 依存解決は再実行しない
    helper = [c["user"] for c in new if "用語集の候補" in c["system"]]
    assert len(helper) == 1 and "Magic Mod" in helper[0]                  # 未処理の MOD だけ
    assert any("補助モデルで処理する MOD: Magic Mod" in l for l in A.S.logs)


def test_finished_mods_skip_helper_on_resume(client, fake_llm, monkeypatch):
    upload(client, ("lib.jar", LIB), ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=True)
    job = A.JOBS.create(A.TranslateReq(uids=["main.jar"]).model_dump())
    # 履歴: 依存解決・計画済み、lib は翻訳完了、両方とも用語抽出は未記録
    job["steps"].update(deps=True, plan=True)
    job["order"] = ["lib.jar", "main.jar"]
    for uid, ns, st in [("lib.jar", "corelib", "done"), ("main.jar", "magic", "pending")]:
        job["units"][jobs.unit_id(uid, ns)] = {"uid": uid, "ns": ns, "format": "json", "total": 1, "done": 0, "status": st}
    job["status"] = jobs.INTERRUPTED
    A.JOBS.save(job)
    client.post(f"/api/jobs/{job['id']}/resume")
    assert wait_job(client)["error"] is None
    helper = [c["user"] for c in fake_llm.calls if "用語集の候補" in c["system"]]
    assert len(helper) == 1 and "Magic Mod" in helper[0]          # 翻訳済みの lib は補助モデル不要


def test_cancel_then_resume(client, fake_llm, monkeypatch):
    upload(client, ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=False)
    real = A._translate_chunk

    def slow(*a, **k):
        A.S.cancel.set()           # 1 チャンク目の途中でキャンセル操作
        return real(*a, **k)
    monkeypatch.setattr(A, "_translate_chunk", slow)
    client.post("/api/translate", json={"uids": ["main.jar"], "chunk_items": 2})
    j = wait_job(client)
    assert j["phase"] == "中断" and "再開できます" in j["error"]
    job_id = client.get("/api/jobs").json()[0]["id"]
    assert A.JOBS.load(job_id)["status"] == jobs.CANCELLED
    assert A.JOBS.load(job_id)["units"][jobs.unit_id("main.jar", "magic")]["done"] == 2
    monkeypatch.undo()
    client.post(f"/api/jobs/{job_id}/resume")
    assert wait_job(client)["result"]


def test_resume_rejections(client, fake_llm, monkeypatch):
    upload(client, ("main.jar", MAIN))
    configure(client, fake_llm.url, helper=False)
    client.post("/api/translate", json={"uids": ["main.jar"]})
    wait_job(client)
    done_id = client.get("/api/jobs").json()[0]["id"]
    assert client.post(f"/api/jobs/{done_id}/resume").status_code == 400      # 完了済み
    assert client.post("/api/jobs/nope/resume").status_code == 404
    assert client.post("/api/jobs/..%2F..%2Fx/resume").status_code == 404

    A.S.memory.tm.clear()
    crash_after(monkeypatch, "_translate_chunk", 0)
    job = run_until_crash({"uids": ["main.jar"]})
    monkeypatch.undo()
    A.JOBS.mark_interrupted_on_startup()
    (A.MODS_DIR / "main.jar").unlink()
    A.S.mods.pop("main.jar")
    r = client.post(f"/api/jobs/{job['id']}/resume")
    assert r.status_code == 400 and "main.jar" in r.json()["detail"]
    assert client.post(f"/api/jobs/{job['id']}/delete").json()["ok"]
    assert all(j["id"] != job["id"] for j in client.get("/api/jobs").json())


def test_run_py_auto_resume(client, fake_llm, monkeypatch, capsys):
    import run
    upload(client, ("lib.jar", LIB))
    configure(client, fake_llm.url, helper=False)
    run.resume("latest")
    assert "中断した翻訳はありません" in capsys.readouterr().out
    crash_after(monkeypatch, "_translate_chunk", 0)
    job = run_until_crash({"uids": ["lib.jar"]})
    monkeypatch.undo()
    A.JOBS.mark_interrupted_on_startup()
    run.resume("latest")
    assert f"中断した翻訳を再開しました: {job['id']}" in capsys.readouterr().out
    assert wait_job(client)["result"]


def test_model_settings_persist(client):
    client.post("/api/models/config", json={"role": "translator", "repo": "a/b", "file": "x.gguf", "n_cpu_moe": 3})
    saved = json.loads((A.WORK / "settings.json").read_text("utf-8"))
    assert saved["models"]["translator"]["repo"] == "a/b" and saved["models"]["translator"]["n_cpu_moe"] == 3
    assert A.State().model_cfg["translator"]["file"] == "x.gguf"            # 再起動後も復元


# ---------------------------------------------------------------- JobStore 単体
def test_jobstore_robustness(tmp_path):
    st = jobs.JobStore(tmp_path)
    job = st.create({"uids": ["a"]})
    st.save_part(job, "u", {"k": "訳"})
    # 書き込み途中で落ちた一時ファイル・壊れた job.json・途中で切れた履歴行
    (st.path(job["id"]) / ".job.json.123.tmp").write_text("{broken")
    bad = st.root / "broken-job"
    bad.mkdir()
    (bad / "job.json").write_text("{not json")
    with st.history.open("a", encoding="utf-8") as f:
        f.write('{"t": "x", "job": "' + job["id"] + '", "ev')
    assert [j["id"] for j in st.list()] == [job["id"]]
    assert st.load_part(job, "u") == {"k": "訳"} and st.load_part(job, "missing") == {}
    assert [e["event"] for e in st.read_history(job["id"])] == ["start"]
    with pytest.raises(ValueError):
        st.path("../evil")
    assert st.mark_interrupted_on_startup() == [job["id"]]
    assert st.mark_interrupted_on_startup() == []
    assert [j["id"] for j in st.resumable()] == [job["id"]]
    s = jobs.summarize(st.load(job["id"]))
    assert s["status"] == "interrupted" and s["total"] == 0


def test_memory_survives_corruption(tmp_path):
    from mctrans import translator as tr
    p = tmp_path / "memory.json"
    p.write_text("{truncated", encoding="utf-8")
    m = tr.Memory(p)
    assert m.tm == {}
    m.tm["a"] = "あ"
    m.save()
    assert tr.Memory(p).tm == {"a": "あ"}
    assert not list(tmp_path.glob(".*.tmp"))
