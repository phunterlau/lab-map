import shutil
from pathlib import Path

import pytest

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import graphviz_export
from trace_mind.storage.db import connect
from trace_mind.transcripts.codex import CodexTranscriptAdapter

FIXTURES = Path(__file__).parent / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex" / "codex_research_memory_decision.jsonl"
SESSION_ID = "01a0aaaa-0000-7000-8000-000000000001"

requires_dot = pytest.mark.skipif(shutil.which("dot") is None, reason="graphviz `dot` binary not installed")


@pytest.fixture()
def graph_db(tmp_path):
    conn = connect(tmp_path / "research.db")
    ingest_session(conn, CodexTranscriptAdapter(), SESSION_ID, CODEX_FIXTURE)
    project_id = graph_repo.ensure_project(conn, "/example/project")

    evidence = conn.execute(
        "SELECT id FROM normalized_events WHERE event_type = 'user_message' ORDER BY byte_start LIMIT 1"
    ).fetchone()["id"]

    graph_repo.add_node(
        conn, project_id, "O-0001", "option", "Graphiti", status="rejected", evidence_event_ids=[evidence]
    )
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Markdown + MCP", status="chosen", evidence_event_ids=[evidence]
    )
    graph_repo.add_edge(
        conn, project_id, "O-0002", "O-0001", "CHOSEN_OVER", reason="lower operational cost",
        evidence_event_ids=[evidence],
    )
    yield conn, project_id
    conn.close()


def test_render_dot_contains_nodes_and_edges(graph_db):
    conn, project_id = graph_db
    dot = graphviz_export.render_dot(conn, project_id)

    assert "digraph" in dot
    assert '"O-0001"' in dot
    assert '"O-0002"' in dot
    assert "CHOSEN_OVER" in dot
    # status/type -> color/shape mapping applied
    assert graphviz_export._STATUS_COLOR["rejected"] in dot
    assert graphviz_export._TYPE_SHAPE["option"] in dot


@requires_dot
def test_render_png_writes_a_valid_png_file(graph_db, tmp_path):
    conn, project_id = graph_db
    out_path = tmp_path / "graph.png"
    graphviz_export.render_png(conn, project_id, out_path)

    assert out_path.exists()
    assert out_path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@requires_dot
def test_render_svg_embeds_node_ids_as_titles(graph_db):
    """Graphviz HTML-entity-escapes the SVG source it emits (e.g. the
    hyphen in "O-0001" becomes "O&#45;0001") -- a real browser's
    `element.textContent` decodes that automatically, which is what
    html_export.py's click handler relies on, so this checks for the raw
    escaped form actually present in the SVG text, not the decoded form.
    """
    conn, project_id = graph_db
    svg = graphviz_export.render_svg(conn, project_id)

    assert svg.strip().startswith("<?xml") or "<svg" in svg
    assert "<title>O&#45;0001</title>" in svg
    assert "<title>O&#45;0002</title>" in svg


def test_run_dot_raises_clear_error_when_dot_missing(monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="brew install graphviz"):
        graphviz_export._run_dot("digraph {}", "png")
