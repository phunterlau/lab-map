"""Shared hook-handling logic (build plan section 11).

A hook must: parse stdin, log the raw event, trigger incremental ingest
opportunistically, and return immediately. It must NEVER call an LLM, parse
the whole transcript from scratch, run embeddings, or block the coding
agent -- and it must fail open: any exception here is caught by the caller
(cli.py) and swallowed after logging, because a crash in this process must
never stop the user's actual Codex/Claude session.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trace_mind.normalize.pipeline import ingest_session
from trace_mind.storage.db import connect
from trace_mind.transcripts.base import TranscriptAdapter

DB_DIRNAME = ".trace-mind"
DB_FILENAME = "research.db"


class HookResult:
    def __init__(self, logged: bool, ingested_events: int, note: str = ""):
        self.logged = logged
        self.ingested_events = ingested_events
        self.note = note


def db_path_for_cwd(cwd: str) -> Path:
    """The project-local DB path (build plan section 7): derived from the
    `cwd` the hook payload reports, not this process's own cwd -- those can
    differ depending on how the agent invokes hook commands."""
    return Path(cwd) / DB_DIRNAME / DB_FILENAME


def handle_hook_event(
    provider: str,
    adapter: TranscriptAdapter,
    payload: dict,
    *,
    event_name_field: str = "hook_event_name",
    session_id_field: str = "session_id",
    transcript_path_field: str = "transcript_path",
    cwd_field: str = "cwd",
) -> HookResult:
    event_name = payload.get(event_name_field)
    session_id = payload.get(session_id_field)
    transcript_path = payload.get(transcript_path_field)
    cwd = payload.get(cwd_field)

    if not cwd:
        return HookResult(logged=False, ingested_events=0, note="no cwd in payload; nothing to do")

    db_path = db_path_for_cwd(cwd)
    conn = connect(db_path)
    try:
        _log_hook_event(conn, provider, event_name or "unknown", payload)

        if not session_id or not transcript_path:
            return HookResult(logged=True, ingested_events=0, note="no session_id/transcript_path yet")

        path = Path(transcript_path)
        if not path.exists():
            return HookResult(logged=True, ingested_events=0, note=f"transcript not on disk yet: {path}")

        result = ingest_session(conn, adapter, session_id, path)
        return HookResult(logged=True, ingested_events=result.new_events)
    finally:
        conn.close()


def _log_hook_event(conn: sqlite3.Connection, provider: str, event_name: str, payload: dict) -> None:
    conn.execute(
        "INSERT INTO hook_events (provider, event_name, payload_json, created_at, processed_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (provider, event_name, json.dumps(payload), _now(), _now()),
    )
    conn.commit()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
