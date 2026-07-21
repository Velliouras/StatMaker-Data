#!/usr/bin/env python3
"""Run the UEFA qualifier refresh with bookmaker-preserving parser integrity."""

from __future__ import annotations

import update_uefa_qualifier_odds_build as target
from odds_market_integrity import install_parser_guard


def main() -> int:
    install_parser_guard(target.base.odds)
    return target.main()


if __name__ == "__main__":
    raise SystemExit(main())
