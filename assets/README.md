# assets/

Generated images/diagrams worth committing and linking from docs --
distinct from `.trace-mind/` (a *tracked project's* per-project generated
state, which is never committed here) and from `reference/` (cloned repos,
gitignored).

Everything in this directory must be regeneratable from a script in `dev/`
and must never be built from real transcript content -- see
[[trace_mind_provenance_guard]] in memory, or just: synthetic fixtures in,
synthetic images out.

- `demo-graph.png` -- example decision graph rendered from
  `tests/fixtures/`, via `dev/render_demo_graph.py`.
