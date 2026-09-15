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


def test_add_node_rejects_id_already_used_by_a_different_project(db_with_events):
    """Regression: found via a real headless test running extraction against
    9 real Codex sessions sharing one DB -- graph_nodes.id is a single
    global primary key, so two projects independently minting the same id
    (e.g. both "A-0001") used to silently let the second overwrite the
    first's content, with the row's project_id still pointing at whichever
    project created it first. This must now fail loudly instead."""
    project_a = graph_repo.ensure_project(db_with_events, "/example/project-a")
    project_b = graph_repo.ensure_project(db_with_events, "/example/project-b")
    evidence = _first_event_id(db_with_events, "user_message")

    graph_repo.add_node(
        db_with_events, project_a, "A-0001", "action", "Project A's action", evidence_event_ids=[evidence],
    )
    with pytest.raises(graph_repo.CrossProjectIdCollisionError):
        graph_repo.add_node(
            db_with_events, project_b, "A-0001", "action", "Project B's action", evidence_event_ids=[evidence],
        )

    # Project A's node must be untouched by the rejected write.
    node = graph_repo.get_node(db_with_events, "A-0001")
    assert node["project_id"] == project_a
    assert node["title"] == "Project A's action"


def test_add_node_same_project_same_id_still_upserts(db_with_events):
    """The guard must only block a DIFFERENT project reusing an id --
    updating your OWN node by id (the existing, intended upsert behavior)
    must keep working."""
    project_id = graph_repo.ensure_project(db_with_events, "/example/project")
    evidence = _first_event_id(db_with_events, "user_message")

    graph_repo.add_node(
        db_with_events, project_id, "A-0001", "action", "v1", status="open", evidence_event_ids=[evidence],
    )
    graph_repo.add_node(
        db_with_events, project_id, "A-0001", "action", "v1", status="completed", evidence_event_ids=[evidence],
    )
    node = graph_repo.get_node(db_with_events, "A-0001")
    assert node["status"] == "completed"


def test_add_edge_rejects_node_from_a_different_project(db_with_events):
    """Same bug class as the node guard, confirmed by the same real headless
    test: 25 edges in that run ended up referencing a node from a different
    project than the edge's own declared project -- a direct downstream
    consequence of node ids colliding across projects."""
    project_a = graph_repo.ensure_project(db_with_events, "/example/project-a")
    project_b = graph_repo.ensure_project(db_with_events, "/example/project-b")
    evidence = _first_event_id(db_with_events, "user_message")

    graph_repo.add_node(
        db_with_events, project_a, "Q-0001", "goal", "Project A's goal", evidence_event_ids=[evidence],
    )
    graph_repo.add_node(
        db_with_events, project_b, "O-0001", "option", "Project B's option", evidence_event_ids=[evidence],
    )
    with pytest.raises(graph_repo.CrossProjectIdCollisionError):
        graph_repo.add_edge(
            db_with_events, project_b, "Q-0001", "O-0001", "EXPLORES", evidence_event_ids=[evidence],
        )


    # Note: add_edge also guards its own deterministic edge id
    # ("source|type|target") against collision with a different project's
    # edge, same pattern as add_node -- but with the node-level guard above
    # now in place, two projects can no longer legitimately hold the same
    # node id in the first place, which makes that specific edge-id
    # collision unreachable through normal validated writes. It stays in
    # add_edge as defense-in-depth (e.g. against a future weakening of the
    # node guard), just without an isolated test for a path that's no
    # longer constructible without directly corrupting the DB by hand.
