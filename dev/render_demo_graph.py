#!/usr/bin/env python3
"""Regenerate assets/demo-graph.png from the synthetic test fixtures.

Builds the build plan's own canonical scenario (section 27: Graphiti vs.
Markdown vs. Postgres for persistent research memory) as a real graph
backed by real provenance into the synthetic Codex fixture, then exports
it. Safe to run and commit the output from: everything here traces back to
tests/fixtures/, which is fictional content, never real transcripts.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import graphviz_export
from trace_mind.storage.db import connect
from trace_mind.transcripts.codex import CodexTranscriptAdapter

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "codex" / "codex_research_memory_decision.jsonl"
OUT_PATH = REPO_ROOT / "assets" / "demo-graph.png"
SESSION_ID = "01a0aaaa-0000-7000-8000-000000000001"
PROJECT_ROOT = "demo-project"


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        conn = connect(Path(tmp) / "demo.db")
        ingest_session(conn, CodexTranscriptAdapter(), SESSION_ID, FIXTURE)
        project_id = graph_repo.ensure_project(conn, PROJECT_ROOT, name="Demo: persistent research memory")

        goal_evidence = _event_id(conn, "We need persistent research memory")

        graph_repo.add_node(
            conn, project_id, "Q-0001", "goal", "Persistent research memory",
            summary="How should evolving research decisions survive across sessions and agents?",
            status="exploring", evidence_event_ids=[goal_evidence],
        )
        graph_repo.add_node(
            conn, project_id, "O-0002", "option", "Graphiti (temporal knowledge graph)",
            summary="Native time-varying edges, but needs a continuously running service.",
            status="dormant", evidence_event_ids=[goal_evidence],
        )
        graph_repo.add_node(
            conn, project_id, "O-0003", "option", "Markdown + MCP",
            summary="Zero-infra, human-readable; weaker at representing how a decision changed over time.",
            status="chosen", evidence_event_ids=[goal_evidence],
        )
        graph_repo.add_node(
            conn, project_id, "O-0004", "option", "Postgres-backed decision graph",
            summary="Most flexible querying; a server + migrations for a single-person project.",
            status="dormant", evidence_event_ids=[goal_evidence],
        )
        graph_repo.add_node(
            conn, project_id, "D-0005", "decision", "Prototype Markdown + MCP first",
            summary="Cheap to build and throw away; keep Graphiti as a documented fallback.",
            status="chosen", evidence_event_ids=[goal_evidence],
        )
        graph_repo.add_node(
            conn, project_id, "R-0006", "revisit_condition", "Reconsider Graphiti",
            summary="If temporal/versioned queries become central to how the graph is used.",
            status="open", evidence_event_ids=[goal_evidence],
        )

        graph_repo.add_edge(conn, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[goal_evidence])
        graph_repo.add_edge(conn, project_id, "Q-0001", "O-0003", "EXPLORES", evidence_event_ids=[goal_evidence])
        graph_repo.add_edge(conn, project_id, "Q-0001", "O-0004", "EXPLORES", evidence_event_ids=[goal_evidence])
        graph_repo.add_edge(conn, project_id, "D-0005", "O-0003", "CHOSEN_OVER", evidence_event_ids=[goal_evidence])
        graph_repo.add_edge(
            conn, project_id, "D-0005", "O-0002", "REJECTED_BECAUSE",
            reason="operationally heavy for current scale", evidence_event_ids=[goal_evidence],
        )
        graph_repo.add_edge(conn, project_id, "R-0006", "O-0002", "REVISIT_WHEN", evidence_event_ids=[goal_evidence])

        OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        graphviz_export.render_png(conn, project_id, OUT_PATH)
        conn.close()

    print(f"wrote {OUT_PATH}")


def _event_id(conn, text_snippet: str) -> str:
    row = conn.execute(
        "SELECT id FROM normalized_events WHERE text LIKE ? LIMIT 1", (f"%{text_snippet}%",)
    ).fetchone()
    if row is None:
        raise SystemExit(f"no event found matching {text_snippet!r}")
    return row["id"]


if __name__ == "__main__":
    main()
