"""Graph storage operations (build plan Milestone 4).

Manual/CLI-driven for now -- the LLM extractor (Milestone 5) will call the
same functions, which is why every mutation here takes explicit evidence
event ids rather than trusting free-text. "Reject any mutation without
provenance" (build plan section 14) is enforced by `add_node` requiring at
least one evidence event unless the caller passes `allow_no_evidence`.
"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone

from trace_mind.graph.models import EDGE_TYPES, NODE_STATUSES, NODE_TYPES


class UnknownNodeError(ValueError):
    pass


class MissingProvenanceError(ValueError):
    pass


class InvalidOntologyError(ValueError):
    """type_/status isn't in the bounded ontology (graph/models.py). Catching
    this at write time, not at Markdown-render time, is the point: an
    extractor (Milestone 5) that invents a node type should fail loudly
    here rather than silently producing an unrenderable graph later."""


def ensure_project(conn: sqlite3.Connection, root_path: str, name: str | None = None) -> str:
    row = conn.execute("SELECT id FROM projects WHERE root_path = ?", (root_path,)).fetchone()
    if row:
        return row["id"]
    project_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO projects (id, root_path, name, created_at) VALUES (?, ?, ?, ?)",
        (project_id, root_path, name or root_path, _now()),
    )
    conn.commit()
    return project_id


def add_node(
    conn: sqlite3.Connection,
    project_id: str,
    node_id: str,
    type_: str,
    title: str,
    *,
    summary: str | None = None,
    status: str = "open",
    confidence: float | None = None,
    evidence_event_ids: list[str] | None = None,
    extractor_version: str = "manual",
    allow_no_evidence: bool = False,
) -> None:
    if type_ not in NODE_TYPES:
        raise InvalidOntologyError(f"unknown node type {type_!r}; expected one of {sorted(NODE_TYPES)}")
    if status not in NODE_STATUSES:
        raise InvalidOntologyError(f"unknown node status {status!r}; expected one of {sorted(NODE_STATUSES)}")

    evidence_event_ids = evidence_event_ids or []
    if not evidence_event_ids and not allow_no_evidence:
        raise MissingProvenanceError(
            f"node {node_id!r} has no evidence_event_ids; pass allow_no_evidence=True "
            "only for structural/organizational nodes with no transcript source"
        )

    now = _now()
    with conn:
        conn.execute(
            """
            INSERT INTO graph_nodes (id, project_id, type, title, summary, status, confidence, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                type = excluded.type,
                title = excluded.title,
                summary = excluded.summary,
                status = excluded.status,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (node_id, project_id, type_, title, summary, status, confidence, now, now),
        )
        for event_id in evidence_event_ids:
            _attach_provenance(conn, "node", node_id, event_id, extractor_version, now)


def add_edge(
    conn: sqlite3.Connection,
    project_id: str,
    source_id: str,
    target_id: str,
    type_: str,
    *,
    reason: str | None = None,
    confidence: float | None = None,
    evidence_event_ids: list[str] | None = None,
    extractor_version: str = "manual",
    allow_no_evidence: bool = False,
) -> str:
    if type_ not in EDGE_TYPES:
        raise InvalidOntologyError(f"unknown edge type {type_!r}; expected one of {sorted(EDGE_TYPES)}")

    for nid in (source_id, target_id):
        if get_node(conn, nid) is None:
            raise UnknownNodeError(f"edge references unknown node {nid!r}; create it first")

    evidence_event_ids = evidence_event_ids or []
    if not evidence_event_ids and not allow_no_evidence:
        raise MissingProvenanceError(
            f"edge {source_id}->{target_id} ({type_}) has no evidence_event_ids"
        )

    edge_id = f"{source_id}|{type_}|{target_id}"
    now = _now()
    with conn:
        conn.execute(
            """
            INSERT INTO graph_edges (id, project_id, source_node_id, target_node_id, type, reason, confidence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                reason = excluded.reason,
                confidence = excluded.confidence
            """,
            (edge_id, project_id, source_id, target_id, type_, reason, confidence, now),
        )
        for event_id in evidence_event_ids:
            _attach_provenance(conn, "edge", edge_id, event_id, extractor_version, now)
    return edge_id


def get_node(conn: sqlite3.Connection, node_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM graph_nodes WHERE id = ?", (node_id,)).fetchone()


def list_nodes(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM graph_nodes WHERE project_id = ? ORDER BY created_at", (project_id,)
    ).fetchall()


def edges_touching(conn: sqlite3.Connection, node_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM graph_edges WHERE source_node_id = ? OR target_node_id = ?",
        (node_id, node_id),
    ).fetchall()


def provenance_for(conn: sqlite3.Connection, object_type: str, object_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT p.*, e.session_id AS event_session_id, e.transcript_path, e.text AS event_text,
               e.role AS event_role, e.event_type AS event_kind, e.timestamp AS event_timestamp
        FROM provenance p
        LEFT JOIN normalized_events e ON e.id = p.normalized_event_id
        WHERE p.object_type = ? AND p.object_id = ?
        ORDER BY p.created_at
        """,
        (object_type, object_id),
    ).fetchall()


def _attach_provenance(
    conn: sqlite3.Connection, object_type: str, object_id: str, normalized_event_id: str,
    extractor_version: str, now: str,
) -> None:
    event = conn.execute(
        "SELECT session_id, turn_id, byte_start, byte_end, content_hash FROM normalized_events WHERE id = ?",
        (normalized_event_id,),
    ).fetchone()
    if event is None:
        raise MissingProvenanceError(f"no normalized_event with id {normalized_event_id!r} -- ingest it first")

    prov_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO provenance (
            id, object_type, object_id, session_id, normalized_event_id, turn_id,
            byte_start, byte_end, excerpt_hash, extractor_version, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            prov_id, object_type, object_id, event["session_id"], normalized_event_id,
            event["turn_id"], event["byte_start"], event["byte_end"], event["content_hash"],
            extractor_version, now,
        ),
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
