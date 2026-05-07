from __future__ import annotations
from pathlib import Path

import pytest

import memsearch.summary_service as summary_service


def test_summary_service_runtime_appends_and_dedupes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_summarize_transcript(*args, **kwargs):
        return "- User asked for a summary\n- OpenCode updated the memory file"

    monkeypatch.setattr(summary_service, "summarize_transcript", fake_summarize_transcript)

    spool_dir = tmp_path / "service-spool"
    runtime = summary_service.SummaryServiceRuntime(spool_dir=spool_dir)
    try:
        payload = {
            "idempotency_key": "turn-123",
            "now": "12:34",
            "memory_file": str(tmp_path / ".memsearch" / "memory" / "2026-05-06.md"),
            "platform": "opencode",
            "agent_name": "OpenCode",
            "session_id": "session-1",
            "db_path": "/tmp/opencode.db",
            "content": "[Human]: Hello\n[OpenCode]: Hi",
        }

        result = runtime.submit(payload)
        assert result["job_id"] == "turn-123"
        assert "- User asked for a summary" in result["summary"]

        memory_file = Path(payload["memory_file"])
        text = memory_file.read_text(encoding="utf-8")
        assert text.count("### 12:34") == 1
        assert "OpenCode updated the memory file" in text

        again = runtime.submit(payload)
        assert again["job_id"] == "turn-123"
        assert memory_file.read_text(encoding="utf-8").count("### 12:34") == 1
    finally:
        runtime.close()
