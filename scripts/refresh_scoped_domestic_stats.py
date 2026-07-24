#!/usr/bin/env python3
"""Compatibility entrypoint for the 53-league incremental Domestic Stats refresh."""
from __future__ import annotations

import refresh_domestic_live_july_stats as target


def main() -> int:
    return target.main()


if __name__ == "__main__":
    raise SystemExit(main())
