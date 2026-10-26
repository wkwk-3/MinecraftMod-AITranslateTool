"""補助モデルが遅い・応答しない場合と、GPU 使用状況の報告。"""
from mctrans import app as A, llm
from test_app import LIB, client, configure, upload, wait_job  # noqa: F401
from test_features import JA_MOD


def test_slow_helper_is_stopped_and_job_continues(client, fake_llm, monkeypatch):
    upload(client, ("arcane.jar", JA_MOD), ("lib.jar", LIB))
    configure(client, fake_llm.url, helper=True)
    monkeypatch.setattr(A, "HELPER_TIMEOUT", 0.5)
    stopped = []
    monkeypatch.setattr(A.S.servers["helper"], "stop", lambda: stopped.append(1))
    # 外部 URL 指定時は running() が常に True なので、止めた後は動いていない扱いにする
    monkeypatch.setattr(A.S.servers["helper"], "running", lambda: not stopped)
    fake_llm.delay = 2                       # 補助モデルの応答が遅い
    client.post("/api/translate", json={"uids": ["arcane.jar"]})
    j = wait_job(client, timeout=60)
    fake_llm.delay = 0
    logs = "\n".join(A.S.logs)
    assert stopped, "補助モデルを止めていない"
    assert "以降はルールだけで処理します" in logs
    helper_calls = [c for c in fake_llm.calls if "入力の扱い" in c["system"] and "# 翻訳対象" not in c["user"]]
    assert len(helper_calls) == A.HELPER_MAX_FAILURES     # 失敗が続いたらそれ以上呼ばない
    assert j["result"], j                                  # 翻訳は最後まで終わる
    g = client.get("/api/glossary").json()
    assert g["official"]["Arcane Dust"] == "秘術の粉"      # ルールだけの用語集は作られる


def test_helper_success_resets_failures(client, fake_llm):
    configure(client, fake_llm.url, helper=True)
    assert A._ensure_model("helper")
    A.S.helper_failures = 1
    assert A._helper_chat(A.tr.HELPER_TERMS_PROMPT, "x") is not None
    assert A.S.helper_failures == 0


def _srv(lines):
    s = llm.LlamaServer("t", 1)
    s.log_tail = lines
    return s


def test_device_report_gpu():
    r = _srv(["ggml_cuda_init: found 1 CUDA devices:", "  Device 0: Tesla T4, compute capability 7.5",
              "load_tensors: offloaded 37/37 layers to GPU", "load_tensors:        CUDA0 model buffer size =  2600 MiB"]
             ).device_report()
    assert any("offloaded 37/37" in l for l in r) and not any("警告" in l for l in r)


def test_device_report_cpu_only_warns():
    r = _srv(["load_tensors: offloaded 0/37 layers to GPU", "load_tensors: CPU_Mapped model buffer size = 2600 MiB"]
             ).device_report()
    assert any("GPU が使われていません" in l for l in r)
    assert any("GPU が使われていません" in l for l in _srv([]).device_report())
