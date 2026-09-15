"""Shared graph-shape + node-detail builders used by every projection
(`ascii_export.py`, `html_export.py`, `lab_notebook_export.py`,
`lattice_layout.py`). Extracted so the tree shape and node detail are each
computed exactly once, not re-derived per exporter -- a divergence between
two projections should mean a real bug, not just a different reimplementation
of the same query.

The graph is a DAG, not a strict tree (build plan section 24.4), and edge
direction is not a consistent parent/child convention in practice -- real
data in local/kgw-example/build.py has DERIVED_FROM pointing in both
"newer explains older" and "older produced newer" senses depending on the
pair. So `build_forest` is deliberately a **connectivity-only BFS spanning
tree**: adjacency treats every edge as undirected for the purpose of
deciding who's whose child; callers that render connectors are expected to
still show the *real* edge type/direction (kept on `Edge`), so the tree
shape never misrepresents what an edge actually says.
"""
from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field

from trace_mind.graph import repository as graph_repo


@dataclass
class Edge:
    source: str
    target: str
    type: str


# child_id -> (parent_id, edge_used_to_reach_child)
ParentOf = dict[str, tuple[str, Edge]]


@dataclass
class TreeResult:
    nodes: list[sqlite3.Row] = field(default_factory=list)
    by_id: dict[str, sqlite3.Row] = field(default_factory=dict)
    # Each entry is (root_id, parent_of) -- one per goal root (or fallback
    # root when there are no goal-type nodes).
    forest: list[tuple[str, ParentOf]] = field(default_factory=list)
    # Same shape, for anything no goal-rooted BFS ever reached.
    unlinked_forest: list[tuple[str, ParentOf]] = field(default_factory=list)
    you_are_here: str | None = None


def build_forest(conn: sqlite3.Connection, project_id: str) -> TreeResult:
    nodes = conn.execute(
        "SELECT id, type, title, status, updated_at, created_at FROM graph_nodes "
        "WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    if not nodes:
        return TreeResult()

    edge_rows = conn.execute(
        "SELECT source_node_id, target_node_id, type FROM graph_edges WHERE project_id = ?",
        (project_id,),
    ).fetchall()

    by_id = {n["id"]: n for n in nodes}
    edges = [Edge(e["source_node_id"], e["target_node_id"], e["type"]) for e in edge_rows]

    adjacency: dict[str, list[Edge]] = {n["id"]: [] for n in nodes}
    for e in edges:
        if e.source in adjacency:
            adjacency[e.source].append(e)
        if e.target in adjacency:
            # Store a reversed view too so BFS can walk either direction;
            # the *original* edge (source/target as declared) is kept so
            # a renderer's annotation always shows the real direction, not
            # a synthesized one.
            adjacency[e.target].append(e)

    you_are_here = max(nodes, key=lambda n: n["updated_at"])["id"]

    visited: set[str] = set()

    def bfs_from(root_id: str) -> ParentOf:
        parent_of: ParentOf = {}
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

    forest: list[tuple[str, ParentOf]] = []
    for root_id in goal_roots:
        if root_id in visited:
            continue
        forest.append((root_id, bfs_from(root_id)))

    unlinked_roots = [n["id"] for n in nodes if n["id"] not in visited]
    unlinked_forest: list[tuple[str, ParentOf]] = []
    for root_id in unlinked_roots:
        if root_id in visited:
            continue
        unlinked_forest.append((root_id, bfs_from(root_id)))

    return TreeResult(
        nodes=nodes, by_id=by_id, forest=forest, unlinked_forest=unlinked_forest, you_are_here=you_are_here,
    )


def sorted_children(tree: TreeResult, parent_of: ParentOf, node_id: str) -> list[str]:
    """Children of `node_id` within one `(root_id, parent_of)` tree, ordered
    by `created_at` -- the same ordering every renderer uses for siblings."""
    return sorted(
        (child_id for child_id, (parent_id, _e) in parent_of.items() if parent_id == node_id),
        key=lambda cid: tree.by_id[cid]["created_at"],
    )


def build_node_snapshot(conn: sqlite3.Connection, project_id: str) -> dict[str, dict]:
    """type/title/summary/status/confidence/edges/provenance per node --
    the JSON shape both `html_export.py` (embedded inline) and
    `trace-mind graph export --json` (written standalone) need. Single-hop
    only (a node's own edges + provenance), matching what `trace-mind why
    <node>` already prints -- not a recursive ancestor walk."""
    node_data: dict[str, dict] = {}
    for n in graph_repo.list_nodes(conn, project_id):
        node_id = n["id"]
        edges = graph_repo.edges_touching(conn, node_id)
        prov = graph_repo.provenance_for(conn, "node", node_id)

        node_data[node_id] = {
            "type": n["type"],
            "title": n["title"],
            "summary": n["summary"],
            "status": n["status"],
            "confidence": n["confidence"],
            "edges": [
                {
                    "direction": "->" if e["source_node_id"] == node_id else "<-",
                    "type": e["type"],
                    "other": e["target_node_id"] if e["source_node_id"] == node_id else e["source_node_id"],
                    "reason": e["reason"],
                }
                for e in edges
            ],
            "provenance": [
                {
                    "session_id": p["event_session_id"],
                    "turn_id": p["turn_id"],
                    "byte_start": p["byte_start"],
                    "byte_end": p["byte_end"],
                    "excerpt": (p["event_text"] or "")[:280],
                }
                for p in prov
            ],
        }
    return node_data


@dataclass
class OpenLoops:
    """The machine's own record of "things possibly missed": parked
    `revisit_condition` nodes (explicit REVISIT / PARK moments, prompt.py
    taxonomy item 6) plus any `needs_review`/`merge_suggestions` an
    extraction run declined to auto-apply (build plan section 15 -- these
    are never applied automatically, so without surfacing them they sit
    unseen in `extraction_runs.output_json` forever). There is no
    resolved/dismissed tracking yet (build plan section 18, not built) --
    everything here is unconditionally still open every time this is
    collected. Shared data source for both `ascii_export.py`'s text
    listing and `lab_notebook_export.py`'s panel -- one query, two
    renderings."""

    revisit_conditions: list[sqlite3.Row] = field(default_factory=list)
    needs_review: list[dict] = field(default_factory=list)  # {"description", "related_node_ids"}
    merge_suggestions: list[dict] = field(default_factory=list)  # {"node_id_a", "node_id_b", "reason"}

    def is_empty(self) -> bool:
        return not (self.revisit_conditions or self.needs_review or self.merge_suggestions)


def collect_open_loops(conn: sqlite3.Connection, project_id: str, by_id: dict[str, sqlite3.Row]) -> OpenLoops:
    loops = OpenLoops()

    for node in by_id.values():
        if node["type"] == "revisit_condition" and node["status"] == "open":
            loops.revisit_conditions.append(node)

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
            loops.needs_review.append(
                {"description": description, "related_node_ids": nr.get("related_node_ids") or []}
            )
        for ms in envelope.get("merge_suggestions", []):
            key = tuple(sorted((ms.get("node_id_a", ""), ms.get("node_id_b", ""))))
            if key in seen_merge:
                continue
            seen_merge.add(key)
            loops.merge_suggestions.append(
                {"node_id_a": ms.get("node_id_a"), "node_id_b": ms.get("node_id_b"), "reason": ms.get("reason")}
            )

    return loops
