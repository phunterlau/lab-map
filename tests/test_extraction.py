"""Milestone 5 tests. Never call the real OpenAI API -- `client.call_json`
is monkeypatched everywhere a full extraction run is exercised. The
canned envelope shapes below mirror the build plan's own section 27
canonical scenario (also used by tests/test_graph.py and
dev/render_demo_graph.py): a goal, three offered options, a decision that
picks one and leaves another dormant (not rejected).
"""
from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from trace_mind.extraction import client, ids, prefilter, runner
from trace_mind.extraction.apply import apply_envelope
from trace_mind.extraction.models import ExtractionEnvelope
from trace_mind.extraction.window import build_window
from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.storage.db import connect
from trace_mind.transcripts.codex import CodexTranscriptAdapter

FIXTURES = Path(__file__).parent / "fixtures"
CODEX_FIXTURE = FIXTURES / "codex" / "codex_research_memory_decision.jsonl"
SESSION_ID = "01a0aaaa-0000-7000-8000-000000000001"
PROJECT_ROOT = "/example/project"


@pytest.fixture()
def db_with_ingest(tmp_path):
    conn = connect(tmp_path / "research.db")
    result = ingest_session(conn, CodexTranscriptAdapter(), SESSION_ID, CODEX_FIXTURE)
    project_id = graph_repo.ensure_project(conn, PROJECT_ROOT)
    yield conn, project_id, result.new_event_ids
    conn.close()


def _insert_raw_event(conn, event_id: str, *, text: str, event_type: str = "user_message", role: str = "user", byte_start: int = 0):
    conn.execute(
        """
        INSERT INTO normalized_events (
            id, session_id, provider, turn_id, event_index, role, event_type,
            text, tool_name, timestamp, transcript_path, byte_start, byte_end, content_hash
        ) VALUES (?, ?, 'codex', NULL, 0, ?, ?, ?, NULL, NULL, '/fake/path.jsonl', ?, ?, ?)
        """,
        (event_id, SESSION_ID, role, event_type, text, byte_start, byte_start + len(text), f"hash-{event_id}"),
    )
    conn.commit()


# ---- prefilter ----

def test_prefilter_triggers_on_real_alternatives_text(db_with_ingest):
    conn, _project_id, new_event_ids = db_with_ingest
    events = conn.execute(
        f"SELECT * FROM normalized_events WHERE id IN ({','.join('?' * len(new_event_ids))})",
        new_event_ids,
    ).fetchall()
    result = prefilter.evaluate(events)
    assert result.worth_extracting
    assert "alternative_language" in result.signals or "decision_language" in result.signals


def test_prefilter_skips_short_acknowledgement(tmp_path):
    conn = connect(tmp_path / "research.db")
    _insert_raw_event(conn, "evt-short", text="yes")
    events = conn.execute("SELECT * FROM normalized_events").fetchall()
    result = prefilter.evaluate(events)
    assert not result.worth_extracting
    conn.close()


# ---- ids ----

def test_allocate_node_id_increments_per_type(tmp_path):
    conn = connect(tmp_path / "research.db")
    project_id = graph_repo.ensure_project(conn, PROJECT_ROOT)
    assert ids.allocate_node_id(conn, project_id, "option") == "O-0001"
    assert ids.allocate_node_id(conn, project_id, "option") == "O-0002"
    assert ids.allocate_node_id(conn, project_id, "goal") == "Q-0001"
    conn.close()


# ---- window ----

def test_window_input_hash_stable_and_independent_of_neighborhood(db_with_ingest):
    """Identity is the event delta + prompt version only. Neighborhood drift
    (e.g. from applying a prior run's own mutations) must NOT change the
    hash, or re-running the same delta right after its first successful
    extraction would look like a "new" input and lose idempotence."""
    conn, project_id, new_event_ids = db_with_ingest
    w1 = build_window(conn, project_id, SESSION_ID, new_event_ids)
    w2 = build_window(conn, project_id, SESSION_ID, new_event_ids)
    assert w1.input_hash == w2.input_hash
    assert set(w1.event_labels) == {f"e{i+1}" for i in range(len(new_event_ids))}

    evidence = new_event_ids[0]
    graph_repo.add_node(
        conn, project_id, "O-0001", "option", "Graphiti",
        evidence_event_ids=[evidence],
    )
    w3 = build_window(conn, project_id, SESSION_ID, new_event_ids)
    assert w3.input_hash == w1.input_hash
    assert w3.neighborhood_nodes  # neighborhood itself did pick up the change


def test_window_always_includes_goals_even_when_not_recent(db_with_ingest):
    """Regression: found via a real extraction run against real transcript
    content -- a goal set once near the start of a project and never
    updated again was getting pushed out of a pure-recency top-N window by
    ordinary churn, so the extractor couldn't see the one node it needed to
    tell a new goal apart from a branch off an existing one."""
    conn, project_id, new_event_ids = db_with_ingest
    evidence = new_event_ids[0]
    graph_repo.add_node(
        conn, project_id, "Q-0001", "goal", "The original goal",
        evidence_event_ids=[evidence],
    )
    # Push NEIGHBORHOOD_SIZE other nodes' updated_at ahead of the goal's.
    from trace_mind.extraction.window import NEIGHBORHOOD_SIZE
    for i in range(NEIGHBORHOOD_SIZE + 2):
        graph_repo.add_node(
            conn, project_id, f"O-{i:04d}", "option", f"Churned option {i}",
            evidence_event_ids=[evidence],
        )

    window = build_window(conn, project_id, SESSION_ID, new_event_ids)
    assert "Q-0001" in {n["id"] for n in window.neighborhood_nodes}


def test_build_window_requires_event_ids(db_with_ingest):
    conn, project_id, _new_event_ids = db_with_ingest
    with pytest.raises(ValueError):
        build_window(conn, project_id, SESSION_ID, [])


# ---- models ----

def test_envelope_rejects_unknown_node_type():
    with pytest.raises(ValidationError):
        ExtractionEnvelope.model_validate({
            "create_nodes": [{
                "temp_id": "n1", "type": "not_a_real_type", "title": "x",
                "evidence_event_labels": ["e1"],
            }],
            "update_nodes": [], "create_edges": [], "supersede": [],
            "merge_suggestions": [], "needs_review": [],
        })


def test_envelope_requires_at_least_one_evidence_label():
    with pytest.raises(ValidationError):
        ExtractionEnvelope.model_validate({
            "create_nodes": [{
                "temp_id": "n1", "type": "option", "title": "x",
                "evidence_event_labels": [],
            }],
            "update_nodes": [], "create_edges": [], "supersede": [],
            "merge_suggestions": [], "needs_review": [],
        })


def _canonical_envelope_dict() -> dict:
    """Matches the fixture's real content: a goal, three offered options,
    a decision picking one, Graphiti left dormant (not rejected)."""
    return {
        "create_nodes": [
            {"temp_id": "n_goal", "type": "goal", "title": "Persistent research memory",
             "status": "exploring", "evidence_event_labels": ["e1"]},
            {"temp_id": "n_a", "type": "option", "title": "Graphiti (temporal knowledge graph)",
             "status": "dormant", "evidence_event_labels": ["e2"]},
            {"temp_id": "n_b", "type": "option", "title": "Markdown + MCP",
             "status": "chosen", "evidence_event_labels": ["e2"]},
            {"temp_id": "n_c", "type": "option", "title": "Postgres-backed decision graph",
             "status": "dormant", "evidence_event_labels": ["e2"]},
            {"temp_id": "n_d", "type": "decision", "title": "Prototype Markdown + MCP first",
             "status": "chosen", "evidence_event_labels": ["e6"]},
        ],
        "update_nodes": [],
        "create_edges": [
            {"source": "n_goal", "target": "n_a", "type": "EXPLORES", "evidence_event_labels": ["e2"]},
            {"source": "n_goal", "target": "n_b", "type": "EXPLORES", "evidence_event_labels": ["e2"]},
            {"source": "n_goal", "target": "n_c", "type": "EXPLORES", "evidence_event_labels": ["e2"]},
            {"source": "n_d", "target": "n_b", "type": "CHOSEN_OVER", "evidence_event_labels": ["e6"]},
        ],
        "supersede": [],
        "merge_suggestions": [],
        "needs_review": [],
    }


# ---- apply ----

def test_apply_envelope_creates_nodes_and_edges_with_provenance(db_with_ingest):
    conn, project_id, new_event_ids = db_with_ingest
    window = build_window(conn, project_id, SESSION_ID, new_event_ids)
    envelope = ExtractionEnvelope.model_validate(_canonical_envelope_dict())

    result = apply_envelope(conn, project_id, window, envelope, extractor_version="test:p1")

    assert result.nodes_created == 5
    assert result.edges_created == 4
    assert not result.rejected

    goal_id = result.created_node_ids["n_goal"]
    assert goal_id == "Q-0001"
    graphiti_id = result.created_node_ids["n_a"]
    node = graph_repo.get_node(conn, graphiti_id)
    assert node["status"] == "dormant"  # not "rejected" -- OFFER_AND_PICK rule

    prov = graph_repo.provenance_for(conn, "node", goal_id)
    assert len(prov) == 1
    assert prov[0]["byte_start"] is not None


def test_apply_envelope_rejects_unresolvable_evidence_label_without_failing_batch(db_with_ingest):
    conn, project_id, new_event_ids = db_with_ingest
    window = build_window(conn, project_id, SESSION_ID, new_event_ids)
    envelope = ExtractionEnvelope.model_validate({
        "create_nodes": [
            {"temp_id": "n_good", "type": "goal", "title": "Real one", "evidence_event_labels": ["e1"]},
            {"temp_id": "n_bad", "type": "option", "title": "Hallucinated label", "evidence_event_labels": ["e999"]},
        ],
        "update_nodes": [], "create_edges": [], "supersede": [],
        "merge_suggestions": [], "needs_review": [],
    })

    result = apply_envelope(conn, project_id, window, envelope, extractor_version="test:p1")

    assert result.nodes_created == 1
    assert "n_good" in result.created_node_ids
    assert len(result.rejected) == 1
    assert "n_bad" in result.rejected[0]


def test_apply_envelope_rejects_edge_to_unresolved_node_reference(db_with_ingest):
    conn, project_id, new_event_ids = db_with_ingest
    window = build_window(conn, project_id, SESSION_ID, new_event_ids)
    envelope = ExtractionEnvelope.model_validate({
        "create_nodes": [
            {"temp_id": "n1", "type": "goal", "title": "A goal", "evidence_event_labels": ["e1"]},
        ],
        "update_nodes": [],
        "create_edges": [
            {"source": "n1", "target": "does-not-exist", "type": "EXPLORES", "evidence_event_labels": ["e1"]},
        ],
        "supersede": [], "merge_suggestions": [], "needs_review": [],
    })

    result = apply_envelope(conn, project_id, window, envelope, extractor_version="test:p1")

    assert result.nodes_created == 1
    assert result.edges_created == 0
    assert len(result.rejected) == 1


def test_apply_envelope_never_applies_merge_suggestions_or_needs_review(db_with_ingest):
    conn, project_id, new_event_ids = db_with_ingest
    window = build_window(conn, project_id, SESSION_ID, new_event_ids)
    envelope = ExtractionEnvelope.model_validate({
        "create_nodes": [], "update_nodes": [], "create_edges": [], "supersede": [],
        "merge_suggestions": [{"node_id_a": "O-0001", "node_id_b": "O-0002", "reason": "maybe duplicates"}],
        "needs_review": [{"description": "ambiguous turn", "related_node_ids": []}],
    })

    result = apply_envelope(conn, project_id, window, envelope, extractor_version="test:p1")

    assert result.nodes_created == 0
    assert len(result.merge_suggestions) == 1
    assert len(result.needs_review) == 1
    assert graph_repo.list_nodes(conn, project_id) == []


# ---- runner (client always monkeypatched -- never hits the real API) ----

def test_runner_skips_prefilter_without_calling_client(tmp_path, monkeypatch):
    conn = connect(tmp_path / "research.db")
    project_id = graph_repo.ensure_project(conn, PROJECT_ROOT)
    _insert_raw_event(conn, "evt-short", text="yes")

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("client.call_json must not be called when the prefilter skips")

    monkeypatch.setattr(client, "call_json", _fail_if_called)

    result = runner.run_extraction(conn, project_id, SESSION_ID, ["evt-short"])
    assert result.status == "skipped_prefilter"
    conn.close()


def test_runner_applies_valid_envelope_and_is_idempotent(db_with_ingest, monkeypatch):
    conn, project_id, new_event_ids = db_with_ingest
    calls = {"n": 0}

    def _fake_call_json(_system, _user, _schema, **_kwargs):
        calls["n"] += 1
        return _canonical_envelope_dict()

    monkeypatch.setattr(client, "call_json", _fake_call_json)

    result1 = runner.run_extraction(conn, project_id, SESSION_ID, new_event_ids)
    assert result1.status == "applied"
    assert result1.apply_result.nodes_created == 5
    assert calls["n"] == 1

    result2 = runner.run_extraction(conn, project_id, SESSION_ID, new_event_ids)
    assert result2.status == "cached"
    assert calls["n"] == 1  # not called again

    run_row = conn.execute(
        "SELECT * FROM extraction_runs WHERE input_hash = ?", (result1.input_hash,)
    ).fetchone()
    assert run_row["status"] == "applied"


def test_runner_records_api_error_and_allows_retry(db_with_ingest, monkeypatch):
    conn, project_id, new_event_ids = db_with_ingest

    def _raise(*_args, **_kwargs):
        raise client.ExtractionAPIError("simulated transport failure")

    monkeypatch.setattr(client, "call_json", _raise)
    result1 = runner.run_extraction(conn, project_id, SESSION_ID, new_event_ids)
    assert result1.status == "api_error"

    run_row = conn.execute(
        "SELECT * FROM extraction_runs WHERE input_hash = ?", (result1.input_hash,)
    ).fetchone()
    assert run_row["status"] == "api_error"

    monkeypatch.setattr(client, "call_json", lambda *_a, **_k: _canonical_envelope_dict())
    result2 = runner.run_extraction(conn, project_id, SESSION_ID, new_event_ids)
    assert result2.status == "applied"


def test_runner_rejects_malformed_output_without_applying_anything(db_with_ingest, monkeypatch):
    conn, project_id, new_event_ids = db_with_ingest

    def _bad_output(*_args, **_kwargs):
        return {
            "create_nodes": [{"temp_id": "n1", "type": "not_a_real_type", "title": "x", "evidence_event_labels": ["e1"]}],
            "update_nodes": [], "create_edges": [], "supersede": [],
            "merge_suggestions": [], "needs_review": [],
        }

    monkeypatch.setattr(client, "call_json", _bad_output)
    result = runner.run_extraction(conn, project_id, SESSION_ID, new_event_ids)

    assert result.status == "invalid_schema"
    assert graph_repo.list_nodes(conn, project_id) == []
