"""Build the LLM extractor's input (build plan section 14, inputs A-E).

A. existing local graph neighborhood -- all `goal` nodes for this project
   (regardless of recency -- see build_window's comment) plus the most
   recently touched non-goal nodes, plus the edges between them. No
   FTS/embedding ranking yet (build plan section 15 explicitly defers
   that); recency is the v1 proxy for everything except goals.
B. new normalized transcript delta -- the events this run was triggered by.
C. ontology -- the bounded node/edge/status vocabulary (graph.models).
D. extraction rules -- the moment taxonomy (prompt.py).
E. provenance ids -- but never the real ids. Each event gets a short label
   ("e3") local to this window; the model cites labels, `apply.py` resolves
   them back to real `normalized_events.id`. This means the model
   structurally cannot cite an id it wasn't given -- unresolvable labels are
   a validation error, not a trust-the-model problem.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

NEIGHBORHOOD_SIZE = 15
"""How many recently-touched nodes to include as context. No ranking beyond
recency in v1 -- see module docstring."""


@dataclass
class ExtractionWindow:
    project_id: str
    session_id: str
    events: list[sqlite3.Row]
    event_labels: dict[str, str] = field(default_factory=dict)
    """label ("e1") -> real normalized_events.id"""
    neighborhood_nodes: list[sqlite3.Row] = field(default_factory=list)
    neighborhood_edges: list[sqlite3.Row] = field(default_factory=list)
    prompt_version: str = "p1"

    @property
    def input_hash(self) -> str:
        """Identity for `extraction_runs` idempotence: depends only on which
        real events went in and the prompt version, NOT on the graph
        neighborhood. Neighborhood is context, not identity -- applying a
        run's mutations changes which nodes are "recently touched", so
        hashing the neighborhood in would make re-running the exact same
        delta hash differently right after its own first successful run,
        breaking idempotence for the one case that matters most (a Stop
        hook firing extraction twice for the same delta because a retry
        raced with the first attempt)."""
        h = hashlib.sha256()
        h.update(self.prompt_version.encode("utf-8"))
        for event_id in sorted(self.event_labels.values()):
            h.update(b"\x00e:")
            h.update(event_id.encode("utf-8"))
        return h.hexdigest()

    def render_events_block(self) -> str:
        lines = []
        for label, event_id in self.event_labels.items():
            row = next(e for e in self.events if e["id"] == event_id)
            text = (row["text"] or "").strip().replace("\n", " ")
            if len(text) > 600:
                text = text[:600] + "…"
            lines.append(f"[{label}] ({row['role'] or row['event_type']}) {text}")
        return "\n".join(lines)

    def render_neighborhood_block(self) -> str:
        if not self.neighborhood_nodes:
            return "(no existing nodes yet -- this is a fresh graph)"
        by_id = {n["id"]: n for n in self.neighborhood_nodes}
        lines = [
            f"- {n['id']} [{n['type']}/{n['status']}] {n['title']}"
            for n in self.neighborhood_nodes
        ]
        for e in self.neighborhood_edges:
            if e["source_node_id"] in by_id and e["target_node_id"] in by_id:
                lines.append(f"  {e['source_node_id']} -{e['type']}-> {e['target_node_id']}")
        return "\n".join(lines)


def build_window(
    conn: sqlite3.Connection,
    project_id: str,
    session_id: str,
    new_event_ids: list[str],
    *,
    prompt_version: str = "p1",
) -> ExtractionWindow:
    if not new_event_ids:
        raise ValueError("build_window called with no new_event_ids")

    placeholders = ",".join("?" * len(new_event_ids))
    events = conn.execute(
        f"SELECT * FROM normalized_events WHERE id IN ({placeholders}) "
        "ORDER BY byte_start, event_index",
        new_event_ids,
    ).fetchall()

    event_labels = {f"e{i + 1}": row["id"] for i, row in enumerate(events)}

    # Goals are always included regardless of recency, unioned with the
    # recency-ranked rest. Found by running a real extraction pass: a goal
    # node, set once near the start of a project and never updated again,
    # got pushed out of a pure-recency top-N window by ordinary churn --
    # exactly the node the extractor needed to tell NEW_GOAL apart from
    # EXPLICIT_PARALLEL_BRANCH, and exactly the node type least likely to
    # look "recent". There are normally few goals per project, so this
    # doesn't meaningfully change the window's size or cost.
    goal_nodes = conn.execute(
        "SELECT * FROM graph_nodes WHERE project_id = ? AND type = 'goal' ORDER BY created_at",
        (project_id,),
    ).fetchall()
    recent_nodes = conn.execute(
        "SELECT * FROM graph_nodes WHERE project_id = ? ORDER BY updated_at DESC LIMIT ?",
        (project_id, NEIGHBORHOOD_SIZE),
    ).fetchall()
    seen_ids: set[str] = set()
    neighborhood_nodes: list[sqlite3.Row] = []
    for node in (*goal_nodes, *recent_nodes):
        if node["id"] not in seen_ids:
            seen_ids.add(node["id"])
            neighborhood_nodes.append(node)
    node_ids = [n["id"] for n in neighborhood_nodes]
    neighborhood_edges: list[sqlite3.Row] = []
    if node_ids:
        placeholders2 = ",".join("?" * len(node_ids))
        neighborhood_edges = conn.execute(
            f"SELECT * FROM graph_edges WHERE project_id = ? "
            f"AND (source_node_id IN ({placeholders2}) OR target_node_id IN ({placeholders2}))",
            [project_id, *node_ids, *node_ids],
        ).fetchall()

    return ExtractionWindow(
        project_id=project_id,
        session_id=session_id,
        events=events,
        event_labels=event_labels,
        neighborhood_nodes=neighborhood_nodes,
        neighborhood_edges=neighborhood_edges,
        prompt_version=prompt_version,
    )
