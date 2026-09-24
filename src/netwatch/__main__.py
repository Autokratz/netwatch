"""Allow ``python -m netwatch``."""

from __future__ import annotations

from netwatch.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
