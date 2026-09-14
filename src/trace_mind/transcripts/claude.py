"""Adapter for Claude Code's per-session JSONL transcript files
(~/.claude/projects/<project>/<session-uuid>.jsonl).

Schema notes (from reading real local transcripts, not from docs):
- Non-turn lines are tagged only by `type`: "mode", "permission-mode",
  "bridge-session", "file-history-snapshot". These carry no message content.
- Turn lines have `type` "user" or "assistant", a `message` object with
  `role` and `content`, plus `uuid`/`parentUuid` for the reply chain,
  `sessionId`, `timestamp`, `cwd`, `gitBranch`.
- `message.content` is a bare string for simple user turns, or a list of
  content blocks (`text`, `tool_use`, `tool_result`, ...) for assistant
  turns and tool-result carrying user turns.
- `isMeta: true` marks synthetic turns injected by the harness (slash
  command output, caveats) rather than text the user actually typed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from trace_mind.normalize.models import NormalizedEvent, TranscriptDelta

NON_TURN_TYPES = {"mode", "permission-mode", "bridge-session", "file-history-snapshot"}


class ClaudeTranscriptAdapter:
    provider = "claude"

    def detect(self, path: Path) -> bool:
        if path.suffix != ".jsonl":
            return False
        try:
            with open(path, "rb") as fh:
                first = fh.readline()
            obj = json.loads(first)
        except Exception:
            return False
        return "sessionId" in obj or obj.get("type") in NON_TURN_TYPES

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
                consumed = len(raw_line)
                if not raw_line.endswith(b"\n"):
                    # Incomplete trailing record (writer hasn't flushed the
                    # newline yet) -- leave it for the next pass.
                    break
                cursor += consumed
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    # Malformed line mid-stream: skip it but still advance
                    # past it, rather than getting stuck forever.
                    continue

                # `event_index` here is a block index local to this one line
                # (0 for a plain-text line, 0..N-1 for a multi-block message),
                # never a running count across the whole read call -- that
                # count would depend on where this particular read started,
                # which would make identity (and therefore dedup) unstable
                # across incremental reads of the same physical bytes.
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
    ts = _parse_ts(obj.get("timestamp"))
    turn_id = obj.get("uuid")
    raw = json.dumps(obj, sort_keys=True)

    if line_type in NON_TURN_TYPES:
        return [
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type="system", role=None, text=None, tool_name=None, timestamp=ts, turn_id=turn_id,
            )
        ]

    if line_type not in ("user", "assistant"):
        return [
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type="other", role=line_type, text=None, tool_name=None, timestamp=ts, turn_id=turn_id,
            )
        ]

    message = obj.get("message") or {}
    role = message.get("role", line_type)
    content = message.get("content")
    is_meta = bool(obj.get("isMeta"))

    events: list[NormalizedEvent] = []

    if isinstance(content, str):
        event_type = "system" if is_meta else ("user_message" if role == "user" else "assistant_message")
        events.append(
            _make(
                session_id, path, byte_start, byte_end, 0, raw,
                event_type=event_type, role=role, text=content, tool_name=None, timestamp=ts, turn_id=turn_id,
            )
        )
        return events

    if isinstance(content, list):
        idx = 0
        for block in content:
            block_type = block.get("type")
            if block_type == "text":
                events.append(
                    _make(
                        session_id, path, byte_start, byte_end, idx, raw,
                        event_type="assistant_message" if role == "assistant" else "user_message",
                        role=role, text=block.get("text"), tool_name=None, timestamp=ts, turn_id=turn_id,
                    )
                )
            elif block_type == "tool_use":
                events.append(
                    _make(
                        session_id, path, byte_start, byte_end, idx, raw,
                        event_type="tool_call", role=role, text=json.dumps(block.get("input", {})),
                        tool_name=block.get("name"), timestamp=ts, turn_id=turn_id,
                    )
                )
            elif block_type == "tool_result":
                result_content = block.get("content")
                text = result_content if isinstance(result_content, str) else json.dumps(result_content)
                events.append(
                    _make(
                        session_id, path, byte_start, byte_end, idx, raw,
                        event_type="tool_result", role=role, text=text,
                        tool_name=None, timestamp=ts, turn_id=turn_id,
                    )
                )
            else:
                events.append(
                    _make(
                        session_id, path, byte_start, byte_end, idx, raw,
                        event_type="other", role=role, text=None, tool_name=None, timestamp=ts, turn_id=turn_id,
                    )
                )
            idx += 1
        return events

    return [
        _make(
            session_id, path, byte_start, byte_end, 0, raw,
            event_type="other", role=role, text=None, tool_name=None, timestamp=ts, turn_id=turn_id,
        )
    ]


def _make(
    session_id, path, byte_start, byte_end, event_index, raw, *,
    event_type, role, text, tool_name, timestamp, turn_id,
) -> NormalizedEvent:
    # `event_index` is a block index local to this one line (stable
    # regardless of read boundaries); it disambiguates multiple events that
    # share the same byte range, e.g. text + tool_use in one assistant turn.
    content_hash = NormalizedEvent.compute_content_hash(str(path), byte_start, byte_end, raw + f"#{event_index}")
    return NormalizedEvent(
        id=f"claude:{content_hash}",
        provider="claude",
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
