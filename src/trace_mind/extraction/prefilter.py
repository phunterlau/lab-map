"""Cheap deterministic filter before any LLM call (build plan section 13).

"Many turns will not change research state" -- V1 filtering is deterministic
first: minimum meaningful text length, ignore tool-plumbing-only turns,
trigger strongly on research vocabulary and choice structures, trigger on
explicit direction changes. No classifier stage (the plan's optional
second tier) is implemented -- deterministic rules alone are what the real
cross-check (docs/extraction_design.md) validated by hand, so that's what
ships first.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

MIN_TEXT_LENGTH = 40
"""Below this, nothing worth extracting has been said ("yes", "run the
tests", "fix lint" -- the build plan's own examples of noise)."""

TEXT_EVENT_TYPES = {"user_message", "assistant_message"}

# Each pattern is tagged with the moment-taxonomy signal it's evidence for
# (docs/extraction_design.md) purely for diagnostics -- the filter itself
# only cares whether *any* pattern hit.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("numbered_or_bulleted_list", re.compile(r"(?m)^\s*(?:[0-9]+[.)]|[-*])\s+\S")),
    ("alternative_language", re.compile(r"\b(option|alternative|either|instead of|rather than|vs\.?)\b", re.I)),
    ("sequence_language", re.compile(r"\b(milestone|phase|first,? then|next phase|roadmap)\b", re.I)),
    ("rejection_language", re.compile(r"\b(dead end|reject(?:ed|ing)?|doesn'?t work|abandon(?:ed|ing)?|ruled out)\b", re.I)),
    ("revisit_language", re.compile(r"\b(park(?:ed|ing)?|revisit|defer(?:red)?|table this|come back to)\b", re.I)),
    ("branch_language", re.compile(r"\b(as a branch|branch from|in parallel|separate (?:branch|track))\b", re.I)),
    ("decision_language", re.compile(r"\b(decide[ds]?|decision|we'?ll go with|going with|chosen)\b", re.I)),
    ("evidence_language", re.compile(r"\b(result|found that|measured|evidence|auc|z-score|threshold|showed that)\b", re.I)),
    ("new_direction_language", re.compile(r"\b(new goal|let'?s (?:start|switch)|different direction|change direction)\b", re.I)),
]


@dataclass
class PrefilterResult:
    worth_extracting: bool
    signals: list[str] = field(default_factory=list)
    combined_text: str = ""


def visible_text(events: list) -> str:
    """Join only the text a human/LLM would actually read -- tool plumbing
    and system noise are excluded (build plan section 13's examples)."""
    parts = []
    for e in events:
        event_type = e["event_type"] if hasattr(e, "keys") else e.get("event_type")
        text = e["text"] if hasattr(e, "keys") else e.get("text")
        if event_type in TEXT_EVENT_TYPES and text:
            parts.append(text)
    return "\n".join(parts)


def evaluate(events: list) -> PrefilterResult:
    combined = visible_text(events)
    if len(combined.strip()) < MIN_TEXT_LENGTH:
        return PrefilterResult(worth_extracting=False, combined_text=combined)

    signals = [name for name, pattern in _PATTERNS if pattern.search(combined)]
    return PrefilterResult(worth_extracting=bool(signals), signals=signals, combined_text=combined)
