from pathlib import Path

import pytest

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import lattice_layout
from trace_mind.projection.tree import build_forest
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


def _build_deep_tree(conn, project_id, ev):
    """Same shape as test_ascii_export.py's indentation regression fixture:
    Q-0001 -> A, B; A -> A1, A2; A1 -> A1x. A is NOT last among Q-0001's
    children (B follows); A1 is NOT last among A's children (A2 follows)."""
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "A", "option", "A", status="open", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "B", "option", "B", status="open", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "A1", "option", "A1", status="open", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "A2", "option", "A2", status="open", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "A1x", "option", "A1x", status="open", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "Q-0001", "A", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "Q-0001", "B", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "A", "A1", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "A", "A2", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "A1", "A1x", "EXPLORES", evidence_event_ids=[ev])


def test_all_columns_are_globally_unique(evidence):
    conn, project_id, ev = evidence
    _build_deep_tree(conn, project_id, ev)

    tree = build_forest(conn, project_id)
    positions = lattice_layout.compute_positions(tree)

    cols = [p.col for p in positions.values()]
    assert len(cols) == len(set(cols)) == 6


def test_row_equals_bfs_depth(evidence):
    conn, project_id, ev = evidence
    _build_deep_tree(conn, project_id, ev)

    tree = build_forest(conn, project_id)
    positions = lattice_layout.compute_positions(tree)

    assert positions["Q-0001"].row == 0
    assert positions["A"].row == 1
    assert positions["B"].row == 1
    assert positions["A1"].row == 2
    assert positions["A2"].row == 2
    assert positions["A1x"].row == 3


def test_subtree_columns_are_contiguous_and_disjoint_from_siblings(evidence):
    """DFS pre-order visits A's whole subtree (A, A1, A2, A1x) before B, so
    A's subtree must occupy a contiguous column range that doesn't include
    B's column -- the property the whole no-overlap design rests on."""
    conn, project_id, ev = evidence
    _build_deep_tree(conn, project_id, ev)

    tree = build_forest(conn, project_id)
    positions = lattice_layout.compute_positions(tree)

    a_subtree_cols = sorted(positions[n].col for n in ("A", "A1", "A2", "A1x"))
    assert a_subtree_cols == list(range(a_subtree_cols[0], a_subtree_cols[0] + 4))
    assert positions["B"].col not in a_subtree_cols
    assert positions["Q-0001"].col not in a_subtree_cols


def test_unlinked_forest_gets_separate_band(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "O-9999", "option", "Stray", status="open", evidence_event_ids=[ev])

    tree = build_forest(conn, project_id)
    positions = lattice_layout.compute_positions(tree)

    assert positions["Q-0001"].band == lattice_layout.MAIN_BAND
    assert positions["O-9999"].band == lattice_layout.UNLINKED_BAND
    # Bands don't share columns either -- confirms the global (not
    # per-band) counter, so nothing from the two bands can render at the
    # same (row, col) even though both start counting rows from 0.
    assert positions["Q-0001"].col != positions["O-9999"].col


def test_empty_project_yields_no_positions(evidence):
    conn, project_id, _ev = evidence
    tree = build_forest(conn, project_id)
    assert lattice_layout.compute_positions(tree) == {}


def test_connector_waypoints_never_cross_a_node_row(evidence):
    """The structural no-overlap guarantee, proven directly rather than by
    eyeballing rendered output: every horizontal segment of a connector
    elbow must lie entirely on an odd (gutter) grid-row, and every vertical
    segment must span exactly one grid-row unit -- together these mean a
    connector can only ever touch a node row at its own two declared
    endpoints, never pass through a third node's row."""
    conn, project_id, ev = evidence
    _build_deep_tree(conn, project_id, ev)

    tree = build_forest(conn, project_id)
    positions = lattice_layout.compute_positions(tree)

    edges_checked = 0
    for _root_id, parent_of in tree.forest:
        for child_id, (parent_id, _edge) in parent_of.items():
            waypoints = lattice_layout.connector_waypoints(positions[parent_id], positions[child_id])
            for (r1, c1), (r2, c2) in zip(waypoints, waypoints[1:]):
                if r1 == r2:  # horizontal segment
                    assert r1 % 2 == 1, f"horizontal segment at row {r1} is not a gutter row"
                else:  # vertical segment
                    assert c1 == c2
                    assert abs(r2 - r1) == 1, f"vertical segment spans {abs(r2 - r1)} grid-rows, not 1"
            edges_checked += 1

    assert edges_checked == 5  # 5 edges in _build_deep_tree
