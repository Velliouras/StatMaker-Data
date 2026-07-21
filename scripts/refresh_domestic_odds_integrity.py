#!/usr/bin/env python3
"""Run the Domestic odds refresh with bookmaker-preserving parser integrity."""

from __future__ import annotations

import refresh_domestic_live_july_odds as target
from odds_market_integrity import install_parser_guard


def main() -> int:
    install_parser_guard(target.odds_fetch)
    return target.main()


if __name__ == "__main__":
    raise SystemExit(main())
