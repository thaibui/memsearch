"""Shared helpers for turn-summary transport and memory-file writes."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

DEFAULT_SUMMARY_SERVICE_URL = "http://127.0.0.1:37777"


def canonical_job_id(payload: dict[str, Any]) -> str:
    """Derive a stable id for a turn-summary job."""
    key = payload.get("idempotency_key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def derive_spool_dir(memory_file: str | Path) -> Path:
    """Derive the local spool dir from a daily memory file path."""
    memory_path = Path(memory_file).expanduser()
    memory_dir = memory_path.parent
    return memory_dir.parent / "spool" / "summaries"


def job_paths(spool_dir: str | Path, job_id: str) -> tuple[Path, Path]:
    """Return pending/done paths for a job."""
    base = Path(spool_dir).expanduser()
    return base / "pending" / f"{job_id}.json", base / "done" / f"{job_id}.json"


def load_payload(path: str | Path) -> dict[str, Any]:
    """Load a JSON payload from disk."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_json_atomic(path: str | Path, data: dict[str, Any]) -> None:
    """Atomically write JSON to disk."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(target)


def load_json(path: str | Path) -> dict[str, Any]:
    """Load a JSON file or return {} if missing."""
    p = Path(path)
    if not p.is_file():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def write_pending_job(spool_dir: str | Path, payload: dict[str, Any]) -> Path:
    """Persist a pending job file and return its path."""
    job_id = canonical_job_id(payload)
    pending_path, _ = job_paths(spool_dir, job_id)
    save_json_atomic(pending_path, payload)
    return pending_path


def mark_job_done(spool_dir: str | Path, payload: dict[str, Any], summary: str) -> Path:
    """Record a finished job in the done directory."""
    job_id = canonical_job_id(payload)
    _, done_path = job_paths(spool_dir, job_id)
    record = {
        "job_id": job_id,
        "payload": payload,
        "summary": summary,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    save_json_atomic(done_path, record)
    return done_path


def append_summary_entry(memory_file: str | Path, payload: dict[str, Any], summary: str) -> None:
    """Append a summary to the daily memory file using the existing anchor format."""
    memory_path = Path(memory_file).expanduser()
    memory_path.parent.mkdir(parents=True, exist_ok=True)

    if "now" in payload and isinstance(payload["now"], str):
        now = payload["now"]
    else:
        now = datetime.now().strftime("%H:%M")

    if not memory_path.exists() or memory_path.stat().st_size == 0:
        memory_path.write_text(f"# {datetime.now().strftime('%Y-%m-%d')}\n\n", encoding="utf-8")

    anchor = ""
    session_id = payload.get("session_id", "")
    platform = payload.get("platform", "")
    if session_id:
        if platform == "opencode" and payload.get("db_path"):
            anchor = f"<!-- session:{session_id} db:{payload['db_path']} -->\n"
        elif payload.get("transcript_path"):
            anchor = f"<!-- session:{session_id} rollout:{payload['transcript_path']} -->\n"
        else:
            anchor = f"<!-- session:{session_id} -->\n"

    entry = f"### {now}\n{anchor}{summary}\n\n"
    with open(memory_path, "a", encoding="utf-8") as f:
        f.write(entry)


def post_turn_summary(service_url: str, payload: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
    """POST a turn summary request to the centralized service."""
    url = service_url.rstrip("/") + "/v1/summarize-turn"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with request.urlopen(req, timeout=max(1, timeout_ms) / 1000.0) as resp:
        data = resp.read().decode("utf-8")
        return json.loads(data)
    # unreachable


def service_healthy(service_url: str, timeout_ms: int = 2000) -> bool:
    """Check whether the summarization service is reachable."""
    url = service_url.rstrip("/") + "/health"
    try:
        with request.urlopen(url, timeout=max(1, timeout_ms) / 1000.0) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except error.URLError:
        return False
