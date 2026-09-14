"""Provider-neutral transcript event schema.

Nothing outside `transcripts/` should ever touch a raw Claude or Codex JSON
object. Every adapter must produce these types; every consumer only sees these
types. See build plan section 10.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

Provider = Literal["claude", "codex"]

EventType = Literal[
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "system",
    "compaction",
    "other",
]


class NormalizedEvent(BaseModel):
    id: str
    provider: Provider
    session_id: str
    turn_id: str | None = None
    event_index: int
    """Block index *within this one transcript record* (0 for a plain-text
    line; 0..N-1 for a multi-block message such as text + tool_use sharing
    one byte range). Deliberately NOT a running count across a whole
    read_delta() call -- that would make an event's identity depend on
    where the read happened to start, breaking idempotent re-reads. For
    chronological ordering across a session, sort by (byte_start,
    event_index)."""
    event_type: EventType
    role: str | None = None
    text: str | None = None
    tool_name: str | None = None
    timestamp: datetime | None = None

    transcript_path: str
    byte_start: int
    byte_end: int
    content_hash: str

    @staticmethod
    def compute_content_hash(session_id: str, byte_start: int, byte_end: int, raw: str) -> str:
        """Identity for dedup: (session, byte range, raw bytes).

        Keying purely on rendered text would collide two distinct short
        messages ("yes" said twice); keying on session+byte-range makes the
        cursor's re-read idempotent while still treating legitimate repeats
        as separate events.
        """
        h = hashlib.sha256()
        h.update(session_id.encode("utf-8"))
        h.update(str(byte_start).encode("utf-8"))
        h.update(str(byte_end).encode("utf-8"))
        h.update(raw.encode("utf-8"))
        return h.hexdigest()


class TranscriptDelta(BaseModel):
    """Result of reading new bytes from a transcript file."""

    events: list[NormalizedEvent] = Field(default_factory=list)
    next_byte_offset: int
    """Offset to resume from next time. Points just past the last complete
    record consumed; any trailing incomplete bytes are left for next pass."""
    file_size_at_read: int
    leading_hash: str | None = None
    """Hash of the first N bytes of the file at read time, to detect
    truncation/rotation even when the new size happens to match."""
