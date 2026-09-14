"""Render the graph_nodes/graph_edges tables as Graphviz DOT.

Not wired into pyproject dependencies deliberately: this shells out to the
system `dot` binary rather than pulling in a Python plotting stack
(matplotlib/networkx), keeping with the build plan's "keep the dependency
set small" rule (section 6) -- that rule is about Python packages, and
DOT/PNG rendering is naturally a system tool's job.
"""
from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

_STATUS_COLOR = {
    "chosen": "#2e7d32",
    "rejected": "#c62828",
    "dormant": "#9e9e9e",
    "completed": "#1565c0",
    "exploring": "#f9a825",
    "open": "#f9a825",
    "candidate": "#6a1b9a",
    "blocked": "#c62828",
    "superseded": "#9e9e9e",
}

_TYPE_SHAPE = {
    "goal": "doubleoctagon",
    "decision": "diamond",
    "option": "box",
    "evidence": "note",
    "experiment": "component",
    "outcome": "ellipse",
    "action": "cds",
    "revisit_condition": "hexagon",
    "hypothesis": "box",
}


def render_dot(conn: sqlite3.Connection, project_id: str) -> str:
    nodes = conn.execute(
        "SELECT id, type, title, status FROM graph_nodes WHERE project_id = ? ORDER BY created_at",
        (project_id,),
    ).fetchall()
    edges = conn.execute(
        "SELECT source_node_id, target_node_id, type, reason FROM graph_edges WHERE project_id = ?",
        (project_id,),
    ).fetchall()

    lines = ["digraph research_map {", '  rankdir="TB";', '  node [style=filled, fontname="Helvetica"];', '  edge [fontname="Helvetica", fontsize=10];']

    for n in nodes:
        color = _STATUS_COLOR.get(n["status"], "#e0e0e0")
        shape = _TYPE_SHAPE.get(n["type"], "box")
        label = _escape(f"{n['id']}\\n{n['title']}\\n[{n['type']} / {n['status']}]")
        lines.append(f'  "{n["id"]}" [label="{label}", shape={shape}, fillcolor="{color}", fontcolor="white"];')

    for e in edges:
        label = _escape(e["type"])
        lines.append(f'  "{e["source_node_id"]}" -> "{e["target_node_id"]}" [label="{label}"];')

    lines.append("}")
    return "\n".join(lines)


def render_png(conn: sqlite3.Connection, project_id: str, out_path: Path) -> None:
    dot_source = render_dot(conn, project_id)
    result = subprocess.run(
        ["dot", "-Tpng", "-o", str(out_path)],
        input=dot_source.encode("utf-8"),
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dot failed: {result.stderr.decode('utf-8', errors='replace')}")


def _escape(text: str) -> str:
    return text.replace('"', '\\"')
