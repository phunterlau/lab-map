#!/usr/bin/env bash
# Install trace-mind's /map integration for Claude Code, Codex, and/or Pi.
#
# Source files always come from this script's own location (so it works
# the same whether you're running it from inside this repo or from a copy
# elsewhere); *project-local* targets (.claude/commands/, .codex/skills/,
# `pi install -l`) are relative to wherever you RUN this script from, i.e.
# your own project -- not this repo -- so this script is reusable, not
# trace-mind-specific. Nothing here is run automatically by anything else;
# see integrations/README.md for what each platform actually supports.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLAUDE_CODE_SRC="$SCRIPT_DIR/claude-code/commands/map.md"
CODEX_SRC="$SCRIPT_DIR/codex/skills/map"
PI_SRC="$SCRIPT_DIR/pi/trace-mind-map"

usage() {
  cat <<'EOF'
Usage: integrations/install.sh [options]

  --claude-code          Install /map for Claude Code, this project only
                          (./.claude/commands/map.md)
  --claude-code-global   Install /map for Claude Code, every project
                          (~/.claude/commands/map.md)
  --codex                Install the map Skill for Codex, this project only
                          (./.codex/skills/map/) -- invoked via /skills or
                          plain language, Codex has no literal /map command
  --codex-global         Install the map Skill for Codex, every project
                          (~/.codex/skills/map/)
  --pi                   Install the map extension for Pi, this project only
                          (`pi install -l`)
  --pi-global            Install the map extension for Pi, every project
                          (`pi install`)
  --all                  Every *-global variant above
  -h, --help             Show this help

Prerequisite for all of these: `trace-mind` must resolve on PATH
(cd <trace-mind repo> && uv tool install .), and the project you want to
map must already have an ingested graph (trace-mind ingest / extract).
EOF
}

warn_if_trace_mind_missing() {
  if ! command -v trace-mind >/dev/null 2>&1; then
    echo "warning: 'trace-mind' is not on PATH -- /map will install but won't run until it is" \
      "(cd <trace-mind repo> && uv tool install .)" >&2
  fi
}

install_claude_code() {
  local dest="$1/.claude/commands/map.md"
  mkdir -p "$(dirname "$dest")"
  cp "$CLAUDE_CODE_SRC" "$dest"
  echo "installed: $dest"
}

install_codex() {
  local dest="$1/.codex/skills/map"
  mkdir -p "$dest"
  cp "$CODEX_SRC/SKILL.md" "$dest/SKILL.md"
  echo "installed: $dest/SKILL.md"
}

install_pi() {
  local scope_flag="$1"  # "" for global, "-l" for project-local
  if ! command -v pi >/dev/null 2>&1; then
    echo "error: 'pi' is not on PATH -- install pi.dev first, then re-run with --pi/--pi-global" >&2
    return 1
  fi
  # shellcheck disable=SC2086
  pi install $scope_flag "$PI_SRC"
}

if [ "$#" -eq 0 ]; then
  usage
  exit 1
fi

did_something=0
for arg in "$@"; do
  case "$arg" in
    --claude-code)
      warn_if_trace_mind_missing
      install_claude_code "$(pwd)"
      did_something=1
      ;;
    --claude-code-global)
      warn_if_trace_mind_missing
      install_claude_code "$HOME"
      did_something=1
      ;;
    --codex)
      warn_if_trace_mind_missing
      install_codex "$(pwd)"
      did_something=1
      ;;
    --codex-global)
      warn_if_trace_mind_missing
      install_codex "$HOME"
      did_something=1
      ;;
    --pi)
      warn_if_trace_mind_missing
      install_pi "-l"
      did_something=1
      ;;
    --pi-global)
      warn_if_trace_mind_missing
      install_pi ""
      did_something=1
      ;;
    --all)
      warn_if_trace_mind_missing
      install_claude_code "$HOME"
      install_codex "$HOME"
      install_pi ""
      did_something=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "unknown option: $arg" >&2
      usage
      exit 1
      ;;
  esac
done

[ "$did_something" -eq 1 ]
