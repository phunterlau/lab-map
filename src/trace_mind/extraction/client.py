"""OpenAI backend for the LLM extractor (per user direction: GPT-5.6-Luna via
`OPENAI_API_KEY`, not the Anthropic API -- see docs/architecture.md).

Deliberately the only backend implemented. The alternative the user
mentioned (shell out to Codex/Claude as a coding-agent task) is a different
enough call shape -- a whole agent invocation vs. one structured-output
call -- that building an abstraction for it before it exists would be
speculative. If it's ever added, this module's one function is the seam.

Uses the Responses API (`/v1/responses`) with a JSON-schema `text.format`
(OpenAI Structured Outputs) -- confirmed against
https://developers.openai.com/api/docs/models/gpt-5.6-luna (Responses AND
Chat Completions both supported, `structured_outputs` listed as a supported
feature) and the Responses API reference (`input`: list of {role, content}
messages; JSON-schema shape nested under `text.format`).
"""
from __future__ import annotations

import json
import os

import httpx

API_BASE = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-5.6-luna"


class ExtractionAPIError(RuntimeError):
    pass


def call_json(
    system_prompt: str,
    user_prompt: str,
    schema: dict,
    *,
    model: str = DEFAULT_MODEL,
    schema_name: str = "extraction_envelope",
    timeout: float = 90.0,
) -> dict:
    """Call the model, return the parsed JSON body. Raises
    `ExtractionAPIError` on transport/HTTP failure or if the response isn't
    parseable JSON -- callers should treat that the same as "extraction
    failed for this window", not crash the caller's session (this is never
    called from hook path; see extraction/runner.py)."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise ExtractionAPIError("OPENAI_API_KEY is not set")

    body = {
        "model": model,
        "input": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "schema": schema,
                "strict": False,
            }
        },
    }

    try:
        resp = httpx.post(
            f"{API_BASE}/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise ExtractionAPIError(f"OpenAI request failed: {exc}") from exc

    data = resp.json()
    text = _output_text(data)
    if text is None:
        raise ExtractionAPIError(f"no output text in response: {json.dumps(data)[:500]}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionAPIError(f"model output was not valid JSON: {exc}") from exc


def _output_text(data: dict) -> str | None:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    for item in data.get("output", []):
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") in ("output_text", "text") and "text" in block:
                return block["text"]
    return None
