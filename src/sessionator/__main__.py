"""``python -m sessionator`` entry point (used by the detached backfill kick)."""

from __future__ import annotations

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
