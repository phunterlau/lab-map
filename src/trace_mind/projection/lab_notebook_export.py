"""Self-contained, read-only "lab notebook" view of the graph.

Boxes sit at fixed positions from `projection.lattice_layout` (DFS
pre-order columns, BFS-depth rows), connected by elbow lines routed
entirely through reserved gutter rows -- see `lattice_layout.py`'s module
docstring for why that makes node/line overlap structurally impossible,
not just visually rare. One file, no server, no CDN, no JS library -- same
"open it directly with file://" pattern `html_export.py` already
establishes, just a different layout: fixed, stable-under-append positions
instead of Graphviz's reflowing auto-layout, for when not having boxes
jump between exports matters more than a maximally compact drawing.

Known v1 limitation (see lattice_layout.py): positions are recomputed
fresh on every export, not persisted, so a normal `trace-mind extract` run
that attaches a new edge to an *existing* node can shift columns for nodes
that didn't themselves change. Persisting `(row, col)` per node is the
named Phase 2 fix (docs/architecture.md), not built here.
"""
from __future__ import annotations

import html as html_escape
import sqlite3

from trace_mind.projection.graphviz_export import _STATUS_COLOR
from trace_mind.projection.lattice_layout import Position, compute_positions, connector_waypoints, grid_row
from trace_mind.projection.tree import build_forest, build_node_snapshot, collect_findings, format_relative_age

BOX_WIDTH = 220
BOX_HEIGHT = 100
COL_GUTTER = 40
GUTTER_ROW_HEIGHT = 60
PADDING = 48
COL_STRIDE = BOX_WIDTH + COL_GUTTER

_DEFAULT_STATUS_COLOR = "#9e9e9e"


def render_lab_notebook(conn: sqlite3.Connection, project_id: str) -> str:
    tree = build_forest(conn, project_id)
    if not tree.nodes:
        return _EMPTY_TEMPLATE

    positions = compute_positions(tree)
    snapshot = build_node_snapshot(conn, project_id)
    findings = collect_findings(conn, project_id, tree.by_id, tree.node_timestamp)

    max_col = max(p.col for p in positions.values())
    max_grid_row = max(grid_row(p.row) for p in positions.values())
    canvas_width = (max_col + 1) * COL_STRIDE + PADDING * 2 - COL_GUTTER
    canvas_height = _grid_row_y(max_grid_row) + BOX_HEIGHT + PADDING * 2

    boxes = [
        _render_box(
            tree.by_id[node_id], pos, snapshot.get(node_id, {}),
            age=format_relative_age(tree.node_timestamp.get(node_id)),
            is_here=(node_id == tree.you_are_here),
        )
        for node_id, pos in positions.items()
    ]

    connectors = []
    for _root_id, parent_of in (*tree.forest, *tree.unlinked_forest):
        for child_id, (parent_id, _edge) in parent_of.items():
            connectors.append(_render_connector(positions[parent_id], positions[child_id]))

    html_out = _TEMPLATE
    html_out = html_out.replace("__CANVAS_WIDTH__", str(canvas_width))
    html_out = html_out.replace("__CANVAS_HEIGHT__", str(canvas_height))
    html_out = html_out.replace("__CONNECTORS__", "\n".join(connectors))
    html_out = html_out.replace("__BOXES__", "\n".join(boxes))
    html_out = html_out.replace("__OPEN_LOOPS__", _render_open_loops_panel(findings))
    return html_out


def _grid_row_y(target_grid_row: int) -> int:
    """Cumulative pixel offset of the TOP of `target_grid_row`: node rows
    (even grid-row index) are BOX_HEIGHT tall, gutter rows (odd) are
    GUTTER_ROW_HEIGHT tall."""
    y = 0
    for i in range(target_grid_row):
        y += BOX_HEIGHT if i % 2 == 0 else GUTTER_ROW_HEIGHT
    return y


def _box_xy(pos: Position) -> tuple[int, int]:
    x = PADDING + pos.col * COL_STRIDE
    y = PADDING + _grid_row_y(grid_row(pos.row))
    return x, y


def _render_box(node: sqlite3.Row, pos: Position, snapshot_entry: dict, *, age: str | None, is_here: bool) -> str:
    """`snapshot_entry` (from `tree.build_node_snapshot`) is what makes this
    view carry "slightly more context than the ascii version": a hover
    tooltip with the node's summary and its first evidence excerpt, neither
    of which fit in a 220x100 card -- read-only, so a native `title`
    attribute is enough, no JS needed. `age` (tree.format_relative_age) is
    folded into the same meta line as type/status rather than a new div, to
    stay inside the fixed card height."""
    x, y = _box_xy(pos)
    color = _STATUS_COLOR.get(node["status"], _DEFAULT_STATUS_COLOR)
    here_class = " here" if is_here else ""
    here_tag = '<div class="here-tag">YOU ARE HERE</div>' if is_here else ""

    tooltip_parts = []
    if snapshot_entry.get("summary"):
        tooltip_parts.append(snapshot_entry["summary"])
    provenance = snapshot_entry.get("provenance") or []
    if provenance:
        tooltip_parts.append(f"evidence: {provenance[0]['excerpt']}")
    tooltip = html_escape.escape(" — ".join(tooltip_parts)) if tooltip_parts else ""
    title_attr = f' title="{tooltip}"' if tooltip else ""

    meta = f'{html_escape.escape(node["type"])} / {html_escape.escape(node["status"])}'
    if age:
        meta = f"{meta} · {html_escape.escape(age)}"

    return (
        f'<div class="card{here_class}" style="left:{x}px; top:{y}px; '
        f'width:{BOX_WIDTH}px; height:{BOX_HEIGHT}px; border-left-color:{color};" '
        f'data-id="{html_escape.escape(node["id"])}"{title_attr}>'
        f"{here_tag}"
        f'<div class="card-id">{html_escape.escape(node["id"])}</div>'
        f'<div class="card-title">{html_escape.escape(node["title"])}</div>'
        f'<div class="card-meta">{meta}</div>'
        f"</div>"
    )


def _render_connector(parent_pos: Position, child_pos: Position) -> str:
    waypoints = connector_waypoints(parent_pos, child_pos)
    # Fixed 4-waypoint shape from connector_waypoints: [parent bottom edge,
    # gutter (under parent's col), gutter (under child's col), child top
    # edge] -- indices 0 and 3 sit ON a node row (use that box's bottom/top
    # edge respectively), indices 1 and 2 sit in the gutter row between them
    # (use its vertical center, for a clean elbow bend).
    px, py = _box_xy(parent_pos)
    cx, cy = _box_xy(child_pos)
    gutter_y = PADDING + _grid_row_y(waypoints[1][0]) + GUTTER_ROW_HEIGHT / 2

    p0 = (px + BOX_WIDTH / 2, py + BOX_HEIGHT)  # parent bottom-center
    p1 = (px + BOX_WIDTH / 2, gutter_y)
    p2 = (cx + BOX_WIDTH / 2, gutter_y)
    p3 = (cx + BOX_WIDTH / 2, cy)  # child top-center

    d = f"M {p0[0]},{p0[1]} L {p1[0]},{p1[1]} L {p2[0]},{p2[1]} L {p3[0]},{p3[1]}"
    return f'<path d="{d}" class="connector" marker-end="url(#arrow)" />'


def _render_open_loops_panel(findings: list) -> str:
    """Generic over `Finding.kind` -- a new check in tree.py shows up here
    with zero changes needed in this function, same as ascii_export's
    equivalent listing."""
    if not findings:
        return ""

    tag_label = {
        "revisit_condition": "revisit",
        "dormant_unresolved": "dormant",
        "experiment_no_outcome": "no outcome",
        "unresolved_sibling": "unresolved",
        "contradicted_but_chosen": "contradicted",
        "needs_review": "needs review",
        "merge_suggestion": "merge?",
    }
    items = [
        f'<li><span class="loop-tag">{html_escape.escape(tag_label.get(f.kind, f.kind))}</span> '
        f"{html_escape.escape(f.text)}</li>"
        for f in findings
    ]
    return f'<div id="open-loops"><h2>Open loops</h2><ul>{"".join(items)}</ul></div>'


_EMPTY_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>trace-mind lab notebook</title></head>
<body style="font-family: -apple-system, Helvetica, Arial, sans-serif; padding: 40px; color: #555;">
(no nodes in this project's graph yet)
</body></html>
"""

_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>trace-mind lab notebook</title>
<style>
  html, body {
    margin: 0; padding: 0;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
    background: #faf6ec;
    background-image: repeating-linear-gradient(
      to bottom, transparent, transparent 39px, #eee0c4 40px
    );
  }
  #wrap { display: flex; align-items: flex-start; }
  #canvas-scroll { flex: 1; overflow: auto; padding: 16px; }
  #canvas { position: relative; }
  .connector {
    fill: none;
    stroke: #8a7f66;
    stroke-width: 1.75;
  }
  .card {
    position: absolute;
    box-sizing: border-box;
    background: #fffdf7;
    border: 1px solid #e4d9bd;
    border-left: 5px solid #9e9e9e;
    border-radius: 8px;
    box-shadow: 1px 2px 4px rgba(80, 60, 20, 0.12);
    padding: 8px 10px;
    overflow: hidden;
  }
  .card.here {
    box-shadow: 0 0 0 3px #f2b705, 1px 2px 4px rgba(80, 60, 20, 0.12);
  }
  .here-tag {
    position: absolute; top: -11px; right: 6px;
    background: #f2b705; color: #3a2e00;
    font-size: 10px; font-weight: 600;
    padding: 1px 6px; border-radius: 4px;
  }
  .card-id {
    font-size: 11px; font-weight: 700; color: #7a6a3f;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .card-title {
    font-size: 13px; color: #2a2a2a; margin-top: 2px;
    display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .card-meta {
    position: absolute; bottom: 6px; left: 10px;
    font-size: 10px; color: #999;
  }
  #open-loops {
    width: 280px; flex: none;
    margin: 16px 16px 16px 0;
    background: #fff8e1;
    border: 1px solid #e8d9a0;
    border-radius: 8px;
    padding: 12px 16px;
  }
  #open-loops h2 { font-size: 14px; margin: 0 0 8px 0; }
  #open-loops ul { padding-left: 18px; margin: 0; font-size: 12px; line-height: 1.6; }
  .loop-tag {
    display: inline-block; font-size: 9px; font-weight: 700; text-transform: uppercase;
    background: #e8d9a0; color: #5a4a10; border-radius: 3px; padding: 1px 4px; margin-right: 4px;
  }
  .loop-related { color: #888; }
  .hint { color: #998; font-size: 12px; padding: 8px 16px; }
</style>
</head>
<body>
<div class="hint">Read-only. Regenerate this file (`trace-mind graph export --lab-notebook`) to refresh after new sessions.</div>
<div id="wrap">
  <div id="canvas-scroll">
    <div id="canvas" style="width: __CANVAS_WIDTH__px; height: __CANVAS_HEIGHT__px;">
      <svg width="__CANVAS_WIDTH__" height="__CANVAS_HEIGHT__" style="position:absolute; top:0; left:0;">
        <defs>
          <marker id="arrow" markerWidth="8" markerHeight="8" refX="4" refY="4" orient="auto">
            <circle cx="4" cy="4" r="2.5" fill="#8a7f66" />
          </marker>
        </defs>
        __CONNECTORS__
      </svg>
      __BOXES__
    </div>
  </div>
  __OPEN_LOOPS__
</div>
</body>
</html>
"""
