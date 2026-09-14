"""trace-mind CLI. See docs/architecture.md for the full design.

V1 slice: manual, provider-explicit ingestion of one transcript file into the
local graph DB. `backfill`/discovery (build plan section 21) and the hook
entry points (section 11) come next; this command is what they'll both call
under the hood.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from trace_mind.normalize.pipeline import ingest_session
from trace_mind.storage.db import connect
from trace_mind.transcripts.claude import ClaudeTranscriptAdapter
from trace_mind.transcripts.codex import CodexTranscriptAdapter

app = typer.Typer(add_completion=False, no_args_is_help=True)

ADAPTERS = {
    "claude": ClaudeTranscriptAdapter(),
    "codex": CodexTranscriptAdapter(),
}

DEFAULT_DB = Path(".trace-mind/research.db")


@app.command()
def ingest(
    transcript: Annotated[Path, typer.Argument(help="Path to a Claude or Codex JSONL transcript file")],
    provider: Annotated[str, typer.Option(help="claude or codex")],
    session_id: Annotated[str, typer.Option(help="Stable external session/thread id")],
    db: Annotated[Path, typer.Option(help="SQLite DB path")] = DEFAULT_DB,
):
    """Incrementally ingest one transcript file's new bytes into the graph DB."""
    if provider not in ADAPTERS:
        typer.echo(f"unknown provider {provider!r}, expected one of {list(ADAPTERS)}", err=True)
        raise typer.Exit(1)
    if not transcript.exists():
        typer.echo(f"no such file: {transcript}", err=True)
        raise typer.Exit(1)

    adapter = ADAPTERS[provider]
    conn = connect(db)
    try:
        result = ingest_session(conn, adapter, session_id, transcript)
    finally:
        conn.close()

    typer.echo(f"ingested {result.new_events} new event(s) for session {session_id}" + (
        " (cursor reset: file was rotated/truncated)" if result.rotated else ""
    ))


def main() -> None:
    app()
