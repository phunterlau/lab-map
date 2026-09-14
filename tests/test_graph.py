from pathlib import Path

import pytest

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.storage.db import connect
from trace_mind.transcripts.codex import CodexTranscriptAdapter

FIXTURES = Path(__file__).parent / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex" / "codex_research_memory_decision.jsonl"
SESSION_ID = "01a0aaaa-0000-7000-8000-000000000001"


@pytest.fixture()
def db_with_events(tmp_path):
    conn = connect(tmp_path / "research.db")
    ingest_session(conn, CodexTranscriptAdapter(), SESSION_ID, CODEX_FIXTURE)
    yield conn
    conn.close()


def _first_event_id(conn, event_type: str) -> str:
    row = conn.execute(
        "SELECT id FROM normalized_events WHERE event_type = ? ORDER BY byte_start LIMIT 1", (event_type,)
    ).fetchone()
    assert row is not None, f"fixture has no {event_type} event"
    return row["id"]


def test_add_node_without_evidence_is_rejected(db_with_events):
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    with pytest.raises(graph_repo.MissingProvenanceError):
        graph_repo.add_node(db_with_events, project_id, "O-0001", "option", "Graphiti")


def test_add_node_with_unknown_evidence_id_is_rejected(db_with_events):
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    with pytest.raises(graph_repo.MissingProvenanceError):
        graph_repo.add_node(
            db_with_events, project_id, "O-0001", "option", "Graphiti",
            evidence_event_ids=["does-not-exist"],
        )


def test_edge_to_unknown_node_is_rejected(db_with_events):
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    evidence = _first_event_id(db_with_events, "user_message")
    graph_repo.add_node(
        db_with_events, project_id, "O-0001", "option", "Graphiti",
        evidence_event_ids=[evidence],
    )
    with pytest.raises(graph_repo.UnknownNodeError):
        graph_repo.add_edge(
            db_with_events, project_id, "O-0001", "D-9999", "CHOSEN_OVER",
            evidence_event_ids=[evidence],
        )


def test_deciduous_style_goal_option_decision_graph(db_with_events):
    """Represent the build plan's own canonical scenario (section 27) and
    verify every node carries provenance back to a real source event."""
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    goal_evidence = _first_event_id(db_with_events, "user_message")

    graph_repo.add_node(
        db_with_events, project_id, "Q-0001", "goal", "Persistent research memory",
        status="exploring", evidence_event_ids=[goal_evidence],
    )
    graph_repo.add_node(
        db_with_events, project_id, "O-0002", "option", "Graphiti",
        status="dormant", evidence_event_ids=[goal_evidence],
    )
    graph_repo.add_node(
        db_with_events, project_id, "O-0003", "option", "Markdown + MCP",
        status="chosen", evidence_event_ids=[goal_evidence],
    )
    graph_repo.add_node(
        db_with_events, project_id, "D-0005", "decision", "Prototype Markdown + MCP first",
        evidence_event_ids=[goal_evidence],
    )

    graph_repo.add_edge(
        db_with_events, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[goal_evidence]
    )
    graph_repo.add_edge(
        db_with_events, project_id, "Q-0001", "O-0003", "EXPLORES", evidence_event_ids=[goal_evidence]
    )
    graph_repo.add_edge(
        db_with_events, project_id, "D-0005", "O-0003", "CHOSEN_OVER", evidence_event_ids=[goal_evidence]
    )
    graph_repo.add_edge(
        db_with_events, project_id, "D-0005", "O-0002", "REJECTED_BECAUSE",
        reason="operationally heavy for current scale", evidence_event_ids=[goal_evidence],
    )

    graphiti = graph_repo.get_node(db_with_events, "O-0002")
    assert graphiti["status"] == "dormant"  # not "rejected" -- kept revisitable

    nodes = graph_repo.list_nodes(db_with_events, project_id)
    assert {n["id"] for n in nodes} == {"Q-0001", "O-0002", "O-0003", "D-0005"}

    for node in nodes:
        prov = graph_repo.provenance_for(db_with_events, "node", node["id"])
        assert len(prov) >= 1, f"{node['id']} has no provenance"
        assert prov[0]["byte_start"] is not None


def test_add_node_rejects_unknown_type(db_with_events):
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    evidence = _first_event_id(db_with_events, "user_message")
    with pytest.raises(graph_repo.InvalidOntologyError):
        graph_repo.add_node(
            db_with_events, project_id, "X-0001", "not_a_real_type", "Something",
            evidence_event_ids=[evidence],
        )


def test_add_edge_rejects_unknown_type(db_with_events):
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    evidence = _first_event_id(db_with_events, "user_message")
    graph_repo.add_node(
        db_with_events, project_id, "O-0001", "option", "Graphiti", evidence_event_ids=[evidence]
    )
    graph_repo.add_node(
        db_with_events, project_id, "O-0002", "option", "Markdown", evidence_event_ids=[evidence]
    )
    with pytest.raises(graph_repo.InvalidOntologyError):
        graph_repo.add_edge(
            db_with_events, project_id, "O-0001", "O-0002", "NOT_A_REAL_EDGE_TYPE",
            evidence_event_ids=[evidence],
        )


def test_add_node_is_idempotent_upsert(db_with_events):
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    evidence = _first_event_id(db_with_events, "user_message")

    graph_repo.add_node(
        db_with_events, project_id, "O-0001", "option", "Graphiti",
        status="exploring", evidence_event_ids=[evidence],
    )
    graph_repo.add_node(
        db_with_events, project_id, "O-0001", "option", "Graphiti",
        status="dormant", evidence_event_ids=[evidence],
    )

    node = graph_repo.get_node(db_with_events, "O-0001")
    assert node["status"] == "dormant"

    all_nodes = graph_repo.list_nodes(db_with_events, project_id)
    assert len([n for n in all_nodes if n["id"] == "O-0001"]) == 1
