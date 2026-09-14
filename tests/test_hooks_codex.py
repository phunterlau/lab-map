"""Payload shapes below are copied from the real, public Codex hook JSON
schemas (reference/codex/codex-rs/hooks/schema/generated/*.schema.json),
not guessed -- see hooks/codex.py's docstring."""
import shutil
from pathlib import Path

from trace_mind.hooks import codex as codex_hooks
from trace_mind.hooks.common import db_path_for_cwd
from trace_mind.storage.db import connect

FIXTURE = Path(__file__).parent / "fixtures" / "codex" / "codex_research_memory_decision.jsonl"
SESSION_ID = "01a0aaaa-0000-7000-8000-000000000001"


def _session_start_payload(cwd: str) -> dict:
    return {
        "hook_event_name": "SessionStart",
        "session_id": SESSION_ID,
        "cwd": cwd,
        "model": "gpt-test",
        "permission_mode": "default",
        "source": "startup",
        "transcript_path": None,  # nothing written to disk yet at SessionStart
    }


def _stop_payload(cwd: str, transcript_path: str) -> dict:
    return {
        "hook_event_name": "Stop",
        "session_id": SESSION_ID,
        "cwd": cwd,
        "model": "gpt-test",
        "permission_mode": "default",
        "transcript_path": transcript_path,
        "turn_id": "t-0001",
        "last_assistant_message": "ok",
        "stop_hook_active": False,
    }


def test_session_start_with_null_transcript_path_just_logs(tmp_path):
    cwd = str(tmp_path)
    result = codex_hooks.handle(_session_start_payload(cwd))
    assert result.logged is True
    assert result.ingested_events == 0

    db_path = db_path_for_cwd(cwd)
    assert db_path.exists()
    conn = connect(db_path)
    row = conn.execute("SELECT event_name FROM hook_events").fetchone()
    assert row["event_name"] == "SessionStart"
    conn.close()


def test_stop_hook_ingests_the_transcript(tmp_path):
    cwd = str(tmp_path)
    transcript = tmp_path / "rollout.jsonl"
    shutil.copy(FIXTURE, transcript)

    result = codex_hooks.handle(_stop_payload(cwd, str(transcript)))
    assert result.logged is True
    assert result.ingested_events > 0

    conn = connect(db_path_for_cwd(cwd))
    count = conn.execute("SELECT COUNT(*) AS n FROM normalized_events").fetchone()["n"]
    assert count == result.ingested_events
    conn.close()


def test_repeated_stop_hook_is_idempotent(tmp_path):
    cwd = str(tmp_path)
    transcript = tmp_path / "rollout.jsonl"
    shutil.copy(FIXTURE, transcript)

    first = codex_hooks.handle(_stop_payload(cwd, str(transcript)))
    second = codex_hooks.handle(_stop_payload(cwd, str(transcript)))

    assert first.ingested_events > 0
    assert second.ingested_events == 0


def test_missing_transcript_file_does_not_raise(tmp_path):
    cwd = str(tmp_path)
    result = codex_hooks.handle(_stop_payload(cwd, str(tmp_path / "does-not-exist.jsonl")))
    assert result.logged is True
    assert result.ingested_events == 0


def test_missing_cwd_does_not_raise():
    result = codex_hooks.handle({"hook_event_name": "Stop", "session_id": "x"})
    assert result.logged is False
