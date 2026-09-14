"""Orchestrates one extraction pass: prefilter -> window -> LLM -> validate
-> apply -> record. This is the only place that should call
`extraction.client` -- never `hooks/common.py` (build plan section 11.2;
hooks must never call an LLM or block the caller's session).

Idempotence (build plan Milestone 5 acceptance criterion) is enforced via
`extraction_runs.UNIQUE(input_hash, prompt_version)`: the same delta against
the same neighborhood, under the same prompt version, short-circuits on a
cache hit rather than re-spending an LLM call and risking a different
result from model non-determinism.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError

from trace_mind.extraction import client, prefilter, prompt
from trace_mind.extraction.apply import ApplyResult, apply_envelope
from trace_mind.extraction.models import ExtractionEnvelope
from trace_mind.extraction.window import build_window

Status = str  # "skipped_prefilter" | "cached" | "api_error" | "invalid_schema" | "applied"


@dataclass
class ExtractionResult:
    status: Status
    signals: list[str] = field(default_factory=list)
    apply_result: ApplyResult | None = None
    error: str | None = None
    input_hash: str | None = None


def run_extraction(
    conn: sqlite3.Connection,
    project_id: str,
    session_id: str,
    new_event_ids: list[str],
    *,
    model: str = client.DEFAULT_MODEL,
    prompt_version: str = "p1",
) -> ExtractionResult:
    window = build_window(conn, project_id, session_id, new_event_ids, prompt_version=prompt_version)

    pf = prefilter.evaluate(window.events)
    if not pf.worth_extracting:
        return ExtractionResult(status="skipped_prefilter", signals=pf.signals)

    input_hash = window.input_hash
    cached = conn.execute(
        "SELECT * FROM extraction_runs WHERE input_hash = ? AND prompt_version = ?",
        (input_hash, prompt_version),
    ).fetchone()
    if cached is not None and cached["status"] == "applied":
        return ExtractionResult(status="cached", signals=pf.signals, input_hash=input_hash)

    extractor_version = f"llm:{model}:{prompt_version}"

    try:
        raw = client.call_json(prompt.system_prompt(), prompt.user_prompt(window), prompt.RESPONSE_SCHEMA, model=model)
    except client.ExtractionAPIError as exc:
        _record_run(conn, session_id, input_hash, prompt_version, model, status="api_error", output_json=None)
        return ExtractionResult(status="api_error", signals=pf.signals, error=str(exc), input_hash=input_hash)

    try:
        envelope = ExtractionEnvelope.model_validate(raw)
    except ValidationError as exc:
        _record_run(conn, session_id, input_hash, prompt_version, model, status="invalid_schema", output_json=json.dumps(raw))
        return ExtractionResult(status="invalid_schema", signals=pf.signals, error=str(exc), input_hash=input_hash)

    apply_result = apply_envelope(conn, window.project_id, window, envelope, extractor_version)
    _record_run(conn, session_id, input_hash, prompt_version, model, status="applied", output_json=json.dumps(raw))
    return ExtractionResult(status="applied", signals=pf.signals, apply_result=apply_result, input_hash=input_hash)


def _record_run(
    conn: sqlite3.Connection,
    session_id: str,
    input_hash: str,
    prompt_version: str,
    model: str,
    *,
    status: str,
    output_json: str | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """
            INSERT INTO extraction_runs (
                id, session_id, from_event_index, to_event_index, model,
                prompt_version, input_hash, output_json, status, created_at
            ) VALUES (?, ?, NULL, NULL, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(input_hash, prompt_version) DO UPDATE SET
                status = excluded.status,
                output_json = excluded.output_json,
                model = excluded.model,
                session_id = excluded.session_id,
                created_at = excluded.created_at
            """,
            (str(uuid.uuid4()), session_id, model, prompt_version, input_hash, output_json, status, now),
        )
