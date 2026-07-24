#!/usr/bin/env python3
"""Run the Domestic registry pipeline behind the 53-league Stats scope guard."""
from __future__ import annotations

import domestic_live_july_pipeline as pipeline
import statmaker_domestic_scope as scope


def main() -> int:
    scope.install_stats_registry_build_guard(pipeline)
    return pipeline.main()


if __name__ == "__main__":
    raise SystemExit(main())
