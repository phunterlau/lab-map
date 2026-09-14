"""Cursor-based incremental ingestion (build plan section 12).

`ingest_session` is the one place that ties adapter + cursor + storage
together. Event insertion and cursor advancement happen in a single SQLite
transaction so a crash between the two can never happen: either both landed
or neither did, and the next run re-reads from the last committed offset.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from trace_mind.normalize.models import NormalizedEvent
from trace_mind.transcripts.base import TranscriptAdapter

logger = logging.getLogger(__name__)

PARSER_VERSION = "v1"


class IngestResult:
    def __init__(self, new_events: int, rotated: bool):
        self.new_events = new_events
        self.rotated = rotated


def ingest_session(
    conn: sqlite3.Connection,
    adapter: TranscriptAdapter,
    session_id: str,
    transcript_path: Path,
) -> IngestResult:
    cursor_row = conn.execute(
        "SELECT byte_offset, file_size, leading_hash FROM transcript_cursors WHERE session_id = ?",
        (session_id,),
    ).fetchone()

    byte_offset = cursor_row["byte_offset"] if cursor_row else 0
    rotated = False

    if cursor_row is not None:
        current_size = transcript_path.stat().st_size
        if current_size < cursor_row["file_size"]:
            # File shrank: truncation or rotation. The old byte offset is no
            # longer meaningful against this file's content, so start over.
            logger.warning(
                "transcript %s shrank (%d -> %d bytes); resetting cursor for session %s",
                transcript_path, cursor_row["file_size"], current_size, session_id,
            )
            byte_offset = 0
            rotated = True

    delta = adapter.read_delta(transcript_path, session_id, byte_offset)

    if (
        cursor_row is not None
        and not rotated
        and cursor_row["leading_hash"] is not None
        and delta.leading_hash is not None
        and cursor_row["leading_hash"] != delta.leading_hash
    ):
        # Same or greater size, but the file's head changed underneath us
        # (e.g. rewritten by a compaction pass) -- re-ingest from scratch.
        logger.warning(
            "transcript %s leading bytes changed; resetting cursor for session %s",
            transcript_path, session_id,
        )
        delta = adapter.read_delta(transcript_path, session_id, 0)
        rotated = True

    now = datetime.now(timezone.utc).isoformat()
    new_events = 0

    with conn:
        if rotated:
            # The old rows' byte ranges belong to a file identity that no
            # longer exists (truncated/rewritten). Keeping them would leave
            # phantom evidence in the graph with byte offsets that no
            # longer point at anything real. Since read_delta above was
            # re-run from offset 0, deleting and re-inserting is safe and
            # keeps event insertion + cursor advance in the one transaction.
            conn.execute("DELETE FROM normalized_events WHERE session_id = ?", (session_id,))

        for event in delta.events:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO normalized_events (
                    id, session_id, provider, turn_id, event_index, role,
                    event_type, text, tool_name, timestamp, transcript_path,
                    byte_start, byte_end, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                _event_row(event),
            )
            if cur.rowcount:
                new_events += 1

        conn.execute(
            """
            INSERT INTO transcript_cursors (
                session_id, transcript_path, byte_offset, file_size, leading_hash,
                parser_version, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                transcript_path = excluded.transcript_path,
                byte_offset = excluded.byte_offset,
                file_size = excluded.file_size,
                leading_hash = excluded.leading_hash,
                parser_version = excluded.parser_version,
                updated_at = excluded.updated_at
            """,
            (
                session_id,
                str(transcript_path),
                delta.next_byte_offset,
                delta.file_size_at_read,
                delta.leading_hash,
                PARSER_VERSION,
                now,
            ),
        )

    return IngestResult(new_events=new_events, rotated=rotated)


def _event_row(event: NormalizedEvent) -> tuple:
    return (
        event.id,
        event.session_id,
        event.provider,
        event.turn_id,
        event.event_index,
        event.role,
        event.event_type,
        event.text,
        event.tool_name,
        event.timestamp.isoformat() if event.timestamp else None,
        event.transcript_path,
        event.byte_start,
        event.byte_end,
        event.content_hash,
    )
