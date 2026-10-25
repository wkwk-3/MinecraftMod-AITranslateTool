"""翻訳ジョブの永続化 (途中再開用)。

<WORK>/jobs/<job_id>/job.json            ジョブの状態 (段階・各 lang の進捗)
<WORK>/jobs/<job_id>/parts/<unit>.json   lang ごとの翻訳済みエントリ (チャンクごとに保存)
<WORK>/history.jsonl                     全ジョブの実行履歴 (追記のみ)

すべての書き込みは一時ファイル → rename のアトミック更新なので、
どのタイミングで強制終了しても壊れたファイルは残らない。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path

# ジョブの状態
RUNNING, INTERRUPTED, FAILED, CANCELLED, DONE = "running", "interrupted", "failed", "cancelled", "done"
RESUMABLE = {INTERRUPTED, FAILED, CANCELLED}


def atomic_write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:   # Windows: 他スレッドが読み込み中だと一時的に置換できない
            time.sleep(0.05 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise PermissionError(f"保存できませんでした: {path}")


def read_json(path: Path, default=None):
    for attempt in range(5):
        try:
            return json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return default
        except PermissionError:   # Windows: 置換中
            time.sleep(0.05)
    return default


def unit_id(uid: str, ns: str) -> str:
    return re.sub(r"[^\w.\-]", "_", f"{uid}__{ns}")


def res_unit_id(uid: str, path: str) -> str:
    """追加リソース用。パスは長く記号も多いのでハッシュで一意にする。"""
    import hashlib
    h = hashlib.sha1(path.encode()).hexdigest()[:10]
    return re.sub(r"[^\w.\-]", "_", f"{uid}__res__{path.rsplit('/', 1)[-1][:40]}__{h}")


class JobStore:
    def __init__(self, work: Path):
        self.root = work / "jobs"
        self.history = work / "history.jsonl"
        self.root.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------- job record
    def create(self, request: dict) -> dict:
        job = {
            "id": time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6],
            "created": time.time(), "updated": time.time(), "status": RUNNING,
            "request": request,
            "order": [], "missing": [],
            "steps": {"deps": False, "helper_done": [], "plan": False, "pack": False},
            "units": {},          # unit_id -> {uid, ns, format, total, done, status}
            "result": None, "error": None, "resumes": 0,
        }
        self.save(job)
        self.event(job, "start", uids=request.get("uids"))
        return job

    def path(self, job_id: str) -> Path:
        if not re.fullmatch(r"[\w\-]+", job_id):
            raise ValueError("bad job id")
        return self.root / job_id

    def load(self, job_id: str) -> dict | None:
        return read_json(self.path(job_id) / "job.json")

    def save(self, job: dict) -> None:
        job["updated"] = time.time()
        atomic_write_json(self.path(job["id"]) / "job.json", job)

    def list(self) -> list[dict]:
        jobs = [j for p in self.root.glob("*/job.json") if (j := read_json(p))]
        return sorted(jobs, key=lambda j: j["created"], reverse=True)

    def mark_interrupted_on_startup(self) -> list[str]:
        """前回のプロセスが RUNNING のまま落ちたジョブを INTERRUPTED にする。"""
        ids = []
        for job in self.list():
            if job["status"] == RUNNING:
                job["status"] = INTERRUPTED
                self.save(job)
                self.event(job, "interrupted", reason="プロセス終了を検出")
                ids.append(job["id"])
        return ids

    def resumable(self) -> list[dict]:
        return [j for j in self.list() if j["status"] in RESUMABLE]

    # ---------------------------------------------------------- partial results
    def load_part(self, job: dict, unit: str) -> dict[str, str]:
        return read_json(self.path(job["id"]) / "parts" / f"{unit}.json", {}) or {}

    def save_part(self, job: dict, unit: str, entries: dict[str, str]) -> None:
        atomic_write_json(self.path(job["id"]) / "parts" / f"{unit}.json", entries)

    # ---------------------------------------------------------- history
    def event(self, job: dict, kind: str, **data) -> None:
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "job": job["id"], "event": kind, **data}
        with self.history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def read_history(self, job_id: str | None = None, limit: int = 300) -> list[dict]:
        out = []
        try:
            with self.history.open(encoding="utf-8") as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # 書き込み途中で落ちた最終行
                    if job_id is None or rec.get("job") == job_id:
                        out.append(rec)
        except FileNotFoundError:
            pass
        return out[-limit:]


def summarize(job: dict) -> dict:
    units = job["units"].values()
    total = sum(u["total"] for u in units)
    done = sum(u["done"] for u in units)
    return {"id": job["id"], "status": job["status"], "created": job["created"], "updated": job["updated"],
            "uids": job["request"].get("uids", []), "pack_name": job["request"].get("pack_name", ""),
            "done": done, "total": total, "units_done": sum(u["status"] == "done" for u in units),
            "units": len(job["units"]), "result": job["result"], "error": job["error"], "resumes": job["resumes"],
            "helper_pending": [u for u in job["order"] if u not in job["steps"]["helper_done"]]}
