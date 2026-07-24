#!/usr/bin/env python3
"""Run the Domestic odds refresh with parser integrity and final-scope guards."""
from __future__ import annotations

import refresh_domestic_live_july_odds as target
import statmaker_domestic_scope as scope
from odds_market_integrity import install_parser_guard


def main() -> int:
    install_parser_guard(target.odds_fetch)
    scope.install_registry_load_guard(target.pipeline)
    return target.main()


if __name__ == "__main__":
    raise SystemExit(main())
