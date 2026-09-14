"""The extraction prompt: rules (input D), not summarization (build plan
section 14). The taxonomy below mirrors docs/extraction_design.md -- keep
the two in sync if either changes; it's duplicated deliberately rather than
loaded from the doc at runtime, so prompt behavior doesn't shift silently
when someone edits prose in the doc.
"""
from __future__ import annotations

from trace_mind.extraction.window import ExtractionWindow
from trace_mind.graph.models import EDGE_TYPES, NODE_STATUSES, NODE_TYPES

SYSTEM_PROMPT = """You are a graph-diff extractor for a coding-agent research session. \
You are given recent conversation turns (labeled e1, e2, ...) and the existing \
nearby decision graph. Emit ONLY justified graph mutations as JSON matching the \
given schema -- never a prose summary, never speculation without a cited turn.

Ontology (do not invent types/statuses outside these):
  node types: {node_types}
  node statuses: {node_statuses}
  edge types: {edge_types}

Recognize these seven conversational "moment" types. Getting the first \
distinction wrong is the most damaging mistake:

1. OFFER_AND_PICK (true alternatives): the agent proposes mutually exclusive \
approaches; the user picks one. Non-picked options become `option` nodes with \
status "dormant" (not "rejected" -- nothing was argued against them).

2. SEQUENCE_PICK (ordered work, NOT a fork): the agent lists several \
sequential milestones/phases; the user starts with #1. This is NOT a choice \
among alternatives -- #2 and #3 are still planned, not rejected. Model them as \
`option` nodes with status "open" or "candidate". Never assert a CHOSEN_OVER \
edge for a sequence pick -- that would misrepresent "we'll get to it" as "we \
ruled it out". Discriminator: ordering language ("phase", "milestone", \
"first... then...") and the un-picked items are framed as still-planned, vs. \
either/or language ("instead of", "rather than") where un-picked items don't \
come back up.

3. DEEP_DIVE (elaboration, not a branch): the user asks for more detail or runs \
more experiments on an option/decision that is ALREADY in the graph neighborhood \
you were given. Do not create a new node for this -- update the existing one \
(`update_nodes`) or attach new evidence to it. Discriminator: does this turn name \
an alternative that is NOT already in the neighborhood you were given? If no, \
it's a deep dive.

4. NEW_GOAL: the user states an objective unrelated to the current active \
thread. Create a new `goal` node, not nested under whatever was active.

5. REJECTION (requires a stated reason): something previously exploring/chosen \
is explicitly ruled out, with a reason given. Do not infer rejection from an \
option simply not being mentioned again -- that silence is far more often a \
SEQUENCE_PICK than a rejection.

6. REVISIT / PARK (explicit deferral): something is explicitly set aside with a \
stated condition or reason, without being rejected outright. Create a \
`revisit_condition` node with a REVISIT_WHEN edge to the thing being deferred. \
This can attach to part of an option's scope, not just the whole node.

7. EXPLICIT_PARALLEL_BRANCH: the user (not the agent) explicitly asks to \
explore a new direction as its own branch, with its own plan, while keeping the \
current line active. Treat explicit branch/fork language from the user as a \
strong signal.

Every node and edge you emit MUST cite at least one evidence label from the \
window you were given (`evidence_event_labels`). Never invent a label. If you \
are not confident enough to create or update something, put it in \
`needs_review` instead of guessing.
"""

RESPONSE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "create_nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "temp_id": {"type": "string"},
                    "type": {"type": "string"},
                    "title": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_event_labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["temp_id", "type", "title", "evidence_event_labels"],
            },
        },
        "update_nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "string"},
                    "status": {"type": "string"},
                    "summary": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_event_labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["node_id", "evidence_event_labels"],
            },
        },
        "create_edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "target": {"type": "string"},
                    "type": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                    "evidence_event_labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["source", "target", "type", "evidence_event_labels"],
            },
        },
        "supersede": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "old_node_id": {"type": "string"},
                    "new_node_id": {"type": "string"},
                    "reason": {"type": "string"},
                    "evidence_event_labels": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["old_node_id", "new_node_id", "evidence_event_labels"],
            },
        },
        "merge_suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "node_id_a": {"type": "string"},
                    "node_id_b": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["node_id_a", "node_id_b"],
            },
        },
        "needs_review": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "related_node_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["description"],
            },
        },
    },
    "required": [
        "create_nodes", "update_nodes", "create_edges",
        "supersede", "merge_suggestions", "needs_review",
    ],
}


def system_prompt() -> str:
    return SYSTEM_PROMPT.format(
        node_types=", ".join(sorted(NODE_TYPES)),
        node_statuses=", ".join(sorted(NODE_STATUSES)),
        edge_types=", ".join(sorted(EDGE_TYPES)),
    )


def user_prompt(window: ExtractionWindow) -> str:
    return (
        "Existing nearby graph state:\n"
        f"{window.render_neighborhood_block()}\n\n"
        "New transcript turns to consider:\n"
        f"{window.render_events_block()}\n"
    )
