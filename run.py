"""起動スクリプト (Colab / ローカル共通)。

  python run.py                       # ローカル: http://127.0.0.1:7860/?token=...
  python run.py --tunnel ngrok        # ngrok で公開 (NGROK_AUTHTOKEN が必要)
  python run.py --tunnel cloudflared  # Cloudflare Quick Tunnel (アカウント不要)
  python run.py --tunnel colab        # Colab のプロキシ (Google ログイン本人のみアクセス可)
  python run.py --workdir D:/mct      # MOD・翻訳結果・途中経過・履歴の保存先を指定
  python run.py --resume latest       # 中断したジョブを起動時に自動で再開 (ジョブ ID も指定可)
"""
from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import threading
import time
import re


def start_ngrok(port: int) -> str:
    from pyngrok import conf, ngrok
    tok = os.environ.get("NGROK_AUTHTOKEN")
    if not tok:
        raise SystemExit("NGROK_AUTHTOKEN がありません。Colab のシークレットに登録してください。")
    conf.get_default().auth_token = tok
    return ngrok.connect(port, "http", bind_tls=True).public_url


def start_cloudflared(port: int) -> str:
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
            return m.group(0)
    raise SystemExit("cloudflared の URL を取得できませんでした")


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=7860)
    ap.add_argument("--tunnel", choices=["none", "ngrok", "cloudflared", "colab"], default="none")
    ap.add_argument("--token", default=os.environ.get("MCT_TOKEN") or secrets.token_urlsafe(24))
    ap.add_argument("--workdir", default=os.environ.get("MCT_WORKDIR"),
                    help="作業フォルダ (MOD・翻訳結果・途中経過・実行履歴)")
    ap.add_argument("--resume", default="", help="起動時に再開するジョブ: latest またはジョブ ID")
    a = ap.parse_args()
    os.environ["MCT_TOKEN"] = a.token
    if a.workdir:
        os.environ["MCT_WORKDIR"] = a.workdir

    import uvicorn
    from mctrans.app import app
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=a.port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2)

    if a.tunnel == "ngrok":
        base = start_ngrok(a.port)
    elif a.tunnel == "cloudflared":
        base = start_cloudflared(a.port)
    elif a.tunnel == "colab":
        from google.colab import output
        output.serve_kernel_port_as_window(a.port, path=f"/?token={a.token}")
        base = None
    else:
        base = f"http://127.0.0.1:{a.port}"

    print("=" * 70)
    if base:
        print(f"次の URL を開いてください: {base}/?token={a.token}")
    print("この URL を知っている人は画面を操作できます。他の人に教えないでください。")
    print("=" * 70, flush=True)
    if a.resume:
        resume(a.resume)
    try:
        while True:
            time.sleep(30)  # セルを実行状態に保つ
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
