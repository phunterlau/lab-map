"""trace-mind CLI. See docs/architecture.md for the full design.

V1 slice covers Milestones 0-4: manual, provider-explicit transcript
ingestion (`ingest`) plus a manual graph CLI (`node`, `edge`, `why`) backed
by the same SQLite DB and provenance table. Automated extraction
(Milestone 5) will call `graph.repository.add_node`/`add_edge` directly
with the same evidence-required contract; nothing here is extractor-only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated

import typer

from trace_mind.graph import repository as graph_repo
from trace_mind.hooks import codex as codex_hooks
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import graphviz_export, html_export
from trace_mind.storage.db import connect
from trace_mind.transcripts.claude import ClaudeTranscriptAdapter
from trace_mind.transcripts.codex import CodexTranscriptAdapter

app = typer.Typer(add_completion=False, no_args_is_help=True)
node_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage graph nodes.")
edge_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Manage graph edges.")
graph_app = typer.Typer(add_completion=False, no_args_is_help=True, help="Inspect/export the graph.")
app.add_typer(node_app, name="node")
app.add_typer(edge_app, name="edge")
app.add_typer(graph_app, name="graph")

ADAPTERS = {
    "claude": ClaudeTranscriptAdapter(),
    "codex": CodexTranscriptAdapter(),
}

DEFAULT_DB = Path(".trace-mind/research.db")
DbOpt = Annotated[Path, typer.Option(help="SQLite DB path")]
ProjectRootOpt = Annotated[str, typer.Option(help="Project root path (identifies which project's graph this is)")]
EvidenceOpt = Annotated[
    list[str] | None,
    typer.Option("--evidence", help="normalized_events.id this claim is sourced from; repeatable"),
]


@app.command()
def ingest(
    transcript: Annotated[Path, typer.Argument(help="Path to a Claude or Codex JSONL transcript file")],
    provider: Annotated[str, typer.Option(help="claude or codex")],
    session_id: Annotated[str, typer.Option(help="Stable external session/thread id")],
    db: DbOpt = DEFAULT_DB,
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


@app.command()
def hook(
    provider: Annotated[str, typer.Option(help="claude or codex")],
):
    """Hook entry point: reads one hook event as JSON from stdin.

    Fail-open by design (build plan section 11.2): this must never block or
    crash the caller's actual Claude/Codex session. Any error here is
    logged to stderr and swallowed; the process always exits 0. No LLM
    call happens in this path -- only stdin parsing, a `hook_events` log
    row, and opportunistic incremental ingest via `ingest_session()`.
    """
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}

        if provider == "codex":
            codex_hooks.handle(payload)
        elif provider == "claude":
            typer.echo("trace-mind: claude hook not yet implemented", err=True)
        else:
            typer.echo(f"trace-mind: unknown provider {provider!r}", err=True)
    except Exception as exc:  # fail open: never break the caller's session
        typer.echo(f"trace-mind hook error (ignored): {exc}", err=True)

    # No stdout output: every field in the Codex/Claude hook output schema
    # is optional with a safe default, so silence means "continue normally."


@node_app.command("add")
def node_add(
    node_id: Annotated[str, typer.Argument(help="e.g. O-0002")],
    type: Annotated[str, typer.Option(help="goal|hypothesis|option|evidence|experiment|decision|outcome|revisit_condition")],
    title: Annotated[str, typer.Option()],
    summary: Annotated[str | None, typer.Option()] = None,
    status: Annotated[str, typer.Option()] = "open",
    confidence: Annotated[float | None, typer.Option()] = None,
    evidence: EvidenceOpt = None,
    project_root: ProjectRootOpt = ".",
    allow_no_evidence: Annotated[bool, typer.Option(help="Only for structural nodes with no transcript source")] = False,
    db: DbOpt = DEFAULT_DB,
):
    """Create or update a graph node. Upserts on node_id."""
    conn = connect(db)
    try:
        project_id = graph_repo.ensure_project(conn, project_root)
        graph_repo.add_node(
            conn, project_id, node_id, type, title,
            summary=summary, status=status, confidence=confidence,
            evidence_event_ids=evidence, allow_no_evidence=allow_no_evidence,
        )
    except (graph_repo.MissingProvenanceError, graph_repo.UnknownNodeError, graph_repo.InvalidOntologyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(f"node {node_id} saved")


def _print_node(conn, node_id: str) -> None:
    """Shared by `node show` and `why` -- the decision -> evidence ->
    source-session chain."""
    node = graph_repo.get_node(conn, node_id)
    if node is None:
        typer.echo(f"no such node: {node_id}", err=True)
        raise typer.Exit(1)

    typer.echo(f"{node['id']} [{node['type']}] {node['title']}  (status={node['status']}, confidence={node['confidence']})")
    if node["summary"]:
        typer.echo(f"  {node['summary']}")

    edges = graph_repo.edges_touching(conn, node_id)
    if edges:
        typer.echo("edges:")
        for e in edges:
            arrow = "->" if e["source_node_id"] == node_id else "<-"
            other = e["target_node_id"] if e["source_node_id"] == node_id else e["source_node_id"]
            typer.echo(f"  {arrow} {e['type']} {other}" + (f"  ({e['reason']})" if e["reason"] else ""))

    prov = graph_repo.provenance_for(conn, "node", node_id)
    if prov:
        typer.echo("provenance:")
        for p in prov:
            excerpt = (p["event_text"] or "")[:100].replace("\n", " ")
            typer.echo(
                f"  session={p['event_session_id']} turn={p['turn_id']} "
                f"bytes=[{p['byte_start']}:{p['byte_end']}] :: {excerpt}"
            )


@node_app.command("show")
def node_show(node_id: str, db: DbOpt = DEFAULT_DB):
    """Show a node, its edges, and its provenance chain back to source events."""
    conn = connect(db)
    try:
        _print_node(conn, node_id)
    finally:
        conn.close()


@edge_app.command("add")
def edge_add(
    source_id: str,
    target_id: str,
    type: Annotated[str, typer.Argument(help="e.g. CHOSEN_OVER, REJECTED_BECAUSE, SUPPORTS")],
    reason: Annotated[str | None, typer.Option()] = None,
    confidence: Annotated[float | None, typer.Option()] = None,
    evidence: EvidenceOpt = None,
    project_root: ProjectRootOpt = ".",
    allow_no_evidence: Annotated[bool, typer.Option()] = False,
    db: DbOpt = DEFAULT_DB,
):
    """Create or update an edge between two existing nodes."""
    conn = connect(db)
    try:
        project_id = graph_repo.ensure_project(conn, project_root)
        edge_id = graph_repo.add_edge(
            conn, project_id, source_id, target_id, type,
            reason=reason, confidence=confidence,
            evidence_event_ids=evidence, allow_no_evidence=allow_no_evidence,
        )
    except (graph_repo.MissingProvenanceError, graph_repo.UnknownNodeError, graph_repo.InvalidOntologyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(f"edge {edge_id} saved")


@graph_app.command("export")
def graph_export(
    project_root: ProjectRootOpt = ".",
    out: Annotated[Path, typer.Option(help="Output file path")] = Path("research-map.dot"),
    png: Annotated[bool, typer.Option(help="Render PNG via the system `dot` binary instead of writing .dot")] = False,
    html: Annotated[bool, typer.Option(help="Render a self-contained interactive HTML viewer instead of .dot")] = False,
    db: DbOpt = DEFAULT_DB,
):
    """Export the graph as Graphviz DOT, PNG (--png), or an interactive HTML viewer (--html)."""
    if png and html:
        typer.echo("pass only one of --png / --html", err=True)
        raise typer.Exit(1)

    conn = connect(db)
    try:
        project_id = graph_repo.ensure_project(conn, project_root)
        if png:
            graphviz_export.render_png(conn, project_id, out)
        elif html:
            out.write_text(html_export.render_html(conn, project_id))
        else:
            out.write_text(graphviz_export.render_dot(conn, project_id))
    finally:
        conn.close()
    typer.echo(f"wrote {out}")


@app.command()
def why(node_id: str, db: DbOpt = DEFAULT_DB):
    """Alias for `node show` -- the decision -> evidence -> source-session chain."""
    conn = connect(db)
    try:
        _print_node(conn, node_id)
    finally:
        conn.close()


def main() -> None:
    app()
