#!/usr/bin/env python3
"""Re-attach cached Domestic injuries/availability context with zero provider calls.

The rotating odds refresh rebuilds parts of domestic_odds.json. This helper restores the
already-cached fixture mappings, injuries/suspensions and base confirmed-lineup fields before
the dedicated lineup/formation and current-squad attach-only helpers add their own extensions.
It deliberately imports the canonical context code so the public JSON contract stays identical.
"""
from __future__ import annotations

import json

import enrich_domestic_match_context as context


def main() -> int:
    feed = context.load(context.ODDS_PATH, {})
    aliases = context.build_alias_lookup(context.load(context.ALIASES_PATH, {}))
    cache = context.load(
        context.CACHE_PATH,
        {"schemaVersion": 1, "matchMappings": {}, "teamLineups": {}, "teamSquads": {}},
    )
    now = context.now_utc()
    report = {
        "generatedAt": context.iso_utc(now),
        "candidateMatches": 0,
        "matchesWithContext": 0,
        "publishedUnavailablePlayers": 0,
        "requestsUsed": 0,
        "source": "cached Domestic match context only",
    }

    if not isinstance(feed, dict) or not feed.get("leagues"):
        print(json.dumps({**report, "status": "skipped_empty_feed"}, ensure_ascii=False))
        return 0

    candidates = context.candidate_matches(feed, aliases, now)
    report["candidateMatches"] = len(candidates)
    context.attach_context(candidates, cache, now, report)
    context.save(context.ODDS_PATH, feed)

    report["status"] = "ok"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
