"""Self-contained interactive HTML export of the graph.

One file, no server, no CDN, no bundler -- open it directly with a
file:// URL. The SVG comes from Graphviz (graphviz_export.render_svg);
node details (summary/status/edges/provenance) come from
`projection.tree.build_node_snapshot` (the same JSON shape `trace-mind
graph export --json` writes standalone) and are embedded as a JSON blob,
rendered into a side panel by a small vanilla-JS block on click.
"""
from __future__ import annotations

import json
import sqlite3

from trace_mind.projection.graphviz_export import _STATUS_COLOR, render_svg
from trace_mind.projection.tree import build_node_snapshot


def render_html(conn: sqlite3.Connection, project_id: str) -> str:
    svg = render_svg(conn, project_id)
    svg_inline = svg[svg.index("<svg") :]  # drop the XML/DOCTYPE preamble

    node_data = build_node_snapshot(conn, project_id)

    html = _TEMPLATE
    html = html.replace("__SVG__", svg_inline)
    html = html.replace("__DATA_JSON__", json.dumps(node_data))
    html = html.replace("__STATUS_COLOR_JSON__", json.dumps(_STATUS_COLOR))
    return html


_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>trace-mind graph</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system, Helvetica, Arial, sans-serif; }
  #app { display: flex; height: 100vh; }
  #viewport { flex: 1; overflow: hidden; position: relative; background: #fafafa; cursor: grab; }
  #viewport.dragging { cursor: grabbing; }
  #canvas { transform-origin: 0 0; width: fit-content; }
  #panel { width: 360px; flex: none; border-left: 1px solid #ddd; padding: 16px; overflow-y: auto; box-sizing: border-box; }
  #panel h2 { margin-top: 0; font-size: 16px; }
  #panel h3 { font-size: 13px; color: #555; margin-bottom: 4px; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 4px; color: white; font-size: 12px; }
  .edge-row, .prov-row { margin-bottom: 8px; padding-bottom: 8px; border-bottom: 1px solid #eee; font-size: 13px; }
  .muted { color: #888; font-size: 12px; }
  .hint { color: #888; padding: 16px; font-size: 13px; }
  g.node { cursor: pointer; }
</style>
</head>
<body>
<div id="app">
  <div id="viewport">
    <div id="canvas">
__SVG__
    </div>
  </div>
  <div id="panel"><div class="hint">Click a node to see its details. Scroll to zoom, drag to pan.</div></div>
</div>
<script>
const NODE_DATA = __DATA_JSON__;
const STATUS_COLOR = __STATUS_COLOR_JSON__;

const viewport = document.getElementById('viewport');
const canvas = document.getElementById('canvas');
let scale = 1, tx = 0, ty = 0, dragging = false, lastX = 0, lastY = 0;

function applyTransform() {
  canvas.style.transform = 'translate(' + tx + 'px, ' + ty + 'px) scale(' + scale + ')';
}

viewport.addEventListener('wheel', function (e) {
  e.preventDefault();
  const delta = -e.deltaY * 0.001;
  const newScale = Math.min(4, Math.max(0.1, scale * (1 + delta)));
  const rect = viewport.getBoundingClientRect();
  const cx = e.clientX - rect.left;
  const cy = e.clientY - rect.top;
  tx = cx - (cx - tx) * (newScale / scale);
  ty = cy - (cy - ty) * (newScale / scale);
  scale = newScale;
  applyTransform();
}, { passive: false });

viewport.addEventListener('mousedown', function (e) {
  dragging = true;
  lastX = e.clientX;
  lastY = e.clientY;
  viewport.classList.add('dragging');
});
window.addEventListener('mousemove', function (e) {
  if (!dragging) return;
  tx += e.clientX - lastX;
  ty += e.clientY - lastY;
  lastX = e.clientX;
  lastY = e.clientY;
  applyTransform();
});
window.addEventListener('mouseup', function () {
  dragging = false;
  viewport.classList.remove('dragging');
});

applyTransform();

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function renderPanel(nodeId) {
  const d = NODE_DATA[nodeId];
  const panel = document.getElementById('panel');
  if (!d) { panel.innerHTML = '<div class="hint">No data for ' + escapeHtml(nodeId) + '</div>'; return; }
  const color = STATUS_COLOR[d.status] || '#888';
  let html = '<h2>' + escapeHtml(nodeId) + '</h2>';
  html += '<div><span class="badge" style="background:' + color + '">' + escapeHtml(d.status) + '</span> ';
  html += '<span class="muted">' + escapeHtml(d.type) + (d.confidence != null ? ' &middot; confidence ' + d.confidence : '') + '</span></div>';
  html += '<p>' + escapeHtml(d.title) + '</p>';
  if (d.summary) html += '<p class="muted">' + escapeHtml(d.summary) + '</p>';
  if (d.edges.length) {
    html += '<h3>Edges</h3>';
    d.edges.forEach(function (e) {
      html += '<div class="edge-row">' + e.direction + ' <b>' + escapeHtml(e.type) + '</b> ' + escapeHtml(e.other);
      if (e.reason) html += '<br><span class="muted">' + escapeHtml(e.reason) + '</span>';
      html += '</div>';
    });
  }
  if (d.provenance.length) {
    html += '<h3>Provenance</h3>';
    d.provenance.forEach(function (p) {
      html += '<div class="prov-row"><span class="muted">session ' + escapeHtml(p.session_id) + ' turn ' + escapeHtml(p.turn_id);
      html += '<br>bytes [' + p.byte_start + ':' + p.byte_end + ']</span><br>' + escapeHtml(p.excerpt) + '</div>';
    });
  }
  panel.innerHTML = html;
}

canvas.addEventListener('click', function (e) {
  const g = e.target.closest('g.node');
  if (!g) return;
  const title = g.querySelector('title');
  if (!title) return;
  renderPanel(title.textContent);
});
</script>
</body>
</html>
"""
