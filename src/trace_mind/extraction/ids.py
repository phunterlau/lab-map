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
        row = conn.execute(
            "SELECT next_seq FROM graph_id_counters WHERE project_id = ? AND prefix = ?",
            (project_id, prefix),
        ).fetchone()
        seq = row["next_seq"] if row else 1
        conn.execute(
            """
            INSERT INTO graph_id_counters (project_id, prefix, next_seq) VALUES (?, ?, ?)
            ON CONFLICT(project_id, prefix) DO UPDATE SET next_seq = excluded.next_seq
            """,
            (project_id, prefix, seq + 1),
        )
    return f"{prefix}-{seq:04d}"
