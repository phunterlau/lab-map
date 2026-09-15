"""Real node-id allocation for LLM-created nodes.

The extractor (build plan section 14) emits `temp_id: "n1"` -- it never
invents the real id. Something has to mint one before `create_edges` can
resolve a reference to a brand-new node, and it has to be durable (the next
extraction run, possibly minutes or days later, must not reuse a number).
Backed by `graph_id_counters` (storage/db.py).
"""
from __future__ import annotations

import sqlite3

TYPE_PREFIX = {
    "goal": "Q",
    "hypothesis": "H",
    "option": "O",
    "evidence": "E",
    "experiment": "X",
    "decision": "D",
    "action": "A",
    "outcome": "EV",
    "revisit_condition": "R",
}


def allocate_node_id(conn: sqlite3.Connection, project_id: str, type_: str) -> str:
    prefix = TYPE_PREFIX.get(type_)
    if prefix is None:
        raise ValueError(f"no id prefix configured for node type {type_!r}")

    with conn:
        # GLOBAL, not per-project: graph_nodes.id is a single global primary
        # key (storage/db.py), not scoped by project_id -- so if two
        # different projects' extraction runs each started counting from 1,
        # they'd independently mint the same id (e.g. both "A-0001"), and
        # add_node's upsert would silently let the second overwrite the
        # first's content across an unrelated project. Confirmed via a real
        # headless test: 7 different projects each minted "A-0001"
        # independently, destroying 6 of 7 node-creation events with no
        # error. Taking the max `next_seq` across every project's row for
        # this prefix (rather than just this project's own row) guarantees
        # every future mint, from any project, is globally unique -- the
        # per-project row is still written so a project's own future mints
        # keep advancing from wherever the global max last was.
        row = conn.execute(
            "SELECT MAX(next_seq) AS next_seq FROM graph_id_counters WHERE prefix = ?",
            (prefix,),
        ).fetchone()
        seq = row["next_seq"] if row and row["next_seq"] is not None else 1
        # The counter table only knows about ids IT allocated. A
        # hand-authored id (`trace-mind node add A-0001`, or a build.py-style
        # direct add_node call) never touches graph_id_counters, so the
        # counter alone can still collide with a real, already-existing row.
        # Bump past any id that's actually present in graph_nodes too.
        while conn.execute(
            "SELECT 1 FROM graph_nodes WHERE id = ?", (f"{prefix}-{seq:04d}",)
        ).fetchone() is not None:
            seq += 1
        conn.execute(
            """
            INSERT INTO graph_id_counters (project_id, prefix, next_seq) VALUES (?, ?, ?)
            ON CONFLICT(project_id, prefix) DO UPDATE SET next_seq = excluded.next_seq
            """,
            (project_id, prefix, seq + 1),
        )
    return f"{prefix}-{seq:04d}"
