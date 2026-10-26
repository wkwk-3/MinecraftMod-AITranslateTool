"""起動スクリプト: Colab でセルを再実行したときの動作。"""
import socket
import sys
import types

import pytest
import requests

import run
from mctrans import app as A


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_rerun_reuses_server_and_token(capsys):
    port = free_port()
    url1 = run.main(["--port", str(port), "--token", "first"], block=False)
    url2 = run.main(["--port", str(port), "--token", "second"], block=False)   # 2 回目 (セルの再実行)
    assert url1 == url2 == f"http://127.0.0.1:{port}/?token={A.TOKEN}"         # 動いているサーバーのトークン
    assert "address already in use" not in capsys.readouterr().err
    r = requests.get(url2, timeout=5)
    assert r.status_code == 200 and "MC MOD Translator" in r.text           # 表示した URL で実際に開ける


def test_port_used_by_other_program(monkeypatch):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        port = s.getsockname()[1]
        monkeypatch.delitem(sys.modules, "mctrans.app")                      # このカーネルでは未起動
        with pytest.raises(SystemExit, match="ほかのプログラムが使っています"):
            run.start_server(port, "x", None)


def test_colab_proxy_url(monkeypatch):
    calls = []
    out = types.ModuleType("google.colab.output")
    out.eval_js = lambda js: calls.append(js) or "https://abc-7860-colab.googleusercontent.com/"
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.colab", types.ModuleType("google.colab"))
    monkeypatch.setitem(sys.modules, "google.colab.output", out)
    port = free_port()
    url = run.main(["--port", str(port), "--tunnel", "colab"], block=False)
    assert url == f"https://abc-7860-colab.googleusercontent.com/?token={A.TOKEN}"
    assert calls == [f"google.colab.kernel.proxyPort({port}, {{'cache': false}})"]


def test_cloudflared_reused_on_rerun(monkeypatch):
    class Alive:
        def poll(self):
            return None
    run._state()["tunnels"]["cloudflared"] = {"proc": Alive(), "url": "https://same.trycloudflare.com"}
    monkeypatch.setattr(run.subprocess, "Popen", lambda *a, **k: pytest.fail("新しいトンネルを作ってはいけない"))
    assert run.start_cloudflared(1234) == "https://same.trycloudflare.com"
    run._state()["tunnels"].clear()
