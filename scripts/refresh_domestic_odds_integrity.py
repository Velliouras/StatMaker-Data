#!/usr/bin/env python3
"""Run the Domestic odds refresh with parser integrity and 53-league scope guards."""
from __future__ import annotations

import os

import refresh_domestic_live_july_odds as target
import statmaker_domestic_scope as scope
from odds_market_integrity import install_parser_guard


DEFAULT_DOMESTIC_RATE_LIMIT_RESERVE = 45


def main() -> int:
    install_parser_guard(target.odds_fetch)
    scope.install_odds_registry_load_guard(target.pipeline)

    # Domestic and UEFA refreshes share the same Odds-API.io key in one workflow.
    # Preserve a meaningful hourly reserve for the UEFA CL/EL/Conference stage.
    reserve = int(os.getenv("ODDS_API_IO_DOMESTIC_RATE_LIMIT_RESERVE", str(DEFAULT_DOMESTIC_RATE_LIMIT_RESERVE)))
    target.odds_fetch.RATE_LIMIT_STOP_BELOW = max(
        int(target.odds_fetch.RATE_LIMIT_STOP_BELOW),
        max(1, reserve),
    )
    return target.main()


if __name__ == "__main__":
    raise SystemExit(main())
