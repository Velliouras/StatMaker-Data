"""Current exact prices are a display overlay; published issues remain immutable."""
import math
import re
from datetime import datetime, timezone


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def _raw_price(row, raw):
    match = row["match"]
    names = {"MATCH_SHOTS": "Total Shots", "TEAM_SHOTS": "Total Shots",
             "MATCH_SHOTS_ON_TARGET": "Total Shots on Target",
             "TEAM_SHOTS_ON_TARGET": "Total Shots on Target"}
    name = row.get("providerMarketName") or names.get(row["market"])
    if row["market"] in names and row.get("scope") == "TEAM":
        name += " Home" if row["teamSide"] == "HOME" else " Away"
    markets = raw.get((str(match["id"]), row["bookmaker"], name), [])
    if not markets:
        return None
    clocks = [float(m.get("_observedAt") or timestamp(m.get("updatedAt"))) for m in markets]
    latest = max(clocks)
    direction = re.search(r"\b(over|under)\b", row["selection"], re.I)
    if direction is None or row.get("line") is None:
        return None
    prices = set()
    for market, observed in zip(markets, clocks):
        if observed != latest or (market.get("_kickoff"), market.get("_home"), market.get("_away")) != (
                match["kickoff"], match.get("providerHomeTeam"), match.get("providerAwayTeam")):
            continue
        for offer in market.get("odds", []):
            try:
                if float(offer.get("hdp")) == float(row["line"]):
                    prices.add(float(offer[direction[1].lower()]))
            except (KeyError, TypeError, ValueError):
                continue
    return (prices.pop(), latest) if len(prices) == 1 else None


def _receipt_price(row, lookup):
    q, match = row["quoteSpec"], row["match"]
    if (str(q["providerEventId"]), q["bookmaker"], q["kickoff"], q.get("line")) != (
            str(match["id"]), row["bookmaker"], timestamp(row["kickoff"]), row.get("line")):
        return None
    group = lookup.get((str(q["providerEventId"]), q["kickoff"], q["bookmaker"], q["sourceMarket"]))
    if group is None:
        return None
    prices = {float(r["odd"]) for r in group["rows"]
              if str(r["selection"]) == str(q["selection"]) and r.get("line") == q.get("line")
              and str(r.get("providerSelectionId")) == str(q.get("providerSelectionId"))
              and r.get("_providerHome") == match.get("providerHomeTeam")
              and r.get("_providerAway") == match.get("providerAwayTeam")}
    return (prices.pop(), group["observed"]) if len(prices) == 1 else None


def price_updates(issued, raw, lookup, now, allowed, eligible):
    result = []
    for row in issued:
        if (row.get("outcome") != "PENDING" or timestamp(row["kickoff"]) <= now
                or not row.get("publishedAt") or not allowed(row["market"], now)):
            continue
        if row["model"] == "ALL_MARKETS_JOINT_V1" or row.get("probabilityModel") == "FROZEN_JOINT_POINT":
            current = _receipt_price(row, lookup)
        else:
            current = _raw_price(row, raw)
        if current is None:
            continue
        odd, observed = current
        if (not all(math.isfinite(v) for v in (odd, observed)) or odd == row["odd"]
                or not max(now - 86400, timestamp(row["publishedAt"]), timestamp(row["quoteUpdatedAt"])) <= observed <= now
                or not eligible(row["winProbability"], row["pushProbability"], odd)):
            continue
        update = {k: row.get(k) for k in ("selectionId", "apiFixtureId", "bookmaker", "market", "selection", "team", "line", "kickoff")}
        update.update(providerEventId=str(row["match"]["id"]), originalOdd=row["odd"], odd=odd,
                      observedAt=iso(observed), indicativeEV=row["winProbability"] * odd + row["pushProbability"] - 1)
        result.append(update)
    return result


def validate_price_updates(payload):
    """Enforce the same identity, clocks and admission contract before publishing."""
    updates = payload.get("priceUpdates", [])
    if len(updates) != payload.get("priceUpdateCount", 0):
        raise ValueError("Price update count mismatch")
    issued = {r["selectionId"]: r for r in payload.get("issued", []) + payload.get("marketIssued", [])}
    legacy_ids = {r["selectionId"] for r in payload.get("candidates", []) + payload.get("marketCandidates", [])}
    seen = set()
    generated = timestamp(payload["generatedAt"])
    for update in updates:
        key = update["selectionId"]
        row = issued.get(key)
        if row is None or key in seen or key in legacy_ids:
            raise ValueError("Price update has no unique withdrawn issue")
        seen.add(key)
        if any(update.get(k) != row.get(k) for k in ("apiFixtureId", "bookmaker", "market", "selection", "team", "line", "kickoff")):
            raise ValueError("Price update changed selection identity")
        odd, win, push = update["odd"], row["winProbability"], row["pushProbability"]
        observed = timestamp(update["observedAt"])
        if (update["providerEventId"] != str(row["match"]["id"]) or update["originalOdd"] != row["odd"]
                or odd == row["odd"] or row["outcome"] != "PENDING" or not generated < timestamp(row["kickoff"])
                or not max(generated - 86400, timestamp(row["publishedAt"]), timestamp(row["quoteUpdatedAt"])) <= observed <= generated
                or not all(math.isfinite(v) for v in (odd, win, push, update["indicativeEV"]))
                or not (.60 <= win <= 1 and 0 <= push <= 1 and win + push <= 1.000001 and 1.8 < odd < 3)
                or win * odd + push - 1 <= 0 or abs(update["indicativeEV"] - (win * odd + push - 1)) > .000003):
            raise ValueError("Invalid current price or expected value")
    return updates
