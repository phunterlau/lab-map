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

## What's built (build plan Milestones 0-3, one vertical slice)

Real transcript file -> adapter -> `NormalizedEvent` -> SQLite -> cursor
advance -> re-run is idempotent.

- `normalize/models.py` -- `NormalizedEvent`, provider-neutral. Nothing
  outside `transcripts/` touches raw Claude/Codex JSON.
- `transcripts/claude.py`, `transcripts/codex.py` -- adapters, written
  against real local schema (JSONL field shapes learned by reading actual
  `~/.claude/projects/*/*.jsonl` and `~/.codex/sessions/**/*.jsonl` files on
  this machine -- not from docs, not copied into this repo).
- `storage/db.py` -- SQLite schema. One deliberate deviation from the build
  plan's draft schema (section 9): `normalized_events` dedups on
  `(session_id, byte_start, byte_end, event_index)`, not
  `(session_id, content_hash)`. Two identical short messages in one session
  ("yes" sent twice) would collide on content hash alone and the second
  would silently vanish.
- `normalize/pipeline.py` -- `ingest_session()`. Event insertion and cursor
  advancement happen in one SQLite transaction: a crash between them can't
  happen, so a re-run after a crash re-reads from the last *committed*
  offset and re-derives exactly the same events (verified in
  `tests/test_idempotence.py`). Detects truncation/rotation by file-size
  shrink or a changed leading-bytes hash and resets the cursor rather than
  erroring.
- `cli.py` -- `trace-mind <transcript> --provider claude|codex --session-id
  <id> --db <path>`. Manual entry point for now; hooks and backfill will
  call the same `ingest_session()`.
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
on where a read happened to start. Chronological order within a session is
`(byte_start, event_index)`, not `event_index` alone.

## What's stubbed but not yet implemented

`graph/`, `extraction/`, `provenance/`, `projection/`, `recall/`, `mcp/`,
`hooks/` exist as empty packages. Graph/provenance *tables* exist in
`storage/db.py` (section 9's schema) but nothing writes to them yet.

## Next steps, in the order the build plan recommends (section 26)

1. Milestone 4: manual graph CLI (`node add/show`, `edge add`) against the
   existing `graph_nodes`/`graph_edges`/`provenance` tables -- exercise the
   section 27 scenario by hand before automating extraction.
2. Milestone 2: `hooks/` -- `SessionStart`/`Stop`/`PreCompact`/`SessionEnd`
   entry points for Claude and Codex that enqueue into `hook_events` and
   call `ingest_session()`. Needs the user's sign-off before touching
   `~/.claude/settings.json` (global, shared across every session on this
   machine).
3. Milestone 5: LLM graph-diff extraction -- needs a labeled corpus; out of
   scope until 1-2 are solid. Extractor backend, per user direction: either
   shell out to Codex/Claude Code as a coding-agent task (i.e. give the
   agent native prompt access, not a raw API call), or call OpenAI's
   GPT-5.6-Luna directly via the `OPENAI_API_KEY` env var
   (https://developers.openai.com/api/docs/models/gpt-5.6-luna). Not an
   Anthropic API call.
4. Milestones 6-9: Markdown projection, MCP recall, backfill, synthesis.

## Reference repos

Cloned into `reference/` (gitignored, read-only, never vendored):
Deciduous, RoBrain, Memory Bank, Loregraph, Basic Memory, ADR Kit, OpenAI
Codex, Anthropic Claude Code. `base76-research-lab/claude-code-hooks` 404'd
on clone -- likely no longer public or never existed under that path.
`REFERENCES.lock`/`REFERENCES.urls` in `reference/` pin the exact commits
studied.
