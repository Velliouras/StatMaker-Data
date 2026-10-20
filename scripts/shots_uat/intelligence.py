"""Auditable game/market facts, separate from immutable numerical predictions."""
import math
from collections import defaultdict
from .features import FEATURES, History
from .sources import Identities, timestamp
from .train import ROOT


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def finite(value):
    return float(value) if isinstance(value, (float, int)) and math.isfinite(value) else None


def market_name(row):
    name = "Total Shots" if row["metric"] == "shots" else "Total Shots on Target"
    return name + (" Home" if row["teamSide"] == "HOME" else " Away") if row["scope"] == "TEAM" else name


def market_facts(row, raw, now):
    """Exact same event/kickoff/teams/line/side; never mix books to remove margin."""
    m = row["match"]; offers = []; names = market_name(row)
    for (event, book, name), markets in raw.items():
        if event != row["fixtureId"] or name != names:
            continue
        groups = defaultdict(lambda: defaultdict(set))
        for market in markets:
            clock = float(market.get("_observedAt") or timestamp(market.get("updatedAt")))
            if (not now - 86400 <= clock <= now or market.get("_kickoff") != m["kickoff"]
                    or market.get("_home") != m.get("providerHomeTeam")
                    or market.get("_away") != m.get("providerAwayTeam")):
                continue
            for quote in market.get("odds", []):
                try:
                    if float(quote.get("hdp")) != row["line"]:
                        continue
                    for side in ("over", "under"):
                        odd = float(quote.get(side, 0))
                        if 1 < odd < 1000:
                            groups[clock][side].add(odd)
                except (TypeError, ValueError):
                    continue
        if not groups:
            continue
        clock = max(groups); sides = groups[clock]; side = row["side"].lower()
        if len(sides[side]) != 1:
            continue
        price = next(iter(sides[side]))
        item = dict(bookmaker=book, odd=price, observedAt=iso(clock))
        opposite = "under" if side == "over" else "over"
        if len(sides[opposite]) == 1:
            other = next(iter(sides[opposite])); margin = 1 / price + 1 / other
            if .85 <= margin <= 1.30:
                item.update(oppositeOdd=other, overround=margin - 1,
                            marketConditionalProbability=(1 / price) / margin)
        offers.append(item)
    same_book = next((q for q in offers if q["bookmaker"] == row["bookmaker"]), None)
    push = row["pushProbability"]; p = row["winProbability"]
    result = dict(issuedOdd=row["odd"], breakEvenWinProbability=(1 - push) / row["odd"],
                  modelWinProbability=p, modelConditionalProbability=p / (1 - push),
                  modelFairOdd=(1 - push) / p, issuedEV=row["indicativeEV"],
                  noVigMethod="PROPORTIONAL_SAME_BOOK_SAME_LINE_CONDITIONAL_ON_NO_PUSH", offers=offers)
    if same_book:
        result["currentSameBook"] = same_book
        result["priceChangeSinceIssuePct"] = (same_book["odd"] / row["odd"] - 1) * 100
        if "marketConditionalProbability" in same_book:
            result["edgeVsMarketPercentagePoints"] = 100 * (p / (1 - push) - same_book["marketConditionalProbability"])
    return result


def team_facts(history, root, features, display):
    facts = dict(zip(FEATURES, features)); matches = list(history.teams[root]); recent = matches[-8:]
    form = []
    for match in matches[-5:]:
        gf, ga = match["values"][4:6]
        form.append("?" if gf is None or ga is None else "W" if gf > ga else "D" if gf == ga else "L")
    keys = ("own_8_shots_for", "own_8_shots_against", "own_8_sot_for", "own_8_sot_against",
            "own_8_xg_for", "own_8_xg_against", "own_venue_shots_for", "own_venue_shots_against")
    return dict(team=display, sample=len(matches), recentWindow=len(recent), formLast5=form,
                restDays=finite(facts["rest"]), elo=history.elo[root],
                xgAvailableMatches=sum(m["values"][6] is not None for m in recent),
                averages={k:finite(facts[k]) for k in keys})


def context_facts(row, current, now):
    context = current or row["match"].get("squadContext") or {}
    clock = timestamp(context.get("generatedAt"))
    if clock > now:
        context = {}; clock = 0.
    fresh = bool(clock and 0 <= now - clock <= 86400)
    sides = {}
    for side in ("home", "away"):
        values = context.get(side) or {}
        sides[side] = dict(injuriesCovered=bool(values.get("injuriesCovered")),
            lineupConfirmed=bool(values.get("lineupConfirmed")),
            unavailablePlayers=values.get("unavailablePlayers", []), formation=values.get("formation"))
    return dict(asOf=iso(clock) if clock and clock <= now else None, fresh=fresh, teams=sides,
                usedByFrozenProbability=False, motivation="NOT_ESTABLISHED_FROM_VERIFIED_DATA")


def annotate(rows, actual, raw, leagues, now):
    identity = Identities(ROOT, actual)
    contexts = {}
    for league in leagues:
        for match in league.get("matches", []):
            context = match.get("squadContext") or {}
            api = context.get("apiFootballFixtureId")
            if api and context.get("home") and context.get("away"):
                bound, _ = identity.bind(league, match)
                if not bound or bound[3] != int(api):
                    continue
                key = int(api), match.get("kickoff")
                if timestamp(context.get("generatedAt")) > timestamp(contexts.get(key, {}).get("generatedAt")):
                    contexts[key] = context
    history = History(actual); result = {}
    for row in sorted(rows, key=lambda r:timestamp(r["featureCutoff"])):
        cutoff = timestamp(row["featureCutoff"]); history.advance(cutoff)
        bound, reason = identity.bind(row["match"], row["match"])
        stats = None
        if bound:
            _, h, a, _ = bound; pair = history.pair(h, a, row["match"]["leagueCode"], timestamp(row["kickoff"]))
            stats = dict(home=team_facts(history, h, pair[0], row["homeTeam"]),
                         away=team_facts(history, a, pair[1], row["awayTeam"]),
                         eloGapWithHomeAdvantage=pair[0][1], binding=reason)
        context = contexts.get((row["apiFixtureId"], row["kickoff"]))
        info = dict(schemaVersion=1, asOf=iso(now), predictionAsOf=row["featureCutoff"], changesPrediction=False,
                    historicalFacts=stats, market=market_facts(row, raw, now), context=context_facts(row, context, now),
                    predictedCounts={k:row.get(k) for k in ("homeShots", "awayShots", "homeSot", "awaySot")},
                    explanationSemantics="FACTUAL_INPUTS_NOT_CAUSAL_FEATURE_ATTRIBUTIONS")
        result[row["selectionId"]] = dict(row, intelligence=info)
    return [result[r["selectionId"]] for r in rows]
