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

## What's built (build plan Milestones 0-4 + Codex half of Milestone 2)

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

## What's stubbed but not yet implemented

`extraction/`, `provenance/` (only its table exists), `recall/`, `mcp/`,
and the Claude Code half of `hooks/` are not built. `projection/` covers
the *graph* projection (DOT/PNG/interactive HTML) -- the narrative
`RESEARCH_MAP.md` Markdown projection (build plan section 17) is still not
built; that's a different, text-first view (active questions, branches,
rejected/dormant callouts) that the graph viewer doesn't replace.

## Next steps, in the order the build plan recommends (section 26)

1. Claude Code half of Milestone 2 (`hooks/claude.py` + `trace-mind hook
   --provider claude`) -- same pattern as `hooks/codex.py`, different
   field names (Claude Code's hook JSON schema hasn't been read from
   `reference/claude-code/` yet; do that before writing it, same discipline
   as the Codex side). Needs the user's sign-off before touching
   `~/.claude/settings.json` (global, shared across every session on this
   machine) -- same as the Codex integration file, installation into any
   real config.toml/settings.json is not automatic.
2. Milestone 5: LLM graph-diff extraction -- design in `docs/extraction_design.md`,
   grounded in a real cross-check of `local/kgw-example/`'s graph against
   the full real transcript (a 7-type "moment taxonomy": OFFER_AND_PICK vs.
   SEQUENCE_PICK is the sharpest distinction found, since both look like
   "agent lists options, user picks one" but only one of them means the
   others were rejected). Not yet implemented -- needs a labeled corpus;
   out of scope until 1 is solid. Extractor backend, per user direction:
   either shell out to Codex/Claude Code as a coding-agent task (i.e. give
   the agent native prompt access, not a raw API call), or call OpenAI's
   GPT-5.6-Luna directly via the `OPENAI_API_KEY` env var
   (https://developers.openai.com/api/docs/models/gpt-5.6-luna). Not an
   Anthropic API call.
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
