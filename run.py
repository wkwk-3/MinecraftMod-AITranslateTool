"""起動スクリプト (Colab / ローカル共通)。

  python run.py                       # ローカル: http://127.0.0.1:7860/?token=...
  python run.py --tunnel ngrok        # ngrok で公開 (NGROK_AUTHTOKEN が必要)
  python run.py --tunnel cloudflared  # Cloudflare Quick Tunnel (アカウント不要)
  python run.py --tunnel colab        # Colab のプロキシ (Google ログイン本人のみアクセス可)
  python run.py --workdir D:/mct      # MOD・翻訳結果・途中経過・履歴の保存先を指定
  python run.py --resume latest       # 中断したジョブを起動時に自動で再開 (ジョブ ID も指定可)

Colab ではセルを再実行しても同じカーネル内でサーバーが動き続ける。
そのため 2 回目以降は起動済みのサーバーとトンネルを使い回し、同じトークンの URL を表示する。
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time


def _state() -> dict:
    """セルの再実行をまたいで残す情報 (起動済みサーバー・トンネル)。モジュールに持たせる。"""
    import mctrans
    if not hasattr(mctrans, "_run_state"):
        mctrans._run_state = {"server": None, "tunnels": {}}
    return mctrans._run_state


def port_in_use(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_server(port: int, token: str, workdir: str | None) -> str:
    """サーバーを起動して実際に使われるトークンを返す。起動済みならそれを使う。"""
    loaded = "mctrans.app" in sys.modules
    if loaded:
        A = sys.modules["mctrans.app"]
        if workdir and os.path.abspath(workdir) != str(A.WORK):
            print(f"保存先の変更は再起動後に反映されます (現在: {A.WORK})")
        if port_in_use(port):
            return A.TOKEN                      # このカーネルで動いているサーバーをそのまま使う
    elif port_in_use(port):
        raise SystemExit(f"ポート {port} はほかのプログラムが使っています。"
                         "ランタイムを再起動するか、--port で別の番号を指定してください。")
    else:
        os.environ["MCT_TOKEN"] = token
        if workdir:
            os.environ["MCT_WORKDIR"] = workdir

    import uvicorn
    from mctrans.app import TOKEN, app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):                        # 起動を待つ (最大 10 秒)
        if port_in_use(port):
            break
        time.sleep(0.1)
    else:
        raise SystemExit("サーバーを起動できませんでした")
    _state()["server"] = server
    return TOKEN


def start_ngrok(port: int) -> str:
    from pyngrok import conf, ngrok
    for t in ngrok.get_tunnels():               # 再実行時は既存のトンネルを使う
        if t.config.get("addr", "").endswith(f":{port}") and t.public_url.startswith("https"):
            return t.public_url
    tok = os.environ.get("NGROK_AUTHTOKEN")
    if not tok:
        raise SystemExit("NGROK_AUTHTOKEN がありません。Colab のシークレットに登録してください。")
    conf.get_default().auth_token = tok
    return ngrok.connect(port, "http", bind_tls=True).public_url


def start_cloudflared(port: int) -> str:
    prev = _state()["tunnels"].get("cloudflared")
    if prev and prev["proc"].poll() is None:    # 再実行時は動いているトンネルを使う
        return prev["url"]
    exe = shutil.which("cloudflared") or "/usr/local/bin/cloudflared"
    if not os.path.exists(exe):
        subprocess.run(["wget", "-q", "-O", exe,
                        "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"], check=True)
        os.chmod(exe, 0o755)
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        m = re.search(r"https://[\w-]+\.trycloudflare\.com", line)
        if m:
            threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()
            _state()["tunnels"]["cloudflared"] = {"proc": p, "url": m.group(0)}
            return m.group(0)
    raise SystemExit("cloudflared の URL を取得できませんでした")


def start_colab(port: int) -> str:
    """Colab のプロキシ URL。ノートブックを開いている本人のブラウザからだけ開ける。"""
    from google.colab.output import eval_js
    return eval_js(f"google.colab.kernel.proxyPort({port}, {{'cache': false}})").rstrip("/")


def resume(target: str):
    from mctrans import app as A
    cands = A.JOBS.resumable()
    if target != "latest":
        cands = [j for j in cands if j["id"] == target]
    if not cands:
        print("中断した翻訳はありません")
        return
    job = cands[0]
    try:
        A.resume_job(job["id"])
        print(f"中断した翻訳を再開しました: {job['id']}")
    except Exception as e:   # HTTPException 等
        print(f"再開できませんでした: {getattr(e, 'detail', e)}")


def show_url(url: str):
    print("=" * 70)
    print(f"次の URL を開いてください: {url}")
    print("この URL を知っている人は画面を操作できます。他の人に教えないでください。")
    print("=" * 70, flush=True)
    try:                                         # Colab ではクリックできるリンクも表示する
        from IPython.display import HTML, display
        from IPython import get_ipython
        if get_ipython() is not None:
            display(HTML(f'<a href="{url}" target="_blank" rel="noreferrer">画面を開く</a>'))
    except ImportError:
        pass


def main(argv: list[str] | None = None, block: bool = True) -> str:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--tunnel", choices=["none", "ngrok", "cloudflared", "colab"], default="none")
    ap.add_argument("--token", default=os.environ.get("MCT_TOKEN") or secrets.token_urlsafe(24))
    ap.add_argument("--workdir", default=os.environ.get("MCT_WORKDIR"),
                    help="作業フォルダ (MOD・翻訳結果・途中経過・実行履歴)")
    ap.add_argument("--resume", default="", help="起動時に再開するジョブ: latest またはジョブ ID")
    a = ap.parse_args(argv)

    token = start_server(a.port, a.token, a.workdir)
    if a.tunnel == "ngrok":
        base = start_ngrok(a.port)
    elif a.tunnel == "cloudflared":
        base = start_cloudflared(a.port)
    elif a.tunnel == "colab":
        base = start_colab(a.port)
    else:
        base = f"http://127.0.0.1:{a.port}"
    url = f"{base}/?token={token}"
    show_url(url)
    if a.resume:
        resume(a.resume)
    if block:
        try:
            while True:
                time.sleep(30)  # セルを実行状態に保つ
        except KeyboardInterrupt:
            print("画面の表示を止めました。サーバーは動き続けています。このセルを再実行すると URL を表示します。")
    return url


if __name__ == "__main__":
    main()
