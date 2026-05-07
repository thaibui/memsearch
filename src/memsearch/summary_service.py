"""Centralized turn summarization service."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from typing import Any
from urllib.parse import urlparse

from .config import MemSearchConfig, resolve_config
from .summarize import load_prompt_template, summarize_transcript
from .turns import (
    append_summary_entry,
    canonical_job_id,
    job_paths,
    load_json,
    mark_job_done,
    write_pending_job,
)


def _fallback_summary(payload: dict[str, Any]) -> str:
    content = (payload.get("content") or "").strip()
    user_question = (payload.get("user_question") or "").strip()
    last_msg = (payload.get("last_msg") or "").strip()

    if user_question and last_msg:
        return f"- User asked: {user_question}\n- {payload.get('agent_name', 'Agent')}: {last_msg[:800]}"
    if content:
        first_line = content.splitlines()[0][:800]
        return f"- User asked: {first_line}"
    return "- Turn captured, but no readable content was available."


@dataclass
class SummaryJob:
    job_id: str
    payload: dict[str, Any]
    pending_path: Path
    done_path: Path
    event: threading.Event = field(default_factory=threading.Event, repr=False)
    result: dict[str, Any] | None = None
    error: str | None = None


class SummaryServiceRuntime:
    """Durable queue + worker for centralized turn summarization."""

    def __init__(self, spool_dir: str | Path, cfg: MemSearchConfig | None = None):
        self.cfg = cfg or resolve_config()
        self.spool_dir = Path(spool_dir).expanduser()
        self.pending_dir = self.spool_dir / "pending"
        self.done_dir = self.spool_dir / "done"
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        self.done_dir.mkdir(parents=True, exist_ok=True)

        self._queue: Queue[str] = Queue()
        self._jobs: dict[str, SummaryJob] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="memsearch-summary-worker", daemon=True)
        self._worker.start()
        self._rehydrate_pending_jobs()

    def close(self) -> None:
        self._stop.set()
        self._queue.put("__stop__")
        self._worker.join(timeout=5)

    def _rehydrate_pending_jobs(self) -> None:
        for pending in sorted(self.pending_dir.glob("*.json")):
            try:
                payload = load_json(pending)
                if not payload:
                    continue
                job_id = canonical_job_id(payload)
                _, done_path = job_paths(self.spool_dir, job_id)
                if done_path.exists():
                    continue
                self._enqueue_job(payload, pending_path=pending, done_path=done_path, wait=False)
            except Exception:
                continue

    def _enqueue_job(
        self,
        payload: dict[str, Any],
        *,
        pending_path: Path | None = None,
        done_path: Path | None = None,
        wait: bool = True,
    ) -> dict[str, Any]:
        job_id = canonical_job_id(payload)
        pending_path, done_path = pending_path or job_paths(self.spool_dir, job_id)[0], done_path or job_paths(
            self.spool_dir, job_id
        )[1]

        with self._lock:
            if done_path.exists():
                cached = load_json(done_path)
                if cached:
                    return cached
            job = self._jobs.get(job_id)
            if job is None:
                job = SummaryJob(job_id=job_id, payload=payload, pending_path=pending_path, done_path=done_path)
                self._jobs[job_id] = job
                write_pending_job(self.spool_dir, payload)
                self._queue.put(job_id)

        if wait:
            job.event.wait()
            if job.result is not None:
                return job.result
            if job.error is not None:
                raise RuntimeError(job.error)
            return {"job_id": job_id, "summary": ""}
        return {"job_id": job_id, "summary": ""}

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._enqueue_job(payload, wait=True)

    def flush_pending(self, memory_file: str | Path | None = None) -> int:
        """Drain queued jobs that are still pending on disk."""
        if memory_file is not None:
            spool_dir = Path(memory_file).expanduser().parent.parent / "spool" / "summaries"
        else:
            spool_dir = self.spool_dir
        pending_dir = spool_dir / "pending"
        count = 0
        for pending in sorted(pending_dir.glob("*.json")):
            payload = load_json(pending)
            if not payload:
                continue
            if memory_file and payload.get("memory_file") and Path(payload["memory_file"]).expanduser() != Path(memory_file).expanduser():
                continue
            self._enqueue_job(payload, pending_path=pending, wait=True)
            count += 1
        return count

    def _write_result(self, job: SummaryJob, summary: str) -> dict[str, Any]:
        result = {
            "job_id": job.job_id,
            "summary": summary,
            "memory_file": job.payload.get("memory_file", ""),
            "session_id": job.payload.get("session_id", ""),
            "platform": job.payload.get("platform", ""),
        }
        mark_job_done(self.spool_dir, job.payload, summary)
        try:
            job.pending_path.unlink(missing_ok=True)
        except Exception:
            pass
        append_summary_entry(job.payload["memory_file"], job.payload, summary)
        return result

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job_id = self._queue.get()
            if job_id == "__stop__":
                break
            job = self._jobs.get(job_id)
            if job is None:
                continue
            try:
                template = load_prompt_template(self.cfg.prompts.summarize) if self.cfg.prompts.summarize else None
                summary = asyncio.run(
                    summarize_transcript(
                        job.payload.get("content", ""),
                        agent_name=job.payload.get("agent_name", "assistant"),
                        llm_provider=self.cfg.llm.provider or self.cfg.compact.llm_provider or "openai",
                        llm_model=self.cfg.llm.model or self.cfg.compact.llm_model or None,
                        prompt_template=template,
                        base_url=self.cfg.llm.base_url or self.cfg.compact.base_url or None,
                        api_key=self.cfg.llm.api_key or self.cfg.compact.api_key or None,
                    )
                )
                summary = (summary or "").strip() or _fallback_summary(job.payload)
            except Exception:
                summary = _fallback_summary(job.payload)

            try:
                result = self._write_result(job, summary)
                job.result = result
            except Exception as exc:
                job.error = str(exc)
            finally:
                with self._lock:
                    self._jobs.pop(job_id, None)
                job.event.set()


class SummaryRequestHandler(BaseHTTPRequestHandler):
    server_version = "memsearch-summary/0.1"

    def _write_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @property
    def runtime(self) -> SummaryServiceRuntime:
        return self.server.runtime  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self._write_json(
                200,
                {
                    "ok": True,
                    "pending": len(list(self.runtime.pending_dir.glob("*.json"))),
                    "done": len(list(self.runtime.done_dir.glob("*.json"))),
                },
            )
            return
        self._write_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/v1/summarize-turn":
            self._write_json(404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        except Exception as exc:
            self._write_json(400, {"ok": False, "error": f"invalid JSON: {exc}"})
            return

        memory_file = payload.get("memory_file")
        if not memory_file:
            self._write_json(400, {"ok": False, "error": "memory_file is required"})
            return

        try:
            result = self.runtime.submit(payload)
        except Exception as exc:
            self._write_json(500, {"ok": False, "error": str(exc)})
            return

        self._write_json(200, {"ok": True, **result})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        return


class SummaryHTTPServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], runtime: SummaryServiceRuntime):
        super().__init__(server_address, SummaryRequestHandler)
        self.runtime = runtime


def serve_summary_service(host: str, port: int, spool_dir: str | Path, cfg: MemSearchConfig | None = None) -> None:
    """Run the centralized summarization service until interrupted."""
    runtime = SummaryServiceRuntime(spool_dir=spool_dir, cfg=cfg)
    server = SummaryHTTPServer((host, port), runtime)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        runtime.close()
        server.server_close()
