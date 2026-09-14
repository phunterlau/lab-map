"""LLM graph-diff output envelope (build plan section 14).

The extractor must emit graph mutations, not summaries. Every node/edge
references evidence by *label* (`"e3"`), not by real `normalized_events.id`
-- the LLM never sees real event ids (they're long hashes), only the short
per-window labels `window.py` assigns, so it structurally cannot invent an
id that happens to collide with a real one. `apply.py` resolves labels back
to real ids and rejects any label that isn't in the window it was given.

Enum-like fields (`type`, `status`) are validated against the same
`graph.models` ontology `graph.repository` enforces, so a malformed type
fails here -- at parse time -- rather than reaching `InvalidOntologyError`
deeper in the stack. This is deliberately duplicated validation: failing
close to the LLM output makes `needs_review` diagnostics point at "the
model emitted an unknown type" instead of a generic repository error.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from trace_mind.graph.models import EDGE_TYPES, NODE_STATUSES, NODE_TYPES


class CreateNode(BaseModel):
    temp_id: str
    type: str
    title: str
    summary: str | None = None
    status: str = "open"
    confidence: float | None = None
    evidence_event_labels: list[str] = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in NODE_TYPES:
            raise ValueError(f"unknown node type {v!r}; expected one of {sorted(NODE_TYPES)}")
        return v

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str) -> str:
        if v not in NODE_STATUSES:
            raise ValueError(f"unknown node status {v!r}; expected one of {sorted(NODE_STATUSES)}")
        return v


class UpdateNode(BaseModel):
    node_id: str
    status: str | None = None
    summary: str | None = None
    confidence: float | None = None
    evidence_event_labels: list[str] = Field(min_length=1)

    @field_validator("status")
    @classmethod
    def _valid_status(cls, v: str | None) -> str | None:
        if v is not None and v not in NODE_STATUSES:
            raise ValueError(f"unknown node status {v!r}; expected one of {sorted(NODE_STATUSES)}")
        return v


class CreateEdge(BaseModel):
    source: str
    target: str
    type: str
    reason: str | None = None
    confidence: float | None = None
    evidence_event_labels: list[str] = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def _valid_type(cls, v: str) -> str:
        if v not in EDGE_TYPES:
            raise ValueError(f"unknown edge type {v!r}; expected one of {sorted(EDGE_TYPES)}")
        return v


class Supersede(BaseModel):
    old_node_id: str
    new_node_id: str
    reason: str | None = None
    evidence_event_labels: list[str] = Field(min_length=1)


class MergeSuggestion(BaseModel):
    node_id_a: str
    node_id_b: str
    reason: str | None = None


class NeedsReview(BaseModel):
    description: str
    related_node_ids: list[str] = Field(default_factory=list)


class ExtractionEnvelope(BaseModel):
    """The full structured output of one extraction call. No free-text
    "summary of this window" field exists on purpose (build plan Milestone 5
    acceptance criterion: "no summary-only text is required for
    persistence") -- `needs_review` is for flagging ambiguity, not for
    narrating what happened."""

    create_nodes: list[CreateNode] = Field(default_factory=list)
    update_nodes: list[UpdateNode] = Field(default_factory=list)
    create_edges: list[CreateEdge] = Field(default_factory=list)
    supersede: list[Supersede] = Field(default_factory=list)
    merge_suggestions: list[MergeSuggestion] = Field(default_factory=list)
    needs_review: list[NeedsReview] = Field(default_factory=list)
