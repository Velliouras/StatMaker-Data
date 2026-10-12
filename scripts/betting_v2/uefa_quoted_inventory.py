"""Offline exact-bookmaker inventory from an EXPLICIT UEFA odds repository checkout.

Never calls providers; never treats an Odds-API.io event id as an API-Football
fixture id, a bookmaker quote timestamp, or a model certification. Caller must
point --uefa-odds-root at the live bookmaker-artifact checkout; main's inactive
2026-07 source is not silently promoted.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import json

from scripts.betting_v2.production_contract import Blocked
from scripts.betting_v2.production_sources import market_identity

COMPETITIONS = {
    "champions_league": ("CL", "Champions League", "champions_league_odds.json"),
    "europa_league": ("EL", "Europa League", "europa_league_odds.json"),
    "conference_league": ("UECL", "Conference League", "conference_league_odds.json"),
}
MAX_FEED_AGE = timedelta(hours=36)


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def exact_uefa_quotes(odds_root: Path, generation: str, now: datetime) -> tuple[list[dict], Counter]:
    """Only real, non-expired, exact bookmaker quotes; record every excluded row."""
    inventory: list[dict] = []
    blocked: Counter = Counter()
    for comp, (league, title, filename) in COMPETITIONS.items():
        source = odds_root / filename
        if not source.is_file():
            blocked[f"{league}_MISSING_QUOTES_FILE"] += 1
            continue
        original = source.read_bytes()
        payload = json.loads(original)
        if payload.get("schemaVersion") != 1 or payload.get("leagueCode") != league:
            blocked[f"{league}_INVALID_SOURCE_CONTRACT"] += 1
            continue
        timestamp = parse_time(payload.get("generatedAt", ""))
        if timestamp is None or timestamp > now or now - timestamp > MAX_FEED_AGE:
            blocked[f"{league}_STALE_OR_INVALID_QUOTES"] += 1
            continue
        sha = hashlib.sha256(original).hexdigest()
        for match in payload.get("matches", []):
            match_id = str(match.get("id") or "").strip()
            kickoff = parse_time(match.get("kickoff", ""))
            home = str(match.get("homeTeam") or "").strip()
            away = str(match.get("awayTeam") or "").strip()
            if not match_id or not kickoff or kickoff <= now or not home or not away:
                blocked[f"{league}_INELIGIBLE_FIXTURE"] += 1
                continue
            offered = list(match.get("markets") or [])
            # Provider match / market identities are immutable source identifiers,
            # not guessed API-Football fixture crosswalks.
            for ordinal, offer in enumerate(offered):
                bookmaker = str(offer.get("bookmaker") or "").strip()
                if offer.get("exactBookmakerOdds") is not True or not bookmaker:
                    blocked[f"{league}_NOT_EXACT_BOOKMAKER"] += 1
                    continue
                market = str(offer.get("market") or "").strip()
                selection = str(offer.get("selection") or "").strip()
                price = offer.get("odds")
                if not market or not selection or isinstance(price, bool):
                    blocked[f"{league}_MISSING_MARKET_OR_PRICE"] += 1
                    continue
                try:
                    odd = float(price)
                except (ValueError, TypeError):
                    blocked[f"{league}_INVALID_PRICE"] += 1
                    continue
                if not 1.8 < odd < 3.0:
                    blocked[f"{league}_OUTSIDE_UAT_PRICE_BAND"] += 1
                    continue
                row = {
                    "competitionId": comp, "generationId": generation,
                    "snapshotVersion": sha,
                    # This is the bookmaker-event identifier, not a historic fixture id.
                    "fixtureId": match_id, "fixtureKey": f"uefa-bookmaker:{comp}:{match_id}",
                    "leagueCode": league, "season": str(payload.get("season") or ""),
                    "kickoff": kickoff.isoformat(), "homeTeam": home, "awayTeam": away,
                    "providerHomeTeam": match.get("providerHomeTeam") or home,
                    "providerAwayTeam": match.get("providerAwayTeam") or away,
                    "canonicalHomeTeam": match.get("canonicalHomeTeam") or home,
                    "canonicalAwayTeam": match.get("canonicalAwayTeam") or away,
                    "market": market, "selection": selection,
                    "team": offer.get("team"), "line": offer.get("line"), "odd": odd,
                    "usableForStats": bool(match.get("usableForStats", False)),
                    "sourceRecordSha256": hashlib.sha256(
                        json.dumps(offer, sort_keys=True, ensure_ascii=False).encode("utf-8")
                    ).hexdigest(),
                }
                exact_match = {
                    "id": match_id, "date": match.get("date") or kickoff.date().isoformat(),
                    "kickoff": kickoff.isoformat(), "country": "UEFA",
                    "leagueCode": league, "competition": title, "season": row["season"],
                    "homeTeam": home, "awayTeam": away,
                    "providerHomeTeam": row["providerHomeTeam"],
                    "providerAwayTeam": row["providerAwayTeam"],
                    "canonicalHomeTeam": row["canonicalHomeTeam"],
                    "canonicalAwayTeam": row["canonicalAwayTeam"],
                    "teamMappingStatus": match.get("teamMappingStatus") or "provider_identity",
                    "usableForStats": row["usableForStats"],
                }
                row["externalQuoteSource"] = {
                    "bookmaker": bookmaker, "exactBookmakerOdds": True,
                    "sourceType": "EXACT_REPOSITORY_UEFA_BOOKMAKER_FEED",
                    "sourceSha256": sha, "sourceGeneratedAt": timestamp.isoformat(),
                    "file": filename, "marketOrdinal": ordinal, "match": exact_match,
                }
                row["bookmaker"] = bookmaker
                try:
                    row.update(market_identity(row))
                except (ValueError, Blocked) as exc:
                    row["blocker"] = str(exc)
                inventory.append(row)
    return inventory, blocked
