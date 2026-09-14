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
    lines = output.splitlines()

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
    assert ascii_export.UNLINKED_HEADING in output
    heading_index = output.splitlines().index(ascii_export.UNLINKED_HEADING)
    stray_index = next(i for i, l in enumerate(output.splitlines()) if "O-9999" in l)
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
    lines = output.splitlines()
    # Parse assumes a single-token node id before "[" -- true for this
    # test's ids (A, B, A1, ...) but would break on a realistic id
    # containing a space or literal "[".
    by_content = {l.split("[")[0].strip().split()[-1]: l for l in lines}
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


def test_empty_project_renders_placeholder_message(evidence):
    conn, project_id, _ev = evidence
    output = ascii_export.render_ascii(conn, project_id, use_color=False)
    assert "no nodes" in output.lower()
