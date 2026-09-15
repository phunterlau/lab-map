"""Direct unit tests for the check logic in projection/tree.py -- the
"what did I miss" findings and the YOU-ARE-HERE tie-break, tested against
the pure graph shape rather than through a renderer's text formatting.
Every check here is derived from a real case found in local/kgw-example
(see docs/architecture.md); these fixtures reconstruct the minimal shape
of each real case, not the real (gitignored) data itself."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import tree as tree_mod
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


def _kinds(findings: list[tree_mod.Finding]) -> list[str]:
    return [f.kind for f in findings]


# --- dormant_unresolved ------------------------------------------------

def test_dormant_option_without_closing_edge_is_flagged(evidence):
    """Case A from the real KGW forensic pass: O-KGW-05, an alternative
    surfaced alongside a pivot decision, set dormant and never formally
    closed (no REJECTED_BECAUSE/CHOSEN_OVER/SUPERSEDES ever points at it)."""
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Alternative surfaced alongside the pivot",
        status="dormant", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "O-0002", "Q-0001", "RELATED_TO", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "dormant_unresolved" in _kinds(findings)
    f = next(f for f in findings if f.kind == "dormant_unresolved")
    assert f.node_id == "O-0002"


def test_dormant_option_with_closing_edge_is_not_flagged(evidence):
    """Regression: test_graph.py's own canonical scenario has O-0002 dormant
    *with* a REJECTED_BECAUSE edge -- that must stay unflagged, or the
    check would cry wolf on the ordinary "offered, not chosen" case."""
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Graphiti", status="dormant", evidence_event_ids=[ev],
    )
    graph_repo.add_node(
        conn, project_id, "D-0003", "decision", "Prototype something else first",
        status="chosen", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "D-0003", "O-0002", "REJECTED_BECAUSE", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "dormant_unresolved" not in _kinds(findings)


# --- experiment_no_outcome ----------------------------------------------

def test_completed_experiment_without_produced_outcome_is_flagged(evidence):
    """Case from the real KGW forensic pass: X-KGW-07 is `completed` but its
    only recorded PRODUCED edge was deliberately removed as factually
    wrong (see local/kgw-example/build.py's own comment) -- it genuinely
    has no recorded result."""
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "X-0001", "experiment", "Scale edit budget",
        status="completed", evidence_event_ids=[ev],
    )

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "experiment_no_outcome" in _kinds(findings)


def test_completed_experiment_with_produced_outcome_is_not_flagged(evidence):
    """PRODUCED's real direction convention (both instances in
    local/kgw-example/build.py) is outcome -source-> experiment -target-:
    the outcome node is the source, the experiment it came from is the
    target."""
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "X-0001", "experiment", "Scale edit budget",
        status="completed", evidence_event_ids=[ev],
    )
    graph_repo.add_node(
        conn, project_id, "EV-0002", "outcome", "AUC 0.751", status="completed", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "EV-0002", "X-0001", "PRODUCED", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "experiment_no_outcome" not in _kinds(findings)


def test_rejected_experiment_without_produced_outcome_is_not_flagged(evidence):
    """A dead-end experiment (status=rejected) doesn't need a PRODUCED
    outcome -- its own status already records the result. Matches the real
    X-KGW-B18K case (explicitly called out as a dead end, never a
    PRODUCED-outcome candidate). Restricting the check to status=completed
    is what keeps this from being a false positive."""
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "X-0001", "experiment", "A dead end",
        status="rejected", evidence_event_ids=[ev],
    )

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "experiment_no_outcome" not in _kinds(findings)


# --- unresolved_sibling ---------------------------------------------------

def test_unresolved_sibling_flagged_when_dormant_sibling_sits_under_a_resolved_fan_out(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "O-0002", "option", "Chosen path", status="chosen", evidence_event_ids=[ev])
    graph_repo.add_node(
        conn, project_id, "O-0003", "option", "Never touched again", status="dormant", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0003", "EXPLORES", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "unresolved_sibling" in _kinds(findings)
    f = next(f for f in findings if f.kind == "unresolved_sibling")
    assert f.node_id == "O-0003"


def test_unresolved_sibling_not_flagged_when_sibling_is_actively_exploring(evidence):
    """Calibration per explicit product decision: a sibling that's currently
    `exploring` is a live parallel branch, not a forgotten one -- matches
    the real O-KGW-DP case, which must NOT be nagged about while it's still
    being actively worked."""
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "O-0002", "option", "Chosen path", status="chosen", evidence_event_ids=[ev])
    graph_repo.add_node(
        conn, project_id, "O-0003", "option", "Explicit parallel branch",
        status="exploring", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0003", "EXPLORES", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "unresolved_sibling" not in _kinds(findings)


def test_unresolved_sibling_not_flagged_when_no_sibling_is_resolved_yet(evidence):
    """Two open branches with nothing resolved yet isn't "forgot to follow
    up", it's just an in-progress fan-out."""
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "O-0002", "option", "A", status="open", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "O-0003", "option", "B", status="open", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0003", "EXPLORES", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "unresolved_sibling" not in _kinds(findings)


# --- contradicted_but_chosen ---------------------------------------------

def test_contradicted_edge_into_a_chosen_node_is_flagged(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "D-0001", "decision", "Current call", status="chosen", evidence_event_ids=[ev],
    )
    graph_repo.add_node(
        conn, project_id, "EV-0002", "evidence", "Later result disagrees",
        status="completed", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "EV-0002", "D-0001", "CONTRADICTS", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "contradicted_but_chosen" in _kinds(findings)


def test_no_contradicts_edge_is_not_flagged(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "D-0001", "decision", "Current call", status="chosen", evidence_event_ids=[ev],
    )

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "contradicted_but_chosen" not in _kinds(findings)


# --- revisit_condition (pre-existing check, now under the uniform shape) --

def test_open_revisit_condition_is_flagged(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "R-0001", "revisit_condition", "Parked", status="open", evidence_event_ids=[ev],
    )

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "revisit_condition" in _kinds(findings)


def test_superseded_revisit_condition_is_not_flagged(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "R-0001", "revisit_condition", "Resolved", status="superseded", evidence_event_ids=[ev],
    )

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)

    assert "revisit_condition" not in _kinds(findings)


# --- ordering --------------------------------------------------------------

def test_findings_render_in_stable_kind_order(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Dormant", status="dormant", evidence_event_ids=[ev],
    )
    graph_repo.add_node(
        conn, project_id, "R-0003", "revisit_condition", "Parked", status="open", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "O-0002", "Q-0001", "RELATED_TO", evidence_event_ids=[ev])

    t = tree_mod.build_forest(conn, project_id)
    findings = tree_mod.collect_findings(conn, project_id, t.by_id, t.node_timestamp)
    kinds = _kinds(findings)

    # revisit_condition sorts before dormant_unresolved per FINDING_ORDER,
    # regardless of insertion/dict-iteration order.
    assert kinds.index("revisit_condition") < kinds.index("dormant_unresolved")


# --- format_relative_age ----------------------------------------------------

@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=10), "just now"),
        (timedelta(minutes=5), "5m ago"),
        (timedelta(hours=3), "3h ago"),
        (timedelta(days=3, hours=2), "3d ago"),
        (timedelta(days=45), "1mo ago"),
        (timedelta(days=400), "1y ago"),
    ],
)
def test_format_relative_age_buckets(delta, expected):
    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    ts = (now - delta).isoformat()
    assert tree_mod.format_relative_age(ts, now=now) == expected


def test_format_relative_age_returns_none_for_missing_timestamp():
    assert tree_mod.format_relative_age(None) is None


# --- YOU-ARE-HERE tie-break --------------------------------------------------

def test_you_are_here_falls_back_to_updated_at_when_no_evidence(evidence):
    """Evidence timestamp is the primary signal (see the regression test
    below), but a structural node written with allow_no_evidence has none
    to use -- updated_at is the only fallback available for it."""
    conn, project_id, _ev = evidence
    graph_repo.add_node(
        conn, project_id, "A-0001", "action", "First", status="open", allow_no_evidence=True,
    )
    conn.execute("UPDATE graph_nodes SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = 'A-0001'")
    graph_repo.add_node(
        conn, project_id, "A-0002", "action", "Second", status="open", allow_no_evidence=True,
    )
    conn.execute("UPDATE graph_nodes SET updated_at = '2026-02-01T00:00:00+00:00' WHERE id = 'A-0002'")
    conn.commit()

    t = tree_mod.build_forest(conn, project_id)
    assert t.you_are_here == "A-0002"


def _insert_synthetic_event(conn, event_id: str, byte_start: int, ts: str) -> None:
    """A minimal, fully-controlled normalized_events row -- avoids depending
    on which/how-many real events happen to exist in the shared fixture
    file, which is what made an earlier draft of this test fragile."""
    conn.execute(
        "INSERT INTO normalized_events "
        "(id, session_id, provider, event_index, event_type, text, timestamp, "
        " transcript_path, byte_start, byte_end, content_hash) "
        "VALUES (?, 'synthetic-session', 'codex', 0, 'user_message', 'x', ?, "
        "'/synthetic.jsonl', ?, ?, ?)",
        (event_id, ts, byte_start, byte_start + 1, event_id),
    )


def test_you_are_here_uses_evidence_timestamp_over_write_order(evidence):
    """Regression: local/kgw-example's R-KGW-SIGN and O-KGW-DP's updated_at
    values turned out NOT to be a tie at all -- they differ by ~5ms, purely
    from build.py's Python loop insertion order, which has nothing to do
    with the real conversation. A plain max(updated_at) silently favored
    O-KGW-DP (written a few microseconds later in the loop) even though
    R-KGW-SIGN's real evidence event is chronologically a full day later.
    Evidence timestamp must win even when updated_at says otherwise."""
    conn, project_id, _ev = evidence
    _insert_synthetic_event(conn, "ev-early", 1000, "2026-01-01T00:00:00+00:00")
    _insert_synthetic_event(conn, "ev-late", 2000, "2026-06-01T00:00:00+00:00")
    conn.commit()

    graph_repo.add_node(
        conn, project_id, "A-0001", "action", "Earlier evidence", status="open", evidence_event_ids=["ev-early"],
    )
    graph_repo.add_node(
        conn, project_id, "A-0002", "action", "Later evidence", status="open", evidence_event_ids=["ev-late"],
    )
    # A-0001 is written to the DB fractionally LATER than A-0002 here (its
    # updated_at is set newer below) -- if updated_at still won, it would
    # incorrectly pick A-0001 despite A-0002's evidence being chronologically
    # later. This is what makes the test prove evidence wins, not just
    # coincide with whichever node happens to have a later write time too.
    conn.execute("UPDATE graph_nodes SET updated_at = '2026-03-01T00:00:00+00:00' WHERE id = 'A-0001'")
    conn.execute("UPDATE graph_nodes SET updated_at = '2026-01-01T00:00:00+00:00' WHERE id = 'A-0002'")
    conn.commit()

    t = tree_mod.build_forest(conn, project_id)
    assert t.you_are_here == "A-0002"
