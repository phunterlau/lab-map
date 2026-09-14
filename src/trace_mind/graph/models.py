"""Typed graph ontology (build plan section 8). MVP subset only -- the
extractor (Milestone 5) may create the richer types later when confidence
is high, but manual/CLI construction should stick to what's load-bearing
now."""
from __future__ import annotations

from typing import Literal

NodeType = Literal[
    "goal",
    "hypothesis",
    "option",
    "evidence",
    "experiment",
    "decision",
    "outcome",
    "revisit_condition",
]

NodeStatus = Literal[
    "open",
    "exploring",
    "candidate",
    "chosen",
    "rejected",
    "dormant",
    "blocked",
    "superseded",
    "completed",
]

EdgeType = Literal[
    "EXPLORES",
    "ALTERNATIVE_TO",
    "SUPPORTS",
    "CONTRADICTS",
    "TESTS",
    "CHOSEN_OVER",
    "REJECTED_BECAUSE",
    "SUPERSEDES",
    "IMPLEMENTS",
    "PRODUCED",
    "REVISIT_WHEN",
    "DERIVED_FROM",
    "RELATED_TO",
]
