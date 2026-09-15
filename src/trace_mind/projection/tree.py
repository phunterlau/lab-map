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
from datetime import datetime, timezone

from trace_mind.graph import repository as graph_repo

# "Branch" node types -- a genuine fork/alternative, as opposed to a
# support/detail type (evidence, outcome, action, revisit_condition) that
# attaches to a branch but isn't one itself. Shared by ascii_export.py's
# per-parent "(N branches, M finished)" summary and tree.py's own
# unresolved-sibling finding (both need the same notion of "branch").
BRANCH_NODE_TYPES = {"option", "decision", "hypothesis", "experiment"}
FINISHED_STATUSES = {"chosen", "rejected", "completed", "superseded"}


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
    # node_id -> a real-world ISO timestamp to *display* at that node: its
    # latest evidence event's actual transcript time if it has provenance,
    # else its own graph row's updated_at. See _evidence_timestamps' own
    # docstring for why evidence is preferred when available.
    node_timestamp: dict[str, str] = field(default_factory=dict)


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

    evidence_ts = _evidence_timestamps(conn, nodes)
    # Evidence timestamp first, updated_at only as a fallback for nodes
    # with no evidence at all -- NOT the reverse. Turns out `updated_at`
    # isn't the reliable primary signal it looks like: two graph_nodes rows
    # written moments apart in the same Python loop (a batch build script,
    # or one extraction run applying several mutations) get DIFFERENT
    # microsecond-precision updated_at values ordered by *write order*, not
    # by anything about the underlying conversation. Confirmed on the real
    # local/kgw-example DB: R-KGW-SIGN and O-KGW-DP's updated_at values
    # aren't a tie at all (differ by ~5ms, in build.py's node-list order),
    # so a plain max(updated_at) picked O-KGW-DP even though R-KGW-SIGN's
    # real evidence event is chronologically a full day later. Evidence
    # timestamp doesn't have this problem: it's the original transcript
    # event time, which only changes when a node is genuinely re-cited.
    node_timestamp = {n["id"]: evidence_ts.get(n["id"]) or n["updated_at"] for n in nodes}
    you_are_here = max(nodes, key=lambda n: node_timestamp[n["id"]])["id"]

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
        node_timestamp=node_timestamp,
    )


def _evidence_timestamps(conn: sqlite3.Connection, nodes: list[sqlite3.Row]) -> dict[str, str | None]:
    """Each node's latest evidence event's real transcript timestamp
    (`normalized_events.timestamp`), where it has any provenance at all.
    Used in preference to `graph_nodes.updated_at` for both YOU-ARE-HERE
    and each node's displayed age: `updated_at` is only ever a *write*
    time, ordered by whatever order the writing code happened to touch
    rows in -- which, inside one script run or one extraction transaction,
    can be microseconds apart and completely unrelated to real
    conversation recency. `normalized_events.timestamp` is the original
    transcript event time, which only changes when a node is genuinely
    re-cited, and (per storage/db.py's own schema comment) is the only
    ordering valid *across* transcript files, unlike raw byte_start. Found
    via a real case: local/kgw-example's R-KGW-SIGN and O-KGW-DP's
    updated_at values differ by ~5ms -- not a tie, just build.py's Python
    loop insertion order -- so a plain max(updated_at) silently favored
    O-KGW-DP even though R-KGW-SIGN's real evidence event is a full day
    later."""
    node_ids = [n["id"] for n in nodes]
    placeholders = ",".join("?" * len(node_ids))
    rows = conn.execute(
        f"""
        SELECT p.object_id, MAX(ne.timestamp) AS latest_ts
        FROM provenance p
        JOIN normalized_events ne ON ne.id = p.normalized_event_id
        WHERE p.object_type = 'node' AND p.object_id IN ({placeholders}) AND ne.timestamp IS NOT NULL
        GROUP BY p.object_id
        """,
        node_ids,
    ).fetchall()
    return {r["object_id"]: r["latest_ts"] for r in rows}


def format_relative_age(iso_ts: str | None, *, now: datetime | None = None) -> str | None:
    """Brief, human relative age ("3d ago", "2h ago") for one of
    TreeResult.node_timestamp's real-world ISO timestamps -- lets staleness
    be read at a glance instead of requiring the viewer to do date
    arithmetic on a raw timestamp themselves. Single coarsest unit, not a
    breakdown (matches the "brief" ask -- "3d ago" not "3 days, 4 hours, 12
    minutes ago"). Returns None for a missing/unparseable timestamp (a
    structural node written with allow_no_evidence, or legacy data) so
    callers can omit the tag entirely instead of showing "None ago"."""
    if not iso_ts:
        return None
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)

    now = now or datetime.now(timezone.utc)
    seconds = max((now - ts).total_seconds(), 0)

    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d ago"
    months = days / 30
    if months < 12:
        return f"{int(months)}mo ago"
    years = days / 365
    return f"{int(years)}y ago"


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


#: Stable rendering order for `Finding.kind` -- unrecognized kinds (there
#: shouldn't be any) sort last rather than erroring.
FINDING_ORDER = [
    "revisit_condition",
    "dormant_unresolved",
    "experiment_no_outcome",
    "unresolved_sibling",
    "contradicted_but_chosen",
    "needs_review",
    "merge_suggestion",
]

# Edge types that formally close out a dormant/rejected option or
# hypothesis -- an incoming edge of one of these means *someone already
# made the call*, even if the node's own status is still "dormant" rather
# than "rejected". Absence of any of these is what makes a dormant node an
# actual open loop instead of a resolved one.
_CLOSING_EDGE_TYPES = {"REJECTED_BECAUSE", "CHOSEN_OVER", "SUPERSEDES"}

# Edge types that connect a parent to a genuine branch/alternative child --
# same "this represents a fork" notion BRANCH_NODE_TYPES captures for node
# types, but for the edge that fans out to them.
_FAN_OUT_EDGE_TYPES = {"EXPLORES", "ALTERNATIVE_TO"}


@dataclass
class Finding:
    """One "thing possibly missed" -- a uniform shape so a new check is a
    pure addition to `collect_findings` with zero renderer changes, instead
    of every check needing its own field threaded through both
    `ascii_export.py` and `lab_notebook_export.py`. `kind` is a stable slug
    (see FINDING_ORDER); `node_id` is the primary node the finding is
    about, or None for a finding that isn't about one specific node
    (`needs_review`, which is free text from an extraction run); `text` is
    the fully-formatted human-readable line, built once here rather than
    reassembled per renderer."""

    kind: str
    node_id: str | None
    text: str


def collect_findings(
    conn: sqlite3.Connection,
    project_id: str,
    by_id: dict[str, sqlite3.Row],
    node_timestamp: dict[str, str] | None = None,
) -> list[Finding]:
    """The machine's own record of "things possibly missed" -- shared data
    source for both `ascii_export.py`'s text listing and
    `lab_notebook_export.py`'s panel, one query per check, many renderings.
    Every check here is derived from real graph shape (node type/status +
    edge type), not a heuristic guess -- see docs/architecture.md for the
    real KGW case each one was validated against. `node_timestamp`
    (TreeResult.node_timestamp) is optional so callers that only have
    `by_id` still work; when given, every node-anchored finding's text
    carries a brief relative age ("21d ago") -- staleness is exactly the
    signal that makes a finding worth acting on over just noting it."""
    node_timestamp = node_timestamp or {}

    def _tag(node_id: str) -> str:
        age = format_relative_age(node_timestamp.get(node_id))
        return f"  [{age}]" if age else ""

    findings: list[Finding] = []

    edge_rows = conn.execute(
        "SELECT source_node_id, target_node_id, type FROM graph_edges WHERE project_id = ?",
        (project_id,),
    ).fetchall()
    incoming: dict[str, list[sqlite3.Row]] = {}
    outgoing: dict[str, list[sqlite3.Row]] = {}
    for e in edge_rows:
        incoming.setdefault(e["target_node_id"], []).append(e)
        outgoing.setdefault(e["source_node_id"], []).append(e)

    for node in by_id.values():
        node_id = node["id"]

        if node["type"] == "revisit_condition" and node["status"] == "open":
            findings.append(Finding("revisit_condition", node_id, f"{node_id}: {node['title']}{_tag(node_id)}"))

        if node["type"] in ("option", "hypothesis") and node["status"] == "dormant":
            closed = any(e["type"] in _CLOSING_EDGE_TYPES for e in incoming.get(node_id, []))
            if not closed:
                findings.append(
                    Finding("dormant_unresolved", node_id, f"{node_id}: {node['title']}{_tag(node_id)}")
                )

        if node["type"] == "experiment" and node["status"] == "completed":
            # PRODUCED's real direction convention (both instances in
            # local/kgw-example/build.py, including the one deliberately
            # NOT added because it was factually wrong) is outcome -source->
            # experiment/option -target-, i.e. "this outcome came FROM this
            # experiment" -- so an experiment's outcome is an *incoming*
            # PRODUCED edge, not outgoing.
            produced = any(e["type"] == "PRODUCED" for e in incoming.get(node_id, []))
            if not produced:
                findings.append(
                    Finding("experiment_no_outcome", node_id, f"{node_id}: {node['title']}{_tag(node_id)}")
                )

        if node["type"] in BRANCH_NODE_TYPES and node["status"] in ("chosen", "exploring"):
            contradicted_by = next(
                (e["source_node_id"] for e in incoming.get(node_id, []) if e["type"] == "CONTRADICTS"), None
            )
            if contradicted_by is not None:
                findings.append(
                    Finding(
                        "contradicted_but_chosen", node_id,
                        f"{node_id}: {node['title']}  (contradicted by {contradicted_by}){_tag(node_id)}",
                    )
                )

    findings.extend(_collect_unresolved_siblings(by_id, outgoing, node_timestamp))
    findings.extend(_collect_needs_review_and_merge_suggestions(conn, project_id))

    order_index = {kind: i for i, kind in enumerate(FINDING_ORDER)}
    findings.sort(key=lambda f: order_index.get(f.kind, len(FINDING_ORDER)))
    return findings


def _collect_unresolved_siblings(
    by_id: dict[str, sqlite3.Row], outgoing: dict[str, list[sqlite3.Row]], node_timestamp: dict[str, str]
) -> list[Finding]:
    """A parent fanned out (EXPLORES/ALTERNATIVE_TO) into >=2 branch
    children, one of which is already resolved (chosen/rejected/completed/
    superseded) -- this is the structural shape of "proceeded down one
    branch". Restricted to siblings that are themselves `open` or
    `dormant`: a sibling that's `exploring` has its own activity and isn't
    a forgotten thread, it's a second thing currently being worked (flagged
    only if it later goes stale, not just for existing in parallel)."""
    findings: list[Finding] = []
    for parent_id, edges in outgoing.items():
        children = [
            e["target_node_id"] for e in edges
            if e["type"] in _FAN_OUT_EDGE_TYPES and e["target_node_id"] in by_id
            and by_id[e["target_node_id"]]["type"] in BRANCH_NODE_TYPES
        ]
        if len(children) < 2:
            continue
        resolved = [cid for cid in children if by_id[cid]["status"] in FINISHED_STATUSES]
        if not resolved:
            continue
        for cid in children:
            status = by_id[cid]["status"]
            if status in ("open", "dormant"):
                age = format_relative_age(node_timestamp.get(cid))
                age_tag = f"  [{age}]" if age else ""
                findings.append(
                    Finding(
                        "unresolved_sibling", cid,
                        f"{cid}: {by_id[cid]['title']}  "
                        f"(sibling of {resolved[0]} under {parent_id}, still {status}){age_tag}",
                    )
                )
    return findings


def _collect_needs_review_and_merge_suggestions(conn: sqlite3.Connection, project_id: str) -> list[Finding]:
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

    findings: list[Finding] = []
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
            findings.append(Finding("needs_review", None, f"{description}{suffix}"))
        for ms in envelope.get("merge_suggestions", []):
            a, b = ms.get("node_id_a", ""), ms.get("node_id_b", "")
            key = tuple(sorted((a, b)))
            if key in seen_merge:
                continue
            seen_merge.add(key)
            reason = ms.get("reason")
            suffix = f": {reason}" if reason else ""
            findings.append(Finding("merge_suggestion", None, f"{a} ~ {b}{suffix}"))

    return findings
