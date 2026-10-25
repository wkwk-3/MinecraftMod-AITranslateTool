import json
import os
import re
import sys
import tempfile
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MCT_WORKDIR", tempfile.mkdtemp(prefix="mct_test_"))
os.environ["MCT_TOKEN"] = "test-token"


def make_jar(path: Path, files: dict[str, str | dict]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, body in files.items():
            z.writestr(name, body if isinstance(body, str) else json.dumps(body))
    return path


@pytest.fixture
def jar_factory(tmp_path):
    def make(name, files):
        return make_jar(tmp_path / name, files)
    return make


# ---------------------------------------------------------------- fake OpenAI compatible LLM
class FakeLLM:
    """llama-server 互換の偽サーバー。呼び出しを記録し、ルールで応答する。"""

    def __init__(self):
        self.calls: list[dict] = []
        self.break_once: set[str] = set()   # 最初の 1 回だけプレースホルダを壊すキー
        self.always_break: set[str] = set()
        self.garbage_calls = 0              # 先頭 N 回は JSON でない応答
        self.delay = 0.0                    # 応答の遅延 (秒)
        self.files: dict[str, bytes] = {}   # /files/<name> で配信するファイル
        self.file_requests: list = []

    def respond(self, body: dict) -> str:
        system = body["messages"][0]["content"]
        user = body["messages"][1]["content"]
        self.calls.append({"system": system, "user": user})
        if self.delay:
            import time
            time.sleep(self.delay)
        if self.garbage_calls > 0:
            self.garbage_calls -= 1
            return "すみません、わかりません"
        if "lang 以外のファイル" in system:
            paths = re.findall(r"^## (\S+)$", user, re.M)
            files = [{"path": p, "translate": "credits" not in p, "reason": "テスト判定",
                      "keys": ["name", "title", "text", "description", "type"] if p.endswith(".json") else []}
                     for p in paths]
            files.append({"path": "assets/evil/../../x.json", "translate": True})   # 入力にないパスは無視されるべき
            return json.dumps({"files": files}, ensure_ascii=False)
        if "公式の英日対訳" in system:
            # 正しい用語 1 つ + 対訳に存在しない捏造 1 つ
            return json.dumps({"terms": {"Arcane Dust": "秘術の粉", "Dragon": "ドラゴン"}}, ensure_ascii=False)
        if "依存関係 ID" in system:
            return json.dumps({"matches": {"corelib-api": "lib.jar"}})
        if "用語集の候補" in system:
            return json.dumps({"terms": {"Arcane Crystal": "アルケインクリスタル"}}, ensure_ascii=False)
        data = json.loads(re.findall(r"```json\n(.*?)\n```", user, re.S)[-1])
        out = {}
        for k, v in data.items():
            t = "訳「" + v + "」"
            if k in self.always_break or k in self.break_once:
                self.break_once.discard(k)
                t = re.sub(r"⟦\d+⟧", "", t)
            out[k] = t
        return "<think>考え中</think>```json\n" + json.dumps(out, ensure_ascii=False) + "\n```"


@pytest.fixture
def fake_llm():
    fake = FakeLLM()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/files/"):          # GGUF の直接ダウンロード (Range 対応)
                data = fake.files.get(self.path[len("/files/"):])
                if data is None:
                    self.send_response(404); self.end_headers(); return
                start = int(self.headers.get("Range", "bytes=0-")[6:].split("-")[0] or 0)
                self.send_response(206 if start else 200)
                self.send_header("Content-Length", str(len(data) - start))
                self.end_headers()
                self.wfile.write(data[start:])
                fake.file_requests.append((self.path, start))
                return
            self.send_response(200 if self.path == "/health" else 404)
            self.end_headers()
            self.wfile.write(b"{}")

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = fake.respond(body)
            payload = json.dumps({"choices": [{"message": {"content": text}}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fake.url = f"http://127.0.0.1:{srv.server_address[1]}"
    yield fake
    srv.shutdown()
