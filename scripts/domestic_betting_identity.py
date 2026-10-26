"""Keep Domestic betting quotes consistent with the shared canonical identity.

Uses checked-out rosters, caches and verified aliases only; no provider requests.
Schedule identity and betting eligibility are separate: unresolved fixtures remain
in the calendar, but cannot acquire quotes from an archive or a second provider.
"""
from __future__ import annotations

from typing import Any, Mapping

import canonical_team_identity as identity


def betting_identity_ready(match: Mapping[str, Any]) -> bool:
    return (
        match.get("teamMappingStatus") == "matched"
        and match.get("usableForStats") is True
        and bool(match.get("canonicalHomeTeam"))
        and bool(match.get("canonicalAwayTeam"))
        and identity.equivalent(match.get("homeTeam"), match.get("canonicalHomeTeam"))
        and identity.equivalent(match.get("awayTeam"), match.get("canonicalAwayTeam"))
        and not identity.equivalent(match.get("canonicalHomeTeam"), match.get("canonicalAwayTeam"))
    )


def reconcile_feed(feed: dict, indexes=None) -> dict:
    """Repair exact known identities; suppress quotes on unresolved schedule rows.

    Existing matched identities are preserved. The same league-scoped resolver as
    ingestion and validation must resolve both sides before a cached schedule row
    can become betting eligible. Merely possessing a provider fixture ID is not
    sufficient. Provider names and raw market provenance remain unchanged.
    """
    if indexes is None:
        import domestic_live_july_pipeline as pipeline
        import update_domestic_odds_api_io as odds

        registry = pipeline.load_json(pipeline.REGISTRY_PATH, {}).get("leagues", [])
        indexes = identity.domestic_indexes(odds, pipeline, registry)

    report = {"mappedFixtures": 0, "suppressedFixtures": 0, "suppressedQuotes": 0,
              "scheduleFlagsRepaired": 0, "extraApiCalls": 0}
    for league in feed.get("leagues", []):
        index = indexes.get(str(league.get("leagueCode") or "").upper())
        for match in league.get("matches", []):
            if not betting_identity_ready(match) and index is not None:
                home = index.resolve(match.get("providerHomeTeam") or match.get("homeTeam"))[0]
                away = index.resolve(match.get("providerAwayTeam") or match.get("awayTeam"))[0]
                if home and away and home != away:
                    renamed = {str(match.get("homeTeam") or ""): home,
                               str(match.get("awayTeam") or ""): away}
                    for quote in match.get("markets", []):
                        old_team = quote.get("team")
                        if old_team in renamed and str(quote.get("market", "")).startswith("TEAM_"):
                            quote["team"] = renamed[old_team]
                            selection = str(quote.get("selection") or "")
                            if old_team and selection.startswith(old_team + " "):
                                quote["selection"] = renamed[old_team] + selection[len(old_team):]
                    match.update(homeTeam=home, awayTeam=away, canonicalHomeTeam=home,
                                 canonicalAwayTeam=away, teamMappingStatus="matched", usableForStats=True)
                    report["mappedFixtures"] += 1

            if betting_identity_ready(match):
                if match.get("markets") and match.get("scheduleOnly") is True:
                    match["scheduleOnly"] = False
                    report["scheduleFlagsRepaired"] += 1
                continue

            if match.get("markets"):
                report["suppressedQuotes"] += len(match["markets"])
                report["suppressedFixtures"] += 1
                match["markets"] = []
            if (match.get("scheduleVerified") is True
                    and match.get("scheduleSource") in {"api-football", "odds-api-io-events"}
                    and match.get("scheduleOnly") is not True):
                match["scheduleOnly"] = True
                report["scheduleFlagsRepaired"] += 1
            # Do not invent verified schedule provenance. An unverified row must
            # continue to fail the strict feed validator, even without quotes.
    return report


def main():
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", type=Path, default=Path("odds/odds_api_io/domestic_odds.json"))
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    feed = json.loads(args.feed.read_text(encoding="utf-8-sig"))
    report = reconcile_feed(feed)
    if args.write:
        args.feed.write_text(json.dumps(feed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
