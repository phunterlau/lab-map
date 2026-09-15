"""Apply a validated `ExtractionEnvelope` to the graph.

Two layers of defense, deliberately redundant with `extraction/models.py`'s
field validators: this module still goes through
`graph.repository.add_node`/`add_edge`, the same functions the manual CLI
uses, so `MissingProvenanceError`/`InvalidOntologyError`/`UnknownNodeError`
stay the single real enforcement point (build plan section 24.1 -- the
repository, not the extractor, is the thing that must never be bypassed).

A single bad mutation (an edge referencing a node id that doesn't resolve,
say) does not fail the whole batch -- it's collected in `rejected` and
everything else still applies. `merge_suggestions`/`needs_review` are never
applied automatically (build plan section 15: "if uncertain, create a
merge_suggestion rather than silently merging") -- they're returned for a
human or the CLI to see.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from trace_mind.extraction import ids
from trace_mind.extraction.models import ExtractionEnvelope, MergeSuggestion, NeedsReview
from trace_mind.extraction.window import ExtractionWindow
from trace_mind.graph import repository as graph_repo


@dataclass
class ApplyResult:
    created_node_ids: dict[str, str] = field(default_factory=dict)
    """temp_id -> real node id, for anything downstream that wants to know
    what got minted this run."""
    nodes_created: int = 0
    nodes_updated: int = 0
    edges_created: int = 0
    rejected: list[str] = field(default_factory=list)
    merge_suggestions: list[MergeSuggestion] = field(default_factory=list)
    needs_review: list[NeedsReview] = field(default_factory=list)


def apply_envelope(
    conn: sqlite3.Connection,
    project_id: str,
    window: ExtractionWindow,
    envelope: ExtractionEnvelope,
    extractor_version: str,
) -> ApplyResult:
    result = ApplyResult(
        merge_suggestions=list(envelope.merge_suggestions),
        needs_review=list(envelope.needs_review),
    )

    def resolve_evidence(labels: list[str]) -> list[str] | None:
        ids_out = []
        for label in labels:
            real_id = window.event_labels.get(label)
            if real_id is None:
                return None
            ids_out.append(real_id)
        return ids_out

    def resolve_node_ref(ref: str) -> str | None:
        if ref in result.created_node_ids:
            return result.created_node_ids[ref]
        if graph_repo.get_node(conn, ref) is not None:
            return ref
        return None

    for cn in envelope.create_nodes:
        evidence_ids = resolve_evidence(cn.evidence_event_labels)
        if evidence_ids is None:
            result.rejected.append(f"create_node {cn.temp_id}: unresolvable evidence label")
            continue
        real_id = ids.allocate_node_id(conn, project_id, cn.type)
        try:
            graph_repo.add_node(
                conn, project_id, real_id, cn.type, cn.title,
                summary=cn.summary, status=cn.status, confidence=cn.confidence,
                evidence_event_ids=evidence_ids, extractor_version=extractor_version,
            )
        except (
            graph_repo.InvalidOntologyError,
            graph_repo.MissingProvenanceError,
            graph_repo.CrossProjectIdCollisionError,
        ) as exc:
            result.rejected.append(f"create_node {cn.temp_id}: {exc}")
            continue
        result.created_node_ids[cn.temp_id] = real_id
        result.nodes_created += 1

    for un in envelope.update_nodes:
        evidence_ids = resolve_evidence(un.evidence_event_labels)
        if evidence_ids is None:
            result.rejected.append(f"update_node {un.node_id}: unresolvable evidence label")
            continue
        existing = graph_repo.get_node(conn, un.node_id)
        if existing is None:
            result.rejected.append(f"update_node {un.node_id}: no such node")
            continue
        try:
            graph_repo.add_node(
                conn, project_id, un.node_id, existing["type"], existing["title"],
                summary=un.summary if un.summary is not None else existing["summary"],
                status=un.status if un.status is not None else existing["status"],
                confidence=un.confidence if un.confidence is not None else existing["confidence"],
                evidence_event_ids=evidence_ids, extractor_version=extractor_version,
            )
        except (
            graph_repo.InvalidOntologyError,
            graph_repo.MissingProvenanceError,
            graph_repo.CrossProjectIdCollisionError,
        ) as exc:
            result.rejected.append(f"update_node {un.node_id}: {exc}")
            continue
        result.nodes_updated += 1

    for ce in envelope.create_edges:
        evidence_ids = resolve_evidence(ce.evidence_event_labels)
        if evidence_ids is None:
            result.rejected.append(f"create_edge {ce.source}->{ce.target}: unresolvable evidence label")
            continue
        source = resolve_node_ref(ce.source)
        target = resolve_node_ref(ce.target)
        if source is None or target is None:
            result.rejected.append(f"create_edge {ce.source}->{ce.target}: unresolved node reference")
            continue
        try:
            graph_repo.add_edge(
                conn, project_id, source, target, ce.type,
                reason=ce.reason, confidence=ce.confidence,
                evidence_event_ids=evidence_ids, extractor_version=extractor_version,
            )
        except (
            graph_repo.InvalidOntologyError,
            graph_repo.MissingProvenanceError,
            graph_repo.UnknownNodeError,
            graph_repo.CrossProjectIdCollisionError,
        ) as exc:
            result.rejected.append(f"create_edge {ce.source}->{ce.target}: {exc}")
            continue
        result.edges_created += 1

    for s in envelope.supersede:
        evidence_ids = resolve_evidence(s.evidence_event_labels)
        if evidence_ids is None:
            result.rejected.append(f"supersede {s.old_node_id}<-{s.new_node_id}: unresolvable evidence label")
            continue
        old_id = resolve_node_ref(s.old_node_id)
        new_id = resolve_node_ref(s.new_node_id)
        if old_id is None or new_id is None:
            result.rejected.append(f"supersede {s.old_node_id}<-{s.new_node_id}: unresolved node reference")
            continue
        old_node = graph_repo.get_node(conn, old_id)
        try:
            graph_repo.add_edge(
                conn, project_id, new_id, old_id, "SUPERSEDES",
                reason=s.reason, evidence_event_ids=evidence_ids, extractor_version=extractor_version,
            )
            graph_repo.add_node(
                conn, project_id, old_id, old_node["type"], old_node["title"],
                summary=old_node["summary"], status="superseded", confidence=old_node["confidence"],
                evidence_event_ids=evidence_ids, extractor_version=extractor_version,
            )
        except (
            graph_repo.InvalidOntologyError,
            graph_repo.MissingProvenanceError,
            graph_repo.UnknownNodeError,
            graph_repo.CrossProjectIdCollisionError,
        ) as exc:
            result.rejected.append(f"supersede {s.old_node_id}<-{s.new_node_id}: {exc}")
            continue
        result.edges_created += 1

    return result
