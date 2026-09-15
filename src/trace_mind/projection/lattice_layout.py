"""Deterministic, non-overlapping grid position for every node in a
`tree.TreeResult` -- the layout `lab_notebook_export.py` renders and that
`tests/test_lattice_layout.py` proves correct.

**row** = BFS depth from its tree's root (0 = root row). **col** = position
in a DFS pre-order walk of that same tree (children visited in
`created_at` order, matching every other projection's sibling ordering),
handing out the next unused integer column as each node is first visited.

DFS pre-order visits an entire subtree before moving to the next sibling,
so every subtree occupies a *contiguous* column range -- that single fact
is what makes both guarantees hold:
  - no two nodes share a cell (columns are unique by construction)
  - no connector line can cross a third node's box, given one reserved
    empty gutter row between every node row (see `Position.row` docstring
    and `lab_notebook_export.py`'s connector-drawing code, which only ever
    routes through that gutter space)

Known v1 limitation (see the build plan doc / architecture.md): these
positions are recomputed fresh on every export, not persisted, so a
`trace-mind extract` run that attaches a new edge to an *existing* node
picked from the neighborhood window (not just the newest leaf) can shift
columns for nodes that didn't themselves change. Persisting `(row, col)`
per node is the named Phase 2 fix, not built here.
"""
from __future__ import annotations

from dataclasses import dataclass

from trace_mind.projection.tree import ParentOf, TreeResult, sorted_children

# Which band a root's subtree belongs to -- keeps the unlinked forest's rows
# from ever numerically overlapping the main tree's rows, even though both
# start counting from row 0 within their own band.
MAIN_BAND = "main"
UNLINKED_BAND = "unlinked"


@dataclass(frozen=True)
class Position:
    row: int
    """Depth within its own tree, 0-based. Node rows only -- the renderer
    doubles this to leave a gutter row between every pair of node rows."""
    col: int
    """Position in DFS pre-order, unique across the WHOLE layout (not just
    within one root's tree) -- so two different roots' subtrees never
    reuse the same column and can never collide horizontally either."""
    band: str
    """MAIN_BAND or UNLINKED_BAND."""


def compute_positions(tree: TreeResult) -> dict[str, Position]:
    positions: dict[str, Position] = {}
    next_col = 0

    def place(root_id: str, parent_of: ParentOf, band: str) -> None:
        nonlocal next_col
        # Explicit stack, not recursion: DFS pre-order, children in the
        # same created_at order every other projection uses. Each frame is
        # (node_id, depth); a node gets its column the moment it's popped
        # (i.e. the moment it's "visited"), which is what makes pre-order
        # column assignment give every subtree a contiguous range -- a
        # node's own column is assigned before any of its children's.
        stack: list[tuple[str, int]] = [(root_id, 0)]
        while stack:
            node_id, depth = stack.pop()
            positions[node_id] = Position(row=depth, col=next_col, band=band)
            next_col += 1
            children = sorted_children(tree, parent_of, node_id)
            # Push in reverse so the stack (LIFO) still pops children in
            # created_at order, matching every other projection's ordering.
            for child_id in reversed(children):
                stack.append((child_id, depth + 1))

    for root_id, parent_of in tree.forest:
        place(root_id, parent_of, MAIN_BAND)
    for root_id, parent_of in tree.unlinked_forest:
        place(root_id, parent_of, UNLINKED_BAND)

    return positions


# Node rows live at even grid-row indices (0, 2, 4, ...); the odd indices
# between them (1, 3, 5, ...) are reserved, empty gutter rows that every
# connector's horizontal jog must stay within -- this constant is what
# `grid_row`/`connector_waypoints` and the renderer's pixel math agree on.
GRID_ROW_STRIDE = 2


def grid_row(row: int) -> int:
    """A node's own logical `row` (0-based tree depth) converted to a
    grid-row index that leaves a gutter row before every node row after
    the first."""
    return row * GRID_ROW_STRIDE


def connector_waypoints(parent: Position, child: Position) -> list[tuple[int, int]]:
    """The 3-segment elbow from `parent` down into `child`, as
    `(grid_row, col)` waypoints: parent's bottom edge -> straight down into
    the gutter row directly below it -> horizontal within that gutter row
    to the child's column -> straight down into the child's top edge.
    Every waypoint pair differs in exactly one of (row, col), so each
    segment is purely vertical or purely horizontal -- there's no diagonal
    to reason about when checking for node-row overlap."""
    gutter = grid_row(parent.row) + 1
    return [
        (grid_row(parent.row), parent.col),
        (gutter, parent.col),
        (gutter, child.col),
        (grid_row(child.row), child.col),
    ]
