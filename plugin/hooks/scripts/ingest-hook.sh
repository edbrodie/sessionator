#!/usr/bin/env bash
# sessionator capture hook: best-effort, local-only session capture.
#
# Wired to both PreCompact (async) and SessionEnd. Contract: exit 0 fast and
# always, print nothing on stdout. Compaction and session teardown must never
# wait on ingestion, never fail because sessionator is absent or errors, and
# never see stray stdout that an async handshake could misread.
#
# The heavy lifting is owned by the CLI: `sessionator ingest --hook` reads the
# hook payload from stdin, spools it atomically, detaches a worker, and returns.
#
# Claude-side only. This hook never touches Codex or ~/.codex configuration.
#
# Note: no `-e`. A failing command must not abort before the guaranteed exit 0.
set -uo pipefail

# uv-tool installs land in ~/.local/bin, which a non-login hook shell may not
# have on PATH. Prepend it so `command -v` can find an installed sessionator.
# ${HOME:-} keeps this safe under `set -u` when HOME is unset.
PATH="${HOME:-}/.local/bin:$PATH"
export PATH

# Per-project opt-out: .claude/sessionator.local.md with `enabled: false` in its
# YAML frontmatter disables capture for this project. Absent file means enabled.
STATE_FILE="${CLAUDE_PROJECT_DIR:-$PWD}/.claude/sessionator.local.md"
if [[ -f "$STATE_FILE" ]]; then
  FRONTMATTER=$(sed -n '/^---$/,/^---$/{ /^---$/d; p; }' "$STATE_FILE" 2>/dev/null)
  ENABLED=$(printf '%s\n' "$FRONTMATTER" | grep '^enabled:' | sed 's/enabled: *//' | sed 's/^"\(.*\)"$/\1/' | tr -d "'")
  if [[ "$ENABLED" == "false" ]]; then
    cat >/dev/null 2>&1 || true
    exit 0
  fi
fi

# No-op when the CLI is not installed. Capture is optional, never a failure.
# Drain stdin first so the writing side never blocks on a full pipe.
if ! command -v sessionator >/dev/null 2>&1; then
  cat >/dev/null 2>&1 || true
  exit 0
fi

# `ingest --hook` consumes the payload on stdin, spools it, detaches a worker,
# and exits well under a second. Silence and `|| true` keep the contract.
sessionator ingest --hook >/dev/null 2>&1 || true

exit 0
