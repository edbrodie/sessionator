#!/usr/bin/env sh
# sessionator SessionEnd hook — best-effort, local-only session capture.
#
# Contract: exit 0 fast, always. Session teardown must never wait on ingestion
# and must never fail because sessionator is absent or errors. The heavy lifting
# (deterministic record write + detached summary backfill) is owned by the CLI's
# `ingest` command; this hook only kicks it in the background.
#
# Claude-side only. This hook never touches Codex or ~/.codex configuration.

set -u

# uv-tool installs land in ~/.local/bin, which a non-login hook shell may not
# have on PATH. Prepend it so `command -v` can find an installed sessionator.
PATH="$HOME/.local/bin:$PATH"
export PATH

# Drain the SessionEnd JSON payload on stdin so the writing side never blocks;
# the CLI rediscovers the finished session by scanning transcript dirs itself.
cat >/dev/null 2>&1 || true

# No-op when the CLI is not installed — capture is optional, never a failure.
if command -v sessionator >/dev/null 2>&1; then
  # Fully detached: the record write and backfill kick run in the background so
  # this hook returns instantly regardless of ingest duration.
  nohup sessionator ingest >/dev/null 2>&1 &
fi

exit 0
