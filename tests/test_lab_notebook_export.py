from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import lab_notebook_export
from trace_mind.storage.db import connect
from trace_mind.transcripts.codex import CodexTranscriptAdapter

FIXTURES = Path(__file__).parent / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex" / "codex_research_memory_decision.jsonl"
SESSION_ID = "01a0aaaa-0000-7000-8000-000000000001"


@pytest.fixture()
def evidence(tmp_path):
    conn = connect(tmp_path / "research.db")
    ingest_session(conn, CodexTranscriptAdapter(), SESSION_ID, CODEX_FIXTURE)
    project_id = graph_repo.ensure_project(conn, "/example/project")
    ev = conn.execute(
        "SELECT id FROM normalized_events WHERE event_type = 'user_message' ORDER BY byte_start LIMIT 1"
    ).fetchone()["id"]
    yield conn, project_id, ev
    conn.close()


def _later_evidence_id(conn) -> str:
    """A second, real, chronologically-LATER user_message event than the
    `evidence` fixture's own `ev` -- for tests that need to show a node
    genuinely became the most recent one (YOU-ARE-HERE is evidence-primary,
    not write-order, so citing the SAME evidence twice never moves it)."""
    return conn.execute(
        "SELECT id FROM normalized_events WHERE event_type = 'user_message' ORDER BY byte_start LIMIT 1 OFFSET 1"
    ).fetchone()["id"]


def _build_canonical_graph(conn, project_id, evidence_id):
    graph_repo.add_node(
        conn, project_id, "Q-0001", "goal", "Persistent research memory",
        status="exploring", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_node(
        # "rejected", not "dormant": a resolved sibling under Q-0001's
        # EXPLORES fan-out alongside chosen O-0003 would otherwise trip the
        # unresolved_sibling finding, even with a closing edge -- tests that
        # need an actual open loop add their own node/edge on top of this
        # baseline instead of relying on this shared fixture to have one.
        conn, project_id, "O-0002", "option", "Graphiti",
        status="rejected", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_node(
        conn, project_id, "O-0003", "option", "Markdown + MCP",
        status="chosen", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[evidence_id])
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0003", "EXPLORES", evidence_event_ids=[evidence_id])


def test_renders_a_card_per_node(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    out = lab_notebook_export.render_lab_notebook(conn, project_id)

    assert out.count("data-id=") == 3
    assert 'data-id="Q-0001"' in out
    assert 'data-id="O-0002"' in out
    assert 'data-id="O-0003"' in out


def test_renders_a_connector_per_edge(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    out = lab_notebook_export.render_lab_notebook(conn, project_id)

    assert out.count('class="connector"') == 2


def test_exactly_one_you_are_here_on_most_recently_updated_node(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    # Touch O-0002 again, citing genuinely later real evidence -- YOU ARE
    # HERE is evidence-primary, so re-citing the same event wouldn't move it.
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Graphiti",
        status="dormant", evidence_event_ids=[_later_evidence_id(conn)],
    )

    out = lab_notebook_export.render_lab_notebook(conn, project_id)

    assert out.count('class="here-tag"') == 1
    # The card containing the here-tag must be O-0002's.
    here_card = out[out.index('class="card here"') : out.index("</div></div>", out.index('class="card here"'))]
    assert "O-0002" in here_card


def test_unlinked_node_still_renders(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)
    graph_repo.add_node(
        conn, project_id, "O-9999", "option", "Totally unrelated stray node",
        status="open", evidence_event_ids=[ev],
    )

    out = lab_notebook_export.render_lab_notebook(conn, project_id)
    assert 'data-id="O-9999"' in out


def test_open_loops_panel_appears_only_when_there_are_loops(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    out_without = lab_notebook_export.render_lab_notebook(conn, project_id)
    # The CSS rule for #open-loops is always in the template; only the
    # rendered panel markup (id="open-loops") should be conditional.
    assert 'id="open-loops"' not in out_without

    graph_repo.add_node(
        conn, project_id, "R-0001", "revisit_condition", "Revisit later",
        status="open", evidence_event_ids=[ev],
    )
    out_with = lab_notebook_export.render_lab_notebook(conn, project_id)
    assert 'id="open-loops"' in out_with
    assert "Revisit later" in out_with


def test_tooltip_carries_summary_and_evidence_excerpt(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "Q-0001", "goal", "Root", status="exploring",
        summary="A longer explanation that doesn't fit in a card.",
        evidence_event_ids=[ev],
    )

    out = lab_notebook_export.render_lab_notebook(conn, project_id)
    assert "A longer explanation that doesn&#x27;t fit in a card." in out or \
        "A longer explanation that doesn't fit in a card." in out
    assert "evidence:" in out


def test_renders_with_no_goal_nodes(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "O-0001", "option", "Some option with no goal",
        status="open", evidence_event_ids=[ev],
    )
    graph_repo.add_node(
        conn, project_id, "D-0002", "decision", "A decision about it",
        status="chosen", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "D-0002", "O-0001", "CHOSEN_OVER", evidence_event_ids=[ev])

    out = lab_notebook_export.render_lab_notebook(conn, project_id)
    assert 'data-id="O-0001"' in out
    assert 'data-id="D-0002"' in out


def test_empty_project_renders_placeholder_message(evidence):
    conn, project_id, _ev = evidence
    out = lab_notebook_export.render_lab_notebook(conn, project_id)
    assert "no nodes" in out.lower()


def test_card_meta_shows_brief_relative_age(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    stale = datetime.now(timezone.utc) - timedelta(days=3, hours=2)
    conn.execute("UPDATE normalized_events SET timestamp = ? WHERE id = ?", (stale.isoformat(), ev))
    conn.commit()

    out = lab_notebook_export.render_lab_notebook(conn, project_id)
    assert "3d ago" in out


def test_open_loops_panel_surfaces_dormant_option_with_no_closing_edge(evidence):
    """The real gap found in the KGW forensic pass: a dormant option that
    was never formally closed out (no REJECTED_BECAUSE/CHOSEN_OVER/
    SUPERSEDES) used to be invisible to this panel entirely -- only open
    revisit_conditions were ever collected."""
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Alternative, never closed out",
        status="dormant", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "O-0002", "Q-0001", "RELATED_TO", evidence_event_ids=[ev])

    out = lab_notebook_export.render_lab_notebook(conn, project_id)
    assert 'id="open-loops"' in out
    assert "Alternative, never closed out" in out
    assert "dormant" in out.split('id="open-loops"')[1]
