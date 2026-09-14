"""Render the graph_nodes/graph_edges tables as a horizontal (`tree`-style)
ASCII tree, for `trace-mind map` (see docs/architecture.md's slash-command
integrations section).

The graph is a DAG, not a strict tree (build plan section 24.4), and edge
direction is not a consistent parent/child convention in practice -- real
data in local/kgw-example/build.py has DERIVED_FROM pointing in both
"newer explains older" and "older produced newer" senses depending on the
pair. So this is deliberately a **connectivity-only BFS spanning tree**:
adjacency treats every edge as undirected for the purpose of deciding
who's whose child, and every rendered connector is annotated with the
*real* edge type and its *real* direction relative to the parent, so the
tree shape never misrepresents what the edge actually says.
"""
from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass

# Mirrors graphviz_export.py's _STATUS_COLOR semantic grouping (chosen=green,
# rejected/blocked=red, dormant/superseded=dim, completed=blue,
# exploring/open=yellow, candidate=magenta) as ANSI SGR codes instead of hex.
# Keep the two mappings' groupings in sync if either changes.
_STATUS_ANSI = {
    "chosen": "32",       # green
    "rejected": "31",     # red
    "blocked": "31",      # red
    "dormant": "2",        # dim
    "superseded": "2",     # dim
    "completed": "34",    # blue
    "exploring": "33",   # yellow
    "open": "33",        # yellow
    "candidate": "35",   # magenta
}
_RESET = "\x1b[0m"
_YOU_ARE_HERE_STYLE = "\x1b[1;7m"  # bold + reverse video
_ID_STYLE = "\x1b[1;36m"  # bold cyan, fixed regardless of status -- a stable
# color independent of node state, so the id token looks the same on every
# line and is easy to spot/select/copy-paste regardless of that node's status.
_DIM = "\x1b[2m"

# "Branch" children -- types that represent a genuine fork/alternative, for
# the per-parent "(N branches, M finished)" summary. Support/detail types
# (evidence, outcome, action, revisit_condition) attach to a branch but
# aren't one themselves, so they're excluded from the count on purpose.
_BRANCH_NODE_TYPES = {"option", "decision", "hypothesis", "experiment"}
_FINISHED_STATUSES = {"chosen", "rejected", "completed", "superseded"}

UNLINKED_HEADING = "(unlinked)"
OPEN_LOOPS_HEADING = "(open loops)"


@dataclass
class _Edge:
    source: str
    target: str
    type: str


def render_ascii(conn: sqlite3.Connection, project_id: str, *, use_color: bool) -> str:
    nodes = conn.execute(
        "SELECT id, type, title, status, updated_at, created_at FROM graph_nodes "
        "WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    edge_rows = conn.execute(
        "SELECT source_node_id, target_node_id, type FROM graph_edges WHERE project_id = ?",
        (project_id,),
    ).fetchall()

    if not nodes:
        return "(no nodes in this project's graph yet)"

    by_id = {n["id"]: n for n in nodes}
    edges = [_Edge(e["source_node_id"], e["target_node_id"], e["type"]) for e in edge_rows]

    adjacency: dict[str, list[_Edge]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e.source in adjacency:
            adjacency[e.source].append(e)
        if e.target in adjacency:
            # Store a reversed view too so BFS can walk either direction;
            # the *original* edge (source/target as declared) is kept so
            # the annotation always shows the real direction, not a
            # synthesized one.
            adjacency[e.target].append(e)

    you_are_here = max(nodes, key=lambda n: n["updated_at"])["id"]

    visited: set[str] = set()
    forest: list[tuple[str, dict[str, tuple[str, _Edge]]]] = []

    def bfs_from(root_id: str) -> dict[str, tuple[str, _Edge]]:
        """Returns child_id -> (parent_id, edge_used_to_reach_child)."""
        parent_of: dict[str, tuple[str, _Edge]] = {}
        queue: deque[str] = deque([root_id])
        visited.add(root_id)
        while queue:
            current = queue.popleft()
            for e in adjacency.get(current, []):
                other = e.target if e.source == current else e.source
                if other in visited or other not in by_id:
                    continue
                visited.add(other)
                parent_of[other] = (current, e)
                queue.append(other)
        return parent_of

    goal_roots = [n["id"] for n in nodes if n["type"] == "goal"]
    if not goal_roots:
        incoming = {e.target for e in edges}
        goal_roots = [n["id"] for n in nodes if n["id"] not in incoming]
    if not goal_roots:
        goal_roots = [nodes[0]["id"]]

    for root_id in goal_roots:
        if root_id in visited:
            continue
        forest.append((root_id, bfs_from(root_id)))

    unlinked_roots = [n["id"] for n in nodes if n["id"] not in visited]
    unlinked_forest: list[tuple[str, dict[str, tuple[str, _Edge]]]] = []
    for root_id in unlinked_roots:
        if root_id in visited:
            continue
        unlinked_forest.append((root_id, bfs_from(root_id)))

    lines: list[str] = [_render_breadcrumb(by_id, forest, unlinked_forest, you_are_here, use_color=use_color), ""]
    for root_id, parent_of in forest:
        _render_tree(lines, by_id, parent_of, root_id, "", you_are_here=you_are_here, use_color=use_color)

    if unlinked_forest:
        lines.append(UNLINKED_HEADING)
        for root_id, parent_of in unlinked_forest:
            _render_tree(lines, by_id, parent_of, root_id, "", you_are_here=you_are_here, use_color=use_color)

    open_loop_lines = _render_open_loops(conn, project_id, by_id)
    if open_loop_lines:
        lines.append("")
        lines.append(OPEN_LOOPS_HEADING)
        lines.extend(open_loop_lines)

    return "\n".join(lines)


def _render_breadcrumb(
    by_id: dict[str, sqlite3.Row],
    forest: list[tuple[str, dict[str, tuple[str, _Edge]]]],
    unlinked_forest: list[tuple[str, dict[str, tuple[str, _Edge]]]],
    you_are_here: str,
    *,
    use_color: bool,
) -> str:
    """'You are here: <root> -> ... -> <you_are_here>' -- the tree shows local
    siblings but not the route from the goal, which is what "where are we"
    actually means at a glance without scanning the whole tree for the
    highlighted line."""
    path = [you_are_here]
    for _root_id, parent_of in (*forest, *unlinked_forest):
        if you_are_here in parent_of:
            current = you_are_here
            while current in parent_of:
                current = parent_of[current][0]
                path.append(current)
            break
    path.reverse()

    if len(path) == 1:
        crumb = f"You are here: {path[0]}"
    else:
        crumb = "You are here: " + " → ".join(path)

    if not use_color:
        return crumb
    return f"{_YOU_ARE_HERE_STYLE}{crumb}{_RESET}"


def _render_open_loops(conn: sqlite3.Connection, project_id: str, by_id: dict[str, sqlite3.Row]) -> list[str]:
    """Surfaces the machine's own record of "things possibly missed":
    parked `revisit_condition` nodes (explicit REVISIT / PARK moments,
    prompt.py taxonomy item 6) plus any `needs_review`/`merge_suggestions`
    an extraction run declined to auto-apply (build plan section 15 -- these
    are never applied automatically, so without this section they sit
    unseen in `extraction_runs.output_json` forever). There is no
    resolved/dismissed tracking yet (build plan section 18, not built) --
    everything here is unconditionally still open every time `map` runs."""
    lines: list[str] = []

    for node in by_id.values():
        if node["type"] == "revisit_condition" and node["status"] == "open":
            lines.append(f"  [revisit_condition] {node['id']}: {node['title']}")

    runs = conn.execute(
        """
        SELECT DISTINCT er.output_json
        FROM extraction_runs er
        WHERE er.status = 'applied' AND er.output_json IS NOT NULL
          AND er.session_id IN (
            SELECT DISTINCT ne.session_id
            FROM provenance p
            JOIN normalized_events ne ON ne.id = p.normalized_event_id
            JOIN graph_nodes gn ON gn.id = p.object_id
            WHERE p.object_type = 'node' AND gn.project_id = ?
          )
        ORDER BY er.output_json
        """,
        (project_id,),
    ).fetchall()

    seen_review: set[str] = set()
    seen_merge: set[tuple[str, str]] = set()
    for row in runs:
        try:
            envelope = json.loads(row["output_json"])
        except (TypeError, ValueError):
            continue
        for nr in envelope.get("needs_review", []):
            description = nr.get("description", "")
            if description in seen_review:
                continue
            seen_review.add(description)
            related = nr.get("related_node_ids") or []
            suffix = f"  (related: {', '.join(related)})" if related else ""
            lines.append(f"  [needs_review] {description}{suffix}")
        for ms in envelope.get("merge_suggestions", []):
            key = tuple(sorted((ms.get("node_id_a", ""), ms.get("node_id_b", ""))))
            if key in seen_merge:
                continue
            seen_merge.add(key)
            reason = ms.get("reason")
            suffix = f": {reason}" if reason else ""
            lines.append(f"  [merge_suggestion] {ms.get('node_id_a')} ~ {ms.get('node_id_b')}{suffix}")

    return lines


def _render_tree(
    lines: list[str],
    by_id: dict[str, sqlite3.Row],
    parent_of: dict[str, tuple[str, _Edge]],
    node_id: str,
    prefix: str,
    *,
    you_are_here: str,
    use_color: bool,
    connector: str = "",
    edge: _Edge | None = None,
) -> None:
    node = by_id[node_id]
    is_here = node_id == you_are_here

    children = sorted(
        (child_id for child_id, (parent_id, _e) in parent_of.items() if parent_id == node_id),
        key=lambda cid: by_id[cid]["created_at"],
    )
    # Deterministic, not LLM-estimated: branch count and finished count are
    # exact facts already in the graph (child type + status), so counting
    # them is instant, free, and can't hallucinate -- an LLM call here would
    # be slower and less accurate than just counting, and `map` is meant to
    # be the fast, no-API-call view (see docs/architecture.md).
    branch_children = [cid for cid in children if by_id[cid]["type"] in _BRANCH_NODE_TYPES]
    branch_summary = None
    if branch_children:
        finished = sum(1 for cid in branch_children if by_id[cid]["status"] in _FINISHED_STATUSES)
        noun = "branch" if len(branch_children) == 1 else "branches"
        branch_summary = f"{len(branch_children)} {noun}, {finished} finished"

    # `prefix` is this node's own ancestor continuation bars (NOT including
    # its own connector); `connector` is this node's own "├── "/"└── "/"".
    # These must stay separate -- the prefix used for a node's OWN line and
    # the base prefix handed down to its CHILDREN are different strings
    # (the latter extends the former based on *this* node's position, not
    # each child's), conflating them was a real bug caught by manually
    # inspecting real rendered output against real data (see git log).
    #
    # The YOU ARE HERE line is always built from the PLAIN (uncolored) label
    # and wrapped in a single outer bold+reverse span. Building it from the
    # colored label instead would embed that label's own mid-string reset
    # codes, which -- since ANSI SGR state isn't scoped/nested, a `\x1b[0m`
    # cancels ALL active styling -- would cut the outer bold+reverse off
    # partway through the line instead of covering it, a real bug only
    # visible in actual rendered output, not in plain-text-only tests.
    if is_here:
        label = _format_node_label(node, edge, node_id, branch_summary, use_color=False)
        line = f"{prefix}{connector}{label} ◀── YOU ARE HERE"
        if use_color:
            line = f"{_YOU_ARE_HERE_STYLE}{line}{_RESET}"
    else:
        label = _format_node_label(node, edge, node_id, branch_summary, use_color=use_color)
        line = f"{prefix}{connector}{label}"
    lines.append(line)

    # Base prefix for this node's children: extend `prefix` by whether THIS
    # node was the last child of ITS OWN parent (root: no extension at all).
    if connector == "":
        children_base_prefix = prefix
    elif connector == "└── ":
        children_base_prefix = prefix + "    "
    else:
        children_base_prefix = prefix + "│   "

    for i, child_id in enumerate(children):
        is_last = i == len(children) - 1
        child_connector = "└── " if is_last else "├── "
        _render_tree(
            lines, by_id, parent_of, child_id, children_base_prefix,
            you_are_here=you_are_here, use_color=use_color,
            connector=child_connector, edge=parent_of[child_id][1],
        )


def _format_node_label(
    node: sqlite3.Row, edge: _Edge | None, node_id: str, branch_summary: str | None, *, use_color: bool
) -> str:
    """Id and type/status are each their own bracketed `[...]` tag -- always,
    even with color off -- so either token has a clean boundary to
    double-click/drag-select and copy, instead of running straight into
    adjacent text. With color on, each tag gets its own span rather than one
    color for the whole line: the id is a fixed color independent of status
    (a stable, recognizable copy target), the type/status tag carries the
    actual status color (it's literally what's stating the status), and the
    title stays uncolored for readability against every status color."""
    id_tag = f"[{node['id']}]"
    type_tag = f"[{node['type']}/{node['status']}]"

    annotation = ""
    if edge is not None:
        # Annotate with the edge's REAL declared direction relative to the
        # parent one line up in the tree, regardless of which way the BFS
        # walked to reach this child -- parent's id isn't repeated here,
        # it's already the line directly above at one less indent level.
        if edge.source == node_id:
            # This node is the edge's declared source -> it points away
            # from this node, toward the parent.
            annotation = f"  (─{edge.type}─▶ to parent)"
        else:
            # This node is the edge's declared target -> it points from
            # the parent into this node.
            annotation = f"  (◀─{edge.type}─ from parent)"

    branch_tag = f"  {{{branch_summary}}}" if branch_summary else ""

    if not use_color:
        return f"{id_tag} {type_tag} {node['title']}{annotation}{branch_tag}"

    colored_id = f"{_ID_STYLE}{id_tag}{_RESET}"
    color = _STATUS_ANSI.get(node["status"])
    colored_type = f"\x1b[{color}m{type_tag}{_RESET}" if color else type_tag
    colored_annotation = f"{_DIM}{annotation}{_RESET}" if annotation else ""
    colored_branch = f"{_DIM}{branch_tag}{_RESET}" if branch_tag else ""
    return f"{colored_id} {colored_type} {node['title']}{colored_annotation}{colored_branch}"
