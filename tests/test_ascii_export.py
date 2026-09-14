import json
import time
from pathlib import Path

import pytest

from trace_mind.graph import repository as graph_repo
from trace_mind.normalize.pipeline import ingest_session
from trace_mind.projection import ascii_export
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


def _tree_body(output: str) -> list[str]:
    """Strip the leading "You are here: ..." breadcrumb + its blank line --
    render_ascii always emits exactly those two lines first when there's at
    least one node -- leaving the tree/unlinked/open-loops lines below."""
    lines = output.splitlines()
    assert lines[0].startswith("You are here:")
    assert lines[1] == ""
    return lines[2:]


def _build_canonical_graph(conn, project_id, evidence_id):
    """Build the build plan's own section 27 scenario: a goal, three
    offered options (one chosen, two dormant), a decision, and one
    revisit_condition -- same shape used by test_graph.py and
    dev/render_demo_graph.py."""
    graph_repo.add_node(
        conn, project_id, "Q-0001", "goal", "Persistent research memory",
        status="exploring", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Graphiti",
        status="dormant", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_node(
        conn, project_id, "O-0003", "option", "Markdown + MCP",
        status="chosen", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_node(
        conn, project_id, "O-0004", "option", "Postgres-backed decision graph",
        status="dormant", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_node(
        conn, project_id, "D-0005", "decision", "Prototype Markdown + MCP first",
        status="chosen", evidence_event_ids=[evidence_id],
    )
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[evidence_id])
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0003", "EXPLORES", evidence_event_ids=[evidence_id])
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0004", "EXPLORES", evidence_event_ids=[evidence_id])
    graph_repo.add_edge(conn, project_id, "D-0005", "O-0003", "CHOSEN_OVER", evidence_event_ids=[evidence_id])
    # DERIVED_FROM declared newer -> older, deliberately opposite direction
    # from CHOSEN_OVER above, to exercise the real-direction annotation.
    graph_repo.add_edge(conn, project_id, "D-0005", "O-0002", "DERIVED_FROM", evidence_event_ids=[evidence_id])


def test_goal_is_root_and_options_are_children(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    lines = _tree_body(output)

    assert lines[0].startswith("Q-0001 [goal/exploring]")
    option_lines = [l for l in lines if "[option/" in l]
    assert len(option_lines) == 3
    for l in option_lines:
        # every option line is more indented than the root
        assert l.startswith(("├── ", "└── ", "│   ", "    "))


def test_edge_annotation_shows_real_type_and_direction_parent_to_child(evidence):
    """Edge declared parent -> child (Q-0001 --EXPLORES--> O-0002): from the
    child's perspective in the tree, the edge points IN from the parent."""
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "Q-0001", "goal", "A goal", status="exploring", evidence_event_ids=[ev],
    )
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "An option", status="open", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "Q-0001", "O-0002", "EXPLORES", evidence_event_ids=[ev])

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    o2_line = next(l for l in output.splitlines() if "O-0002" in l and "[option" in l)
    assert "EXPLORES" in o2_line
    assert "from parent" in o2_line


def test_edge_annotation_shows_real_type_and_direction_child_to_parent(evidence):
    """Edge declared child -> parent (D-0002 --DERIVED_FROM--> Q-0001): from
    the child's perspective in the tree, the edge points OUT toward the
    parent -- the annotation must reflect that, not just "connected"."""
    conn, project_id, ev = evidence
    graph_repo.add_node(
        conn, project_id, "Q-0001", "goal", "A goal", status="exploring", evidence_event_ids=[ev],
    )
    graph_repo.add_node(
        conn, project_id, "D-0002", "decision", "A decision derived from the goal",
        status="chosen", evidence_event_ids=[ev],
    )
    graph_repo.add_edge(conn, project_id, "D-0002", "Q-0001", "DERIVED_FROM", evidence_event_ids=[ev])

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    d2_line = next(l for l in output.splitlines() if "D-0002" in l and "[decision" in l)
    assert "DERIVED_FROM" in d2_line
    assert "to parent" in d2_line


def test_exactly_one_you_are_here_on_most_recently_updated_node(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    # Touch O-0002 again so it's unambiguously the most recently updated.
    time.sleep(0.01)
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Graphiti",
        status="dormant", evidence_event_ids=[ev],
    )

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    here_lines = [l for l in output.splitlines() if "YOU ARE HERE" in l]
    assert len(here_lines) == 1
    assert "O-0002" in here_lines[0]


def test_color_mode_adds_ansi_escapes_only_when_requested(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    colored = ascii_export.render_ascii(conn, project_id, use_color=True)
    plain = ascii_export.render_ascii(conn, project_id, use_color=False)

    assert "\x1b[" in colored
    assert "\x1b[" not in plain
    # stripping ANSI leaves the same structural content behind
    import re
    stripped = re.sub(r"\x1b\[[0-9;]*m", "", colored)
    assert stripped == plain


def test_unlinked_node_appears_under_heading(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)
    graph_repo.add_node(
        conn, project_id, "O-9999", "option", "Totally unrelated stray node",
        status="open", evidence_event_ids=[ev],
    )

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    lines = _tree_body(output)
    assert ascii_export.UNLINKED_HEADING in lines
    heading_index = lines.index(ascii_export.UNLINKED_HEADING)
    stray_index = next(i for i, l in enumerate(lines) if "O-9999" in l)
    assert stray_index > heading_index


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

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert "O-0001" in output
    assert "D-0002" in output


def test_deep_tree_indentation_is_structurally_correct(evidence):
    """Regression: found by manually inspecting real rendered output against
    real (gitignored) data -- a non-last child's own line was getting an
    extra, wrongly-placed indent/bar before its connector, because the
    prefix handed to a child's recursive call was being extended based on
    THAT child's own position instead of its PARENT's. Exercise 3 levels
    deep with a mix of last/non-last siblings at each level, where a plain
    substring check on indentation alone wouldn't catch a shifted connector."""
    conn, project_id, ev = evidence
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

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    lines = _tree_body(output)
    # Parse assumes a single-token node id before "[" -- true for this
    # test's ids (A, B, A1, ...) but would break on a realistic id
    # containing a space or literal "[".
    by_content = {l.split("[")[0].strip().split()[-1]: l for l in lines if "[" in l}
    # A is not-last among Q-0001's children (B follows) -> its subtree keeps
    # a continuing bar; B is last -> no bar under it.
    assert by_content["A"].startswith("├── A")
    assert by_content["B"].startswith("└── B")
    assert by_content["A1"].startswith("│   ├── A1")
    assert by_content["A2"].startswith("│   └── A2")
    # A1 is NOT last among A's children (A2 follows), so A1's own bar
    # continues into its children's prefix too: "│   " (from A) + "│   "
    # (from A1) + "└── " (A1x is A1's only, thus last, child).
    assert by_content["A1x"].startswith("│   │   └── A1x")


def test_breadcrumb_shows_path_from_root_to_you_are_here(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)
    time.sleep(0.01)
    graph_repo.add_node(
        conn, project_id, "O-0002", "option", "Graphiti", status="dormant", evidence_event_ids=[ev],
    )
    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert output.splitlines()[0] == "You are here: Q-0001 → O-0002"


def test_breadcrumb_shows_multi_hop_path(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "A", "option", "A", status="open", evidence_event_ids=[ev])
    graph_repo.add_node(conn, project_id, "A1", "option", "A1", status="open", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "Q-0001", "A", "EXPLORES", evidence_event_ids=[ev])
    graph_repo.add_edge(conn, project_id, "A", "A1", "EXPLORES", evidence_event_ids=[ev])

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert output.splitlines()[0] == "You are here: Q-0001 → A → A1"


def test_breadcrumb_handles_you_are_here_in_unlinked_component(evidence):
    conn, project_id, ev = evidence
    graph_repo.add_node(conn, project_id, "Q-0001", "goal", "Root", status="exploring", evidence_event_ids=[ev])
    time.sleep(0.01)
    graph_repo.add_node(conn, project_id, "O-9999", "option", "Stray", status="open", evidence_event_ids=[ev])

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert output.splitlines()[0] == "You are here: O-9999"


def test_open_loops_shows_parked_revisit_condition(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)
    graph_repo.add_node(
        conn, project_id, "R-0001", "revisit_condition", "Revisit budget cap once k>10 lands",
        status="open", evidence_event_ids=[ev],
    )

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert ascii_export.OPEN_LOOPS_HEADING in output
    assert "  [revisit_condition] R-0001: Revisit budget cap once k>10 lands" in output.splitlines()


def test_open_loops_excludes_non_open_revisit_condition(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)
    graph_repo.add_node(
        conn, project_id, "R-0001", "revisit_condition", "Already resolved",
        status="superseded", evidence_event_ids=[ev],
    )

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    # R-0001 legitimately appears elsewhere (it's the most-recently-touched
    # node, so it's the breadcrumb/YOU ARE HERE target) -- only the
    # open-loops listing itself must exclude it.
    assert "[revisit_condition] R-0001" not in output
    assert ascii_export.OPEN_LOOPS_HEADING not in output


def test_open_loops_shows_needs_review_and_merge_suggestions_from_extraction_runs(evidence):
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    session_id = conn.execute(
        "SELECT session_id FROM normalized_events WHERE id = ?", (ev,)
    ).fetchone()["session_id"]
    output_json = json.dumps({
        "needs_review": [
            {"description": "Unclear if O-0004 was ever really considered", "related_node_ids": ["O-0004"]},
        ],
        "merge_suggestions": [
            {"node_id_a": "O-0002", "node_id_b": "O-0004", "reason": "may be duplicates"},
        ],
    })
    conn.execute(
        "INSERT INTO extraction_runs (id, session_id, model, prompt_version, input_hash, output_json, status, created_at) "
        "VALUES ('run-1', ?, 'gpt-5.6-luna', 'p1', 'hash-1', ?, 'applied', '2026-01-01T00:00:00Z')",
        (session_id, output_json),
    )
    conn.commit()

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert ascii_export.OPEN_LOOPS_HEADING in output
    assert "  [needs_review] Unclear if O-0004 was ever really considered  (related: O-0004)" in output.splitlines()
    assert "  [merge_suggestion] O-0002 ~ O-0004: may be duplicates" in output.splitlines()


def test_open_loops_ignores_extraction_runs_from_other_projects(evidence):
    """An extraction run whose session never produced evidence for THIS
    project's nodes must not leak into this project's open-loops section --
    session_id alone isn't project-scoped in the schema, so the join through
    provenance is load-bearing, not decorative."""
    conn, project_id, ev = evidence
    _build_canonical_graph(conn, project_id, ev)

    output_json = json.dumps({
        "needs_review": [{"description": "Belongs to a different project entirely"}],
        "merge_suggestions": [],
    })
    conn.execute(
        "INSERT INTO extraction_runs (id, session_id, model, prompt_version, input_hash, output_json, status, created_at) "
        "VALUES ('run-2', 'some-unrelated-session', 'gpt-5.6-luna', 'p1', 'hash-2', ?, 'applied', '2026-01-01T00:00:00Z')",
        (output_json,),
    )
    conn.commit()

    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert "Belongs to a different project entirely" not in output


def test_empty_project_renders_placeholder_message(evidence):
    conn, project_id, _ev = evidence
    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert "no nodes" in output.lower()
