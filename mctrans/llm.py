"""llama.cpp (llama-server) を GPU で起動・管理し、OpenAI 互換 API で呼び出す。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests

DEFAULT_TRANSLATE_REPO = "IndexTeam/Index-Translate-35B-A3B-preview-GGUF"
DEFAULT_HELPER_REPO = "Qwen/Qwen2.5-3B-Instruct-GGUF"

# 量子化の優先順 (無料 Colab T4 16GB VRAM + 12GB RAM を想定)
QUANT_PREFERENCE = ["Q4_K_M", "IQ4_XS", "Q4_K_S", "Q3_K_M", "Q3_K_L", "Q4_0", "Q5_K_M", "Q2_K", "Q8_0"]

PRESETS = {
    "translator": [
        {"repo": DEFAULT_TRANSLATE_REPO, "label": "Index-Translate 35B-A3B preview (既定)"},
        {"repo": "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF", "label": "Qwen3 30B-A3B Instruct 2507"},
        {"repo": "mradermacher/Sakura-14B-Qwen2.5-v1.0-GGUF", "label": "Sakura 14B (日本語特化)"},
        {"repo": "Qwen/Qwen2.5-7B-Instruct-GGUF", "label": "Qwen2.5 7B Instruct (軽量)"},
    ],
    "helper": [
        {"repo": DEFAULT_HELPER_REPO, "label": "Qwen2.5 3B Instruct (既定)"},
        {"repo": "unsloth/Qwen3-4B-Instruct-2507-GGUF", "label": "Qwen3 4B Instruct 2507"},
        {"repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF", "label": "Qwen2.5 1.5B Instruct"},
        {"repo": "", "label": "使わない"},
    ],
}


def list_gguf(repo: str) -> list[dict]:
    from huggingface_hub import HfApi
    info = HfApi().model_info(repo, files_metadata=True)
    files = []
    for s in info.siblings or []:
        f = s.rfilename
        if not f.endswith(".gguf") or "mmproj" in f.lower():
            continue
        m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", f)
        if m and m.group(1) != "00001":
            continue  # 分割ファイルは先頭のみ表示
        size = s.size or 0
        if m:
            prefix = f[: m.start()]
            size = sum((x.size or 0) for x in info.siblings if x.rfilename.startswith(prefix))
        files.append({"file": f, "size": size})
    return sorted(files, key=lambda x: x["size"])


def memory_budget() -> tuple[int, int]:
    """(GPU VRAM 合計, 空き RAM) をバイトで返す。取得できなければ 0。"""
    vram = 0
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5).stdout
        vram = int(out.splitlines()[0]) * 2**20
    except Exception:
        pass
    try:
        import psutil
        ram = psutil.virtual_memory().available
    except Exception:
        ram = 0
    return vram, ram


VRAM_USABLE = 0.85   # KV キャッシュ・CUDA 作業領域の分を残す
RAM_USABLE = 0.45    # Colab 本体・ダウンロード等の分を残す


def pick_default_file(files: list[dict], vram: int = 0, ram: int = 0) -> str | None:
    """優先量子化のうち、VRAM (+ MoE を CPU に逃がせる RAM) に収まる最良のものを選ぶ。"""
    if not files:
        return None
    budget = vram * VRAM_USABLE + ram * RAM_USABLE if vram else float("inf")
    if budget == float("inf"):
        for q in QUANT_PREFERENCE:
            for f in files:
                if q.lower() in f["file"].lower():
                    return f["file"]
    # Q4_K_M 以下の推奨量子化で、収まる中で最大 (=最高品質) のもの
    ok = [f for f in files if any(q.lower() in f["file"].lower() for q in QUANT_PREFERENCE[:7])
          and f["size"] and f["size"] <= budget]
    if ok:
        return max(ok, key=lambda f: f["size"])["file"]
    return min(files, key=lambda f: f["size"] or 0)["file"]


def auto_cpu_moe(model_path: str, vram: int) -> int:
    """モデルが VRAM に収まらない場合、CPU に置く MoE 層の数を見積もる (0 = 全部 GPU)。"""
    size = sum(p.stat().st_size for p in Path(model_path).parent.glob(
        re.sub(r"-\d{5}-of-(\d{5})\.gguf$", r"-*-of-\1.gguf", Path(model_path).name)))
    if not vram or size <= vram * VRAM_USABLE:
        return 0
    try:
        import gguf
        r = gguf.GGUFReader(model_path)
        arch = bytes(r.fields["general.architecture"].parts[-1]).decode()
        if f"{arch}.expert_count" not in r.fields:
            return 0          # MoE でなければ対象外 (-ngl で調整する)
        layers = int(r.fields[f"{arch}.block_count"].parts[-1][0])
    except Exception:
        layers = 48
    over = (size - vram * VRAM_USABLE) / size
    return min(layers, int(layers * over * 1.2) + 1)


def download(repo: str, filename: str, cache_dir: str, log=print) -> str:
    from huggingface_hub import hf_hub_download, HfApi
    m = re.search(r"-(\d{5})-of-(\d{5})\.gguf$", filename)
    targets = [filename]
    if m:
        prefix = filename[: m.start()]
        targets = sorted(s.rfilename for s in HfApi().model_info(repo).siblings
                         if s.rfilename.startswith(prefix) and s.rfilename.endswith(".gguf"))
    first = None
    for t in targets:
        log(f"ダウンロード中: {repo}/{t}")
        p = hf_hub_download(repo, t, cache_dir=cache_dir)
        first = first or p
    return first


def find_server_binary() -> str | None:
    cands = [os.environ.get("LLAMA_SERVER", ""),
             "/content/llama.cpp/build/bin/llama-server",
             str(Path(__file__).resolve().parent.parent / "llama.cpp/build/bin/llama-server"),
             shutil.which("llama-server") or ""]
    for c in cands:
        if c and Path(c).exists():
            return c
        if c and Path(c + ".exe").exists():
            return c + ".exe"
    return None


HF_URL_RE = re.compile(
    r"^https://(?:huggingface\.co|hf\.co)/(?P<repo>[\w.\-]+/[\w.\-]+)/(?:resolve|blob)/(?P<rev>[^/?#]+)/(?P<file>[^?#]+\.gguf)(?:[?#].*)?$")


def parse_model_url(url: str) -> dict:
    """モデルの URL を解釈する。
    - Hugging Face: https://huggingface.co/<owner>/<repo>/resolve/<rev>/<file>.gguf?download=true (blob/ も可)
    - その他: https://.../<file>.gguf への直接リンク
    """
    from urllib.parse import unquote, urlparse
    url = url.strip()
    m = HF_URL_RE.match(url)
    if m:
        file = unquote(m.group("file"))
        if ".." in file.split("/"):
            raise ValueError("不正なファイルパスです")
        return {"kind": "hf", "repo": m.group("repo"), "revision": unquote(m.group("rev")), "file": file}
    u = urlparse(url)
    name = unquote(Path(u.path).name)
    if u.scheme != "https" or not u.netloc:
        raise ValueError("https の URL を指定してください")
    if not name.lower().endswith(".gguf"):
        raise ValueError("GGUF ファイル (.gguf) の URL を指定してください")
    return {"kind": "http", "url": url, "file": re.sub(r"[^\w.\-+]", "_", name)}


def download_from_url(info: dict, cache_dir: str, log=print, progress=None) -> str:
    """parse_model_url の結果からダウンロードし、ローカルパスを返す (途中から再開可能)。"""
    if info["kind"] == "hf":
        from huggingface_hub import hf_hub_download
        log(f"ダウンロード中: {info['repo']}/{info['file']} ({info['revision']})")
        if re.search(r"-\d{5}-of-\d{5}\.gguf$", info["file"]):
            return download(info["repo"], info["file"], cache_dir, log=log)
        return hf_hub_download(info["repo"], info["file"], revision=info["revision"], cache_dir=cache_dir)
    dest = Path(cache_dir) / "url_models" / info["file"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    if dest.exists():
        return str(dest)
    done = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={done}-"} if done else {}
    log(f"ダウンロード中: {info['url']}" + (f" ({done / 2**30:.1f} GB から再開)" if done else ""))
    with requests.get(info["url"], headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        if done and r.status_code != 206:
            done = 0                                    # サーバーが Range 非対応 → 最初から
        total = done + int(r.headers.get("Content-Length", 0) or 0)
        with part.open("ab" if done else "wb") as f:
            for chunk in r.iter_content(8 * 2**20):
                f.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    os.replace(part, dest)
    return str(dest)


class LlamaServer:
    def __init__(self, role: str, port: int):
        self.role = role
        self.port = port
        self.proc: subprocess.Popen | None = None
        self.model_path = ""
        self.repo = ""
        self.log_tail: list[str] = []
        self.external_url = ""  # 既存の OpenAI 互換サーバーを使う場合

    @property
    def base_url(self) -> str:
        return self.external_url or f"http://127.0.0.1:{self.port}"

    def running(self) -> bool:
        if self.external_url:
            return True
        return self.proc is not None and self.proc.poll() is None

    def ready(self) -> bool:
        try:
            return requests.get(self.base_url + "/health", timeout=2).status_code == 200
        except Exception:
            return False

    def start(self, model_path: str, ctx: int = 16384, ngl: int = 999, n_cpu_moe: int = 0,
              parallel: int = 1, log=print, timeout: int = 1800):
        self.stop()
        binary = find_server_binary()
        if not binary:
            raise RuntimeError("llama-server が見つかりません。セットアップセルを実行するか LLAMA_SERVER 環境変数を設定してください。")
        cmd = [binary, "-m", model_path, "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(ctx), "-ngl", str(ngl), "-np", str(parallel), "--jinja", "-fa", "auto"]
        if n_cpu_moe > 0:
            cmd += ["--n-cpu-moe", str(n_cpu_moe)]
        log(f"[{self.role}] 起動: {' '.join(cmd)}")
        self.log_tail = []
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                     encoding="utf-8", errors="replace")
        self.model_path = model_path
        pump = threading.Thread(target=self._pump, daemon=True)
        pump.start()
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.proc.poll() is not None:
                pump.join(3)          # 終了直前の出力 (エラーの理由) を読み切ってから報告する
                detail = "\n".join(self.log_tail[-30:]) or self._diagnose(binary)
                raise RuntimeError(f"[{self.role}] llama-server が終了しました (終了コード {self.proc.returncode}):\n{detail}")
            if self.ready():
                log(f"[{self.role}] 準備完了 ({time.time() - t0:.0f}s)")
                for line in self.device_report():
                    log(f"[{self.role}] {line}")
                return
            time.sleep(2)
        raise RuntimeError(f"[{self.role}] 起動タイムアウト")

    DEVICE_RE = re.compile(r"ggml_cuda_init|CUDA\d? *:|using device|offloaded \d+/\d+ layers|no usable GPU|"
                           r"failed to initialize|compiled without GPU", re.I)

    def device_report(self) -> list[str]:
        """llama-server の起動ログから GPU の使用状況を抜き出す。GPU を使っていなければ警告を付ける。"""
        lines = [l.strip() for l in self.log_tail if self.DEVICE_RE.search(l)][:6]
        offload = [re.search(r"offloaded (\d+)/(\d+) layers", l) for l in self.log_tail]
        offload = [m for m in offload if m]
        uses_gpu = any(int(m.group(1)) > 0 for m in offload) or \
            any(re.search(r"CUDA\d? *: .*(buffer|model) size", l) for l in self.log_tail)
        if not uses_gpu:
            lines.append("警告: GPU が使われていません。CPU で動くため非常に遅くなります。"
                         "ランタイムのタイプが GPU か、セットアップで CUDA が有効になっているかを確認してください。")
        return lines

    def recent_log(self, n: int = 15) -> str:
        return "\n".join(self.log_tail[-n:])

    @staticmethod
    def _diagnose(binary: str) -> str:
        """出力がないまま終了した場合に、単体で実行して理由 (ライブラリ不足など) を得る。"""
        try:
            r = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30,
                               encoding="utf-8", errors="replace")
            out = (r.stderr or r.stdout).strip()
            return out[-1500:] if out else f"出力なし (llama-server --version の終了コード {r.returncode})"
        except Exception as e:
            return f"llama-server を実行できません: {e}"

    def _pump(self):
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.log_tail.append(line.rstrip())
            del self.log_tail[:-200]

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def chat(self, system: str, user: str, temperature: float = 0.2, max_tokens: int = 4096,
             json_mode: bool = False, timeout: float = 1800) -> str:
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": temperature, "max_tokens": max_tokens, "stream": False}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = requests.post(self.base_url + "/v1/chat/completions", json=body, timeout=timeout)
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"] or ""
        return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
