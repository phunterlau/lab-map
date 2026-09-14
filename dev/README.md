# dev/

Developer-only scripts. Not part of the installed `trace-mind` package
(nothing here is imported by `src/trace_mind/`), not installed by
`uv tool install .`, and not shipped to users -- this is where one-off or
recurring maintenance tooling lives instead of scratch space that gets
thrown away between sessions.

- `render_demo_graph.py` -- regenerates `assets/demo-graph.png` from the
  synthetic test fixtures (the build plan's own canonical scenario, section
  27). Safe to run and commit output from: fixture content is fictional,
  never real transcript data. Run with `uv run dev/render_demo_graph.py`
  from the repo root.
