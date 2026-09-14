import json
import re
import shutil
from pathlib import Path

import pytest

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import html_export
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
        conn, project_id, "O-0001", "option", "Graphiti",
        summary="Rejected for now.", status="rejected", evidence_event_ids=[evidence],
    )
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Markdown + MCP",
        summary="Chosen for the prototype.", status="chosen", evidence_event_ids=[evidence],
    )
    graph_repo.add_edge(
        conn, project_id, "O-0002", "O-0001", "CHOSEN_OVER", reason="lower operational cost",
        evidence_event_ids=[evidence],
    )
    yield conn, project_id
    conn.close()


@requires_dot
def test_html_contains_svg_and_no_external_requests(graph_db):
    conn, project_id = graph_db
    html = html_export.render_html(conn, project_id)

    assert "<svg" in html
    # Graphviz HTML-entity-escapes the SVG it emits (hyphen -> &#45;); a
    # real browser's element.textContent decodes this back to "O-0001",
    # which is what the click handler and the NODE_DATA JSON keys below
    # both use -- so check for the raw escaped form actually in the text.
    assert "<title>O&#45;0001</title>" in html
    assert "<title>O&#45;0002</title>" in html
    # self-contained: no CDN/external script or stylesheet references.
    # (SVG's xmlns="http://www.w3.org/2000/svg" is a namespace URI, not a
    # fetched resource, so a blanket "http" substring check would false-
    # positive on it -- check for actual external-resource tags instead.)
    assert "<script src=" not in html
    assert '<link rel="stylesheet"' not in html


@requires_dot
def test_html_embeds_valid_json_with_node_details_and_provenance(graph_db):
    conn, project_id = graph_db
    html = html_export.render_html(conn, project_id)

    match = re.search(r"const NODE_DATA = (\{.*?\});\s*\n", html, re.DOTALL)
    assert match, "NODE_DATA blob not found in output"
    data = json.loads(match.group(1))

    assert set(data.keys()) == {"O-0001", "O-0002"}

    graphiti = data["O-0001"]
    assert graphiti["status"] == "rejected"
    assert graphiti["summary"] == "Rejected for now."
    assert graphiti["provenance"], "expected at least one provenance entry"
    prov = graphiti["provenance"][0]
    assert prov["session_id"] == SESSION_ID
    assert prov["byte_start"] is not None
    assert prov["excerpt"]

    markdown = data["O-0002"]
    assert len(markdown["edges"]) == 1
    edge = markdown["edges"][0]
    assert edge["direction"] == "->"
    assert edge["type"] == "CHOSEN_OVER"
    assert edge["other"] == "O-0001"
    assert edge["reason"] == "lower operational cost"


def test_render_html_propagates_missing_dot_error(graph_db, monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(subprocess, "run", fake_run)
    conn, project_id = graph_db

    with pytest.raises(RuntimeError, match="brew install graphviz"):
        html_export.render_html(conn, project_id)
