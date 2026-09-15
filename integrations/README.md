# Slash-command / skill integrations

All of these run `trace-mind map --color always` and show the result
verbatim -- an ASCII, horizontal (`tree`-style) view of the current
project's whole decision graph, most-recently-touched node highlighted as
"YOU ARE HERE". None of these are installed automatically by anything
else (no hook, no postinstall) -- you run `install.sh` yourself, or copy
files by hand.

```
./integrations/install.sh --claude-code        # this project, Claude Code
./integrations/install.sh --claude-code-global # every project, Claude Code
./integrations/install.sh --codex              # this project, Codex skill
./integrations/install.sh --codex-global       # every project, Codex skill
./integrations/install.sh --pi                 # this project, Pi extension
./integrations/install.sh --pi-global          # every project, Pi extension
./integrations/install.sh --all                # every *-global variant above
./integrations/install.sh --help
```

Project-local flags (`--claude-code`, `--codex`, `--pi`) install relative
to wherever you *run* the script from, not this repo -- so it's reusable
against any project, not just trace-mind's own. `--pi`/`--pi-global` need
`pi` on PATH and fail with a clear message if it's missing; every flag
warns (but still installs) if `trace-mind` itself isn't on PATH yet, since
that's a separate, later prerequisite from installing the command file.
Equivalent manual copy-the-file steps are still below, in case you'd
rather not run a script or want to see exactly what it does first.

Prerequisite for all three: `trace-mind` must resolve on PATH as a real
command (`cd <this repo> && uv tool install .`), and the target project
must already have an ingested graph (`trace-mind ingest`/`extract`) -- `map`
reads an existing DB, it doesn't create one.

## What's actually real per platform

Checked against each platform's own source/docs before building this, not
assumed:

- **Claude Code**: genuinely supports a user-typed `/map` via a real
  project or global custom command file
  (`integrations/claude-code/commands/map.md` -> copy to
  `.claude/commands/map.md` or `~/.claude/commands/map.md`). Still goes
  through one model turn (the command's body is a prompt Claude Code
  interprets), so there's a few seconds' latency, not instant.
- **Codex has no user-pluggable slash command at all.** Its command list
  is a closed, compiled-in set (checked against
  `reference/codex/codex-rs/tui/src/slash_command.rs` -- a fixed Rust
  enum) and an unrecognized leading-slash string is actively rejected as
  `UnknownCommand`. A literal typed `/map` is not possible in Codex. The
  closest equivalent is a **Skill**
  (`integrations/codex/skills/map/SKILL.md` -> copy to
  `.codex/skills/map/` or `~/.codex/skills/map/`), invoked via the
  `/skills` picker or by describing what you want in plain language --
  not by typing `/map`.
- **Pi** supports a real user-typed `/map` via an extension
  (`integrations/pi/trace-mind-map/` -> `pi install
  ./integrations/pi/trace-mind-map` for global, or `pi install -l
  ./integrations/pi/trace-mind-map` for project-local), and it's the only
  one of the three where the command handler runs with **zero LLM turn**
  -- genuinely instant, not model-mediated. A pi extension is an
  installable package (`package.json` declaring `"pi":
  {"extensions": [...]}` plus the `.ts` file it points at), not a loose
  file dropped into a folder -- confirmed against a real installed Pi
  extension on this machine and the actual
  `@earendil-works/pi-coding-agent` type definitions, not docs alone
  (unlike the first version of this integration); see the comment at the
  top of `extension.ts` for exactly what's confirmed and where.

## The color caveat

`trace-mind map --color always` forces ANSI color codes into its output
even though the process it's piped from (a coding agent's shell-out) is
never a real terminal -- Click (which the CLI is built on) strips color by
default in that situation, so this had to be forced explicitly; see
`cli.py`'s `map` command. Whether that color then actually *displays* once
it reaches you is a different, per-client question: a plain terminal CLI
will render it; whether an IDE-embedded client's tool-output panel does
too needs a one-time visual check, not something guaranteed by the file
format alone. The Claude Code command's prompt body explicitly tells the
model not to re-print the tree in its own reply for this reason -- a
model's own text response is never colored, only the raw tool-output block
the `!` execution produced is.
