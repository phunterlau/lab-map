"""Adapter for Codex rollout JSONL files
(~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<thread-id>.jsonl).

Schema notes (from reading real local rollout files, not from docs):
- Every line is `{"timestamp", "ordinal", "type", "payload"}`.
- `type: "session_meta"` (always line 0) carries `session_id` (the durable
  thread id -- stable across compaction/resume) and `id` (this particular
  rollout file's own id, which can differ from `session_id` for subagent
  or continuation files).
- `type: "response_item"` payloads carry the actual conversation: `type`
  "message" (role user/assistant/developer, `content` list of
  `input_text`/`output_text` blocks) or "reasoning" (encrypted, skipped).
- `type: "event_msg"` carries turn lifecycle (`task_started`,
  `turn_completed`, ...) and rate-limit/info noise -- not message content.
- `type: "turn_context"`, `"world_state"`, `"token_usage_record"` are
  metadata, not conversation content.
- `type: "compacted"` marks a context-compaction boundary.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from trace_mind.normalize.models import NormalizedEvent, TranscriptDelta


class CodexTranscriptAdapter:
    provider = "codex"

    def detect(self, path: Path) -> bool:
        if path.suffix != ".jsonl":
            return False
        try:
            with open(path, "rb") as fh:
                first = fh.readline()
            obj = json.loads(first)
        except Exception:
            return False
        return obj.get("type") == "session_meta"

    def read_delta(self, path: Path, session_id: str, byte_offset: int) -> TranscriptDelta:
        events: list[NormalizedEvent] = []
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            file_size = fh.tell()
            fh.seek(0)
            leading = fh.read(256)
            leading_hash = hashlib.sha256(leading).hexdigest() if leading else None
            fh.seek(byte_offset)

            cursor = byte_offset
            while True:
                line_start = cursor
                raw_line = fh.readline()
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    break
                cursor += len(raw_line)
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                # Every Codex rollout line maps to exactly one event, so the
                # per-line block index is always 0 -- see claude.py for why
                # this must never be a running count across the whole read.
                events.extend(_normalize_line(obj, session_id, path, line_start, cursor))

        return TranscriptDelta(
            events=events,
            next_byte_offset=cursor,
            file_size_at_read=file_size,
            leading_hash=leading_hash,
        )


def _normalize_line(
    obj: dict, session_id: str, path: Path, byte_start: int, byte_end: int
) -> list[NormalizedEvent]:
    line_type = obj.get("type")
    payload = obj.get("payload") or {}
    ts = _parse_ts(obj.get("timestamp"))
    raw = json.dumps(obj, sort_keys=True)

    if line_type == "response_item":
        return _normalize_response_item(payload, session_id, path, byte_start, byte_end, ts, raw)

    if line_type == "compacted":
        return [
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type="compaction", role=None, text=payload.get("message"),
                tool_name=None, timestamp=ts, turn_id=None,
            )
        ]

    if line_type == "session_meta":
        return [
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type="system", role=None, text=None, tool_name=None, timestamp=ts, turn_id=None,
            )
        ]

    if line_type == "event_msg":
        # Lifecycle signal only (task_started/turn_completed/rate-limit
        # info/...). `last_agent_message` on a turn_completed event mirrors
        # text already captured as its own assistant `response_item` a few
        # lines earlier -- surfacing it here too would hand the extractor
        # the same evidence twice under two different event ids.
        turn_id = payload.get("turn_id")
        return [
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type="system", role=None, text=None,
                tool_name=None, timestamp=ts, turn_id=turn_id,
            )
        ]

    # turn_context, world_state, token_usage_record: metadata, not content.
    return [
        _make(
            session_id, path, byte_start, byte_end, 0, raw,
            event_type="other", role=None, text=None, tool_name=None, timestamp=ts, turn_id=None,
        )
    ]


def _normalize_response_item(
    payload: dict, session_id: str, path: Path, byte_start: int, byte_end: int,
    ts: datetime | None, raw: str,
) -> list[NormalizedEvent]:
    item_type = payload.get("type")
    turn_id = (payload.get("internal_chat_message_metadata_passthrough") or {}).get("turn_id")

    if item_type == "reasoning":
        return [
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type="other", role="assistant", text=None, tool_name=None,
                timestamp=ts, turn_id=turn_id,
            )
        ]

    if item_type != "message":
        return [
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type="other", role=payload.get("role"), text=None, tool_name=None,
                timestamp=ts, turn_id=turn_id,
            )
        ]

    role = payload.get("role")
    text_parts = [
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") in ("input_text", "output_text")
    ]
    text = "\n".join(p for p in text_parts if p)

    if role == "developer":
        event_type = "system"
    elif role == "user":
        event_type = "user_message"
    elif role == "assistant":
        event_type = "assistant_message"
    else:
        event_type = "other"

    return [
        _make(
            session_id, path, byte_start, byte_end, 0, raw,
            event_type=event_type, role=role, text=text or None, tool_name=None,
            timestamp=ts, turn_id=turn_id,
        )
    ]


def _make(
    session_id, path, byte_start, byte_end, event_index, raw, *,
    event_type, role, text, tool_name, timestamp, turn_id,
) -> NormalizedEvent:
    content_hash = NormalizedEvent.compute_content_hash(session_id, byte_start, byte_end, raw + f"#{event_index}")
    return NormalizedEvent(
        id=f"codex:{session_id}:{content_hash[:16]}",
        provider="codex",
        session_id=session_id,
        turn_id=turn_id,
        event_index=event_index,
        event_type=event_type,
        role=role,
        text=text,
        tool_name=tool_name,
        timestamp=timestamp,
        transcript_path=str(path),
        byte_start=byte_start,
        byte_end=byte_end,
        content_hash=content_hash,
    )


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
