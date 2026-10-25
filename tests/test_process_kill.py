"""本物のプロセスを強制終了 (kill) → --resume latest で再起動して最後まで完成するか。"""
import json
import os
import socket
import subprocess
import sys
import time
import zipfile

import requests

from conftest import ROOT, make_jar


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def launch(workdir, port, *extra):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "MCT_WORKDIR": str(workdir)}
    env.pop("MCT_TOKEN", None)
    return subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--port", str(port), "--token", "k",
                             "--workdir", str(workdir), *extra],
                            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8")


def wait_for(fn, timeout=60):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            v = fn()
            if v:
                return v
        except requests.RequestException:
            pass
        time.sleep(0.2)
    raise TimeoutError


def test_kill_and_resume_real_process(tmp_path, fake_llm):
    work = tmp_path / "drive" / "mct"          # Google ドライブ上のフォルダに相当
    entries = {f"item.big.thing{i}": f"Thing number {i}" for i in range(12)}
    make_jar((work / "mods").mkdir(parents=True) or work / "mods" / "big.jar",
             {"fabric.mod.json": {"id": "big", "name": "Big Mod"}, "assets/big/lang/en_us.json": entries})
    (work / "settings.json").write_text(json.dumps({"models": {
        "translator": {"url": fake_llm.url}, "helper": {"repo": "", "url": ""}}}), "utf-8")
    fake_llm.delay = 0.3
    port = free_port()
    base, H = f"http://127.0.0.1:{port}", {"x-mct-token": "k"}
    st = lambda: requests.get(base + "/api/status", headers=H, timeout=5).json()

    p1 = launch(work, port)
    try:
        wait_for(lambda: requests.get(base + "/api/mods", headers=H, timeout=5).json())
        requests.post(base + "/api/translate", headers=H, json={"uids": ["big.jar"], "chunk_items": 1}, timeout=5)
        wait_for(lambda: st()["job"]["done"] >= 4)
    finally:
        p1.kill()                               # ← 強制終了 (セッション切れ相当)
        p1.wait()
    calls_before = len(fake_llm.calls)
    job_file = next(work.glob("jobs/*/job.json"))
    saved = json.loads(job_file.read_text("utf-8"))
    assert saved["status"] == "running"
    done_before = saved["units"]["big.jar__big"]["done"]
    assert 4 <= done_before < 12

    fake_llm.delay = 0
    p2 = launch(work, port, "--resume", "latest")
    try:
        result = wait_for(lambda: st()["job"]["result"], timeout=60)
        logs = "\n".join(st()["logs"])
    finally:
        p2.kill()
        p2.wait()
    assert "中断されたジョブを検出" in logs and "を再開" in logs

    sent_after = sum(len(json.loads(c["user"].split("# 翻訳対象")[1].split("```json")[1].split("```")[0]))
                     for c in fake_llm.calls[calls_before:])
    assert sent_after <= 12 - done_before + 1     # 保存済みは再送しない (kill 時の処理中 1 件のみ重複可)

    z = zipfile.ZipFile(work / "output" / result)
    ja = json.loads(z.read("assets/big/lang/ja_jp.json"))
    assert list(ja) == list(entries) and all(v == f"訳「{entries[k]}」" for k, v in ja.items())
    final = json.loads(job_file.read_text("utf-8"))
    assert final["status"] == "done" and final["resumes"] == 1
    hist = [json.loads(l)["event"] for l in (work / "history.jsonl").read_text("utf-8").splitlines()]
    assert hist[0] == "start" and "interrupted" in hist and hist[-1] == "done"
