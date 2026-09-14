"""Codex hook entry point.

Field names below come from the real Codex hook JSON schemas at
reference/codex/codex-rs/hooks/schema/generated/*.command.input.schema.json
(SessionStart, Stop, PreCompact, PostCompact, SessionEnd all share
`hook_event_name`, `session_id`, `transcript_path` (nullable), `cwd`) --
not guessed, not from prose docs.
"""
from __future__ import annotations

from trace_mind.hooks.common import HookResult, handle_hook_event
from trace_mind.transcripts.codex import CodexTranscriptAdapter

_ADAPTER = CodexTranscriptAdapter()


def handle(payload: dict) -> HookResult:
    return handle_hook_event("codex", _ADAPTER, payload)
