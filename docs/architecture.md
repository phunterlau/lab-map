# trace-mind: current state

Full design: `../research_decision_map_build_plan.md` (working name in that
doc is "research-map"; this repo is `trace-mind` / `trace_mind`). Reference
repos studied for architecture (not vendored): `../reference/` (gitignored;
see `REFERENCES.lock`/`REFERENCES.urls` for exact commits).

Central invariant, unchanged from the build plan:

```
hooks are the control plane
transcripts are the evidence/data plane
the graph is interpreted state
Markdown is the human-readable projection
```

## What's built (build plan Milestones 0-5 + Codex half of Milestone 2)

Real transcript file -> adapter -> `NormalizedEvent` -> SQLite -> cursor
advance -> re-run is idempotent -> manual graph (nodes/edges/provenance) ->
`why <node>` walks decision -> evidence -> source session/turn/bytes.
Codex hooks call the same `ingest_session()` automatically now; Claude Code
hooks are not wired yet ("hooks to codex for now").

- `normalize/models.py` -- `NormalizedEvent`, provider-neutral. Nothing
  outside `transcripts/` touches raw Claude/Codex JSON.
- `transcripts/claude.py`, `transcripts/codex.py` -- adapters, written
  against real local schema (JSONL field shapes learned by reading actual
  `~/.claude/projects/*/*.jsonl` and `~/.codex/sessions/**/*.jsonl` files on
  this machine -- not from docs, not copied into this repo).
- `storage/db.py` -- SQLite schema. One deliberate deviation from the build
  plan's draft schema (section 9): `normalized_events` dedups on
  `(transcript_path, byte_start, byte_end, event_index)`, not
  `(session_id, content_hash)`. `transcript_cursors` is keyed on
  `(session_id, transcript_path)`, not `session_id` alone. The reason both
  are keyed on the *physical file*, not the logical session, is a real bug
  found against real data (below) -- one Codex `session_id` can span
  multiple rollout files (a compaction rewrites the rollout under a new
  filename but keeps the same `session_id`), and comparing/deduping across
  two different files under one session_id is meaningless.
- `normalize/pipeline.py` -- `ingest_session()`. Event insertion and cursor
  advancement happen in one SQLite transaction: a crash between them can't
  happen, so a re-run after a crash re-reads from the last *committed*
  offset and re-derives exactly the same events (verified in
  `tests/test_idempotence.py`). Detects truncation/rotation by comparing a
  file against *its own* prior size/leading-bytes hash (never against a
  different file) and resets only that file's cursor + rows.
- `graph/models.py`, `graph/repository.py` -- MVP node/edge ontology (build
  plan section 8) with enforced constraints: `add_node`/`add_edge` reject
  unknown types/statuses (`InvalidOntologyError`), require at least one
  real `evidence_event_id` pointing at an already-ingested
  `normalized_events` row (`MissingProvenanceError`, override with
  `allow_no_evidence` only for structural nodes), and reject edges to
  nonexistent nodes (`UnknownNodeError`). Upserts by node/edge id.
- `cli.py` -- `trace-mind ingest`, `node add/show`, `edge add`, `why`
  (alias for `node show`: the decision -> evidence -> source-session
  chain), `graph export [--png]`, `hook --provider codex`. Backfill will
  call the same `ingest_session()`/`graph.repository` functions the CLI
  calls.
- `hooks/common.py`, `hooks/codex.py` -- the Codex half of Milestone 2.
  `trace-mind hook --provider codex` reads one hook event as JSON from
  stdin, logs it to `hook_events`, and opportunistically calls
  `ingest_session()` if a transcript is already on disk. Field names
  (`hook_event_name`, `session_id`, `transcript_path` (nullable), `cwd`)
  come from the real schemas at
  `reference/codex/codex-rs/hooks/schema/generated/*.command.input.schema.json`,
  not guessed. Fails open by design: every exception is caught, logged to
  stderr, and swallowed -- the process always exits 0 with empty stdout
  (valid per the output schema; every field there is optional). DB path is
  derived from the hook payload's own `cwd` field
  (`<cwd>/.trace-mind/research.db`), not this process's cwd, since those
  can differ depending on how the agent invokes hook commands.
  `integrations/codex/config.toml.example` has the exact `[hooks]` TOML to
  add to a project's `.codex/config.toml` (or the global
  `~/.codex/config.toml` -- not installed automatically either way; that's
  the user's call, same as the existing note about `~/.claude/settings.json`
  below).
- `projection/graphviz_export.py` -- `graph_nodes`/`graph_edges` -> DOT,
  rendered to PNG or SVG via the system `dot` binary (no new Python
  dependency; raises a clear `RuntimeError` pointing at `brew install
  graphviz` if `dot` isn't on PATH). Used to visualize the real-session
  graph below.
- `projection/html_export.py` -- `trace-mind graph export --html`: one
  self-contained HTML file (inline SVG + a JSON blob of node
  details/edges/provenance + ~150 lines of vanilla JS for pan/zoom and
  click-to-inspect). No server, no CDN, no bundler -- opens directly via a
  `file://` URL, fully offline. Clicking a node shows the same
  title/summary/status/edges/provenance that `trace-mind why <node>`
  prints on the CLI, one hop only (not a recursive ancestor walk).
  `dev/render_demo_graph.py` regenerates `assets/demo-graph.html` alongside
  the PNG.
- `projection/ascii_export.py` -- `trace-mind map`: the whole project's
  graph as a horizontal (`tree`-style) ASCII tree, most-recently-touched
  node highlighted as "YOU ARE HERE". The graph is a DAG with no consistent
  parent/child edge-direction convention (real `DERIVED_FROM` edges in
  `local/kgw-example/build.py` point both ways depending on the pair), so
  the tree is a **connectivity-only BFS spanning tree** rooted at `goal`
  nodes -- adjacency treats every edge as undirected for shape purposes,
  but every connector is still annotated with the edge's real type and
  real declared direction, so the tree shape never misrepresents what an
  edge actually says. Nodes no BFS reaches render under an `(unlinked)`
  heading. `--color always` has to explicitly pass `color=True` through to
  `click.echo()` -- Click strips ANSI by default whenever stdout isn't a
  real terminal, which is true for every actual caller of this flag (a
  coding agent's shell-out captures stdout through a pipe); this was a
  real bug caught by running the installed CLI end to end through a pipe,
  not by the unit tests, which call `render_ascii()` directly and never
  touch Click's output layer at all. See `integrations/README.md` for the
  Claude Code/Codex/Pi wiring and the platform gap below.
- `extraction/` -- the Milestone 5 LLM graph-diff extractor, per
  `docs/extraction_design.md`'s taxonomy. `prefilter.py` (build plan
  section 13: deterministic regex triggers, no classifier stage yet) gates
  everything else -- nothing reaches the LLM unless a window matches. The
  event ids a window covers are given to the model only as short local
  labels (`e1`, `e2`, ...) via `window.py`, never as real
  `normalized_events.id`s -- the model structurally cannot cite evidence it
  wasn't given; `apply.py` rejects any label that doesn't resolve.
  `client.py` calls GPT-5.6-Luna via the OpenAI Responses API
  (`OPENAI_API_KEY`, per user direction -- not the Anthropic API); the
  alternative backend the user mentioned (shell out to Codex/Claude as a
  coding-agent task) is not implemented, since it's a different enough
  call shape that building an abstraction for it before it exists would be
  speculative. `models.py` validates the model's JSON against the same
  ontology `graph.repository` enforces (build plan section 14's output
  contract) before anything is applied; a structurally malformed envelope
  is rejected whole, but one bad mutation inside an otherwise-good envelope
  is skipped without discarding the rest (`apply.py`). `ids.py` mints real
  node ids for the model's `temp_id`s (one counter per project+type-prefix).
  `runner.py` ties it together and enforces idempotence via
  `extraction_runs.UNIQUE(input_hash, prompt_version)` -- the hash covers
  only the event delta + prompt version, deliberately excluding the graph
  neighborhood (see the real bug this caught, below). `merge_suggestions`/
  `needs_review` are surfaced, never auto-applied (build plan section 15).
  Never called from `hooks/` -- hooks must stay LLM-free and off the
  critical path (section 11.2); `trace-mind extract` is a separate, manual
  command that ingests then extracts.
- `tests/fixtures/{claude,codex}/` -- synthetic fixtures, not copied from
  any real session (copying real local transcript content into this repo
  was deliberately refused mid-build as a provenance risk -- see git log).
  Field shapes mirror real transcripts; conversation content is fictional,
  built from the build plan's own canonical test scenario (section 27:
  Graphiti vs. Markdown vs. Postgres for persistent research memory), split
  across a Codex session (day 1: goal, options, evidence, decision to
  prototype Markdown) and a follow-up Claude Code session (day 2: outcome +
  revisit condition) -- exercising the cross-agent, cross-day continuity
  this whole tool exists for.

Key schema fact worth remembering: `NormalizedEvent.event_index` is a block
index *local to one transcript record* (0 for a plain-text line, 0..N-1 for
a multi-block message), never a running count across a `read_delta()` call.
Making it a running count was an actual bug caught during this build:
content hashes should also depend on it, but it must derive purely from
where the bytes sit, not from how many events preceded it in a particular
read -- otherwise identity (and therefore idempotent dedup) shifts depending
on where a read happened to start. Chronological order within a session
that spans one physical file is `(byte_start, event_index)`; across a
multi-file session, order by `(session_id, timestamp)` instead.

### Validated against real data

With the user's explicit read-only authorization, ran the built pipeline
against one of the user's own real, long-running (~1 month), multi-file
Codex research sessions -- read directly from `~/.codex/sessions/...`,
never copied into this repo, output written only to a scratch DB outside
any git-tracked directory. Result: tens of thousands of normalized events
across two physical rollout files under one logical session; a real
multi-node/edge decision graph built from actual turns (a goal, a rejected
early hypothesis with the external evidence that contradicted it, the
pivot decision, follow-on experiment phases, a quantitative outcome, and a
later action/decision pair), each node backed by a real event id and byte
range; `trace-mind why <node>` correctly walked the chain including across
the real compaction boundary between the two physical files. This run is
what surfaced the multi-file cursor bug above. Specifics of that research
project's content are intentionally not reproduced here.

The Milestone 5 extractor was also run for real against this session (on a
scratch copy of the local example DB, never the committed one) at two
turns independently identified and byte-verified during the
`docs/extraction_design.md` cross-check: an explicit user-initiated branch,
and a rejected experiment immediately followed by the next direction it
motivated. Findings:

- The rejection turn: the model correctly reused the two already-existing
  nodes it should have (the real goal, and the already-modeled rejected
  experiment) rather than duplicating either, and linked a new option
  it created back to both with real, resolvable evidence -- no hallucinated
  labels, nothing rejected by `apply.py`.
- The branch turn, first attempt: the model minted a brand-new `goal` node
  instead of linking to the existing one. Root cause, confirmed by
  inspecting the actual window built: the existing goal had been created
  once near the start of the project and never updated again, so a
  pure-recency top-15 neighborhood -- ordinary churn from later turns --
  had pushed it out of the context the model was given. It could not link
  to a node it was never shown. Fixed in `window.py` (goals are now always
  included regardless of recency) and covered by a regression test
  (`test_window_always_includes_goals_even_when_not_recent`).
- Same branch turn, re-run after the fix: the existing goal was correctly
  visible and correctly reused for one linkage, but the model still
  introduced a new, narrower `goal` node for the branch's specific
  objective rather than scoping it under the existing option/goal. This is
  the decomposition-granularity question `docs/extraction_design.md`
  already flagged as open, now with a concrete real example rather than a
  hypothetical -- left as a known v1 limitation, not fixed here.

Net: one real, mechanical bug found and fixed (goal visibility); one real,
open semantic-judgment limitation confirmed and left as-is, consistent
with the acceptance criteria actually being tested (provenance, schema
validation, idempotence) rather than extraction quality, which build plan
section 22 correctly treats as a separate, later measurement problem
needing a labeled corpus.

### Hook latency: measured

Build plan section 11.1 targets p95 < 50ms for the hook enqueue path and
explicitly calls that "a benchmark, not an assumption" -- measured rather
than assumed:

```
echo '<Stop payload>' | .venv/bin/trace-mind hook --provider codex
```

consistently takes ~110-130ms wall time (direct venv binary, no `uv run`
overhead). `python3 -X importtime` traced it: `pydantic` (~56ms, via
`normalize.models`) and `typer` (~24ms) import cost dominate; the actual
hook logic (stdin parse, one SQLite insert, opportunistic ingest) is a
small fraction of that. In practice this doesn't cost the user turn
latency: `Stop` fires once per turn, in a separate process, off the
critical path -- the 50ms target in section 11.1 is about not blocking the
agent, which this doesn't. It's still worth recording because it's 2-3x
the stated benchmark and because a future `UserPromptSubmit` or
per-tool-call hook (section 11.1's "add later" list) would put this cost
on a much hotter path. If that happens, the fix is probably a separate,
minimal-import hook entry point rather than reusing the full Typer app --
not done now since nothing currently needs it.

### Live install

`uv tool install .` was run so a global `trace-mind` binary exists on PATH
(`~/.local/bin/trace-mind`) for hook commands to invoke without hardcoding
a venv path. This is a **non-editable** install -- future code changes in
this repo do not automatically reach it. Any session continuing this build
needs `cd /Users/hliu/temp/trace_mind && uv tool install . --reinstall`
after changing hook-path code, or the live hooks below keep running the
old version.

Hooks are wired into `/Users/hliu/temp/watermark/.codex/config.toml`
(per-project, user's choice over the global `~/.codex/config.toml`
alternative) for `SessionStart`/`Stop`/`PreCompact`/`SessionEnd`. Verified
end to end against that exact path before leaving it live. Not wired
anywhere else.

## Slash-command integrations

`trace-mind map` (above) is meant to be triggered from inside whichever
coding agent the user is already talking to, not just the bare CLI.
Checked against each platform's real source/docs before building this
(`integrations/README.md` has the full detail):

- **Claude Code**: a real, user-typed `/map` works
  (`integrations/claude-code/commands/map.md`) -- still one model turn,
  a few seconds' latency, not instant.
- **Codex has no user-pluggable slash command mechanism at all** -- its
  command list is a closed, compiled-in Rust enum
  (`reference/codex/codex-rs/tui/src/slash_command.rs`), and an
  unrecognized leading-slash string is actively rejected. The closest
  equivalent is a Skill (`integrations/codex/skills/map/SKILL.md`),
  invoked via the `/skills` picker or natural language -- never by typing
  `/map` itself. This is a real platform gap, documented as such rather
  than worked around with something that would misleadingly look the
  same as the other two.
- **Pi**: a real, user-typed `/map` via an extension
  (`integrations/pi/trace-mind-map.ts`), and the only one of the three
  where the handler runs with zero LLM turn -- genuinely instant. Built
  from Pi's published docs only; no local Pi source was available to
  verify against the way the Codex/Claude Code claims above were.

None of the three are installed automatically -- same manual-copy
convention as `integrations/codex/config.toml.example`.

## What's stubbed but not yet implemented

`provenance/` (only its table exists -- `graph/repository.py` writes
provenance rows directly today), `recall/`, `mcp/`, and the Claude Code
half of `hooks/` are not built. `extraction/` (Milestone 5) is now a
working v1 slice -- see above -- but has no labeled corpus and no measured
precision/recall (build plan section 22); "not yet implemented" no longer
applies to it, "not yet measured" does. `projection/` covers the *graph*
projection (DOT/PNG/interactive HTML) -- the narrative `RESEARCH_MAP.md`
Markdown projection (build plan section 17) is still not built; that's a
different, text-first view (active questions, branches, rejected/dormant
callouts) that the graph viewer doesn't replace.

## Next steps, in the order the build plan recommends (section 26)

1. Claude Code half of Milestone 2 (`hooks/claude.py` + `trace-mind hook
   --provider claude`) -- same pattern as `hooks/codex.py`, different
   field names (Claude Code's hook JSON schema hasn't been read from
   `reference/claude-code/` yet; do that before writing it, same discipline
   as the Codex side). Needs the user's sign-off before touching
   `~/.claude/settings.json` (global, shared across every session on this
   machine) -- same as the Codex integration file, installation into any
   real config.toml/settings.json is not automatic.
2. Milestone 5 hardening: a labeled corpus and measured precision/recall
   per node/edge type (build plan section 22-23; "Rejected Alternative
   Recall" is the metric that matters most), lexical/FTS neighborhood
   retrieval to replace the recency-only heuristic beyond the goals-always
   fix already made, and a decision on the three open questions in
   `docs/extraction_design.md` (decomposition granularity -- now with a
   second real example, see above; extraction cadence; whether queued
   sequence items get nodes).
3. Milestones 6-9: Markdown projection (`RESEARCH_MAP.md` -- the actual
   human-facing deliverable; not yet built, deliberately, pending direction
   on scope/output location), MCP recall, backfill, synthesis.

## Reference repos

Cloned into `reference/` (gitignored, read-only, never vendored):
Deciduous, RoBrain, Memory Bank, Loregraph, Basic Memory, ADR Kit, OpenAI
Codex, Anthropic Claude Code. `base76-research-lab/claude-code-hooks` 404'd
on clone -- likely no longer public or never existed under that path.
`REFERENCES.lock`/`REFERENCES.urls` in `reference/` pin the exact commits
studied.
