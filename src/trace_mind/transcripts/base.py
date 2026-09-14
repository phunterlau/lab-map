"""Adapter contract. See build plan section 10 -- this boundary is load-bearing:
a provider format change should require touching exactly one adapter."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from trace_mind.normalize.models import TranscriptDelta


class TranscriptAdapter(Protocol):
    provider: str

    def detect(self, path: Path) -> bool:
        """Return True if this adapter can parse the file at `path`."""
        ...

    def read_delta(self, path: Path, session_id: str, byte_offset: int) -> TranscriptDelta:
        """Read and normalize only the bytes at-or-after `byte_offset`.

        Must leave any incomplete trailing record unconsumed (reflected in
        `next_byte_offset`) rather than raising or dropping it.
        """
        ...
