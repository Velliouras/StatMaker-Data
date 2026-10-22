"""Auditable game/market facts, separate from immutable numerical predictions."""
import math
from collections import defaultdict, deque
from .features import FEATURES, History, normalized
from .sources import Identities, timestamp
from .train import ROOT


def iso(t):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(t, timezone.utc).isoformat().replace("+00:00", "Z")


def finite(value):
    return float(value) if isinstance(value, (float, int)) and math.isfinite(value) else None


def result_odds_context(row, raw, now):
    """One complete same-book FT 1X2 quote, bound to the actual provider fixture."""
    match = row["match"]
    books = sorted({book for event, book, name in raw if event == str(row["fixtureId"]) and name == "ML"},
                   key=lambda book: (book != row["bookmaker"], book))
    for book in books:
        groups = defaultdict(lambda: defaultdict(set))
        for market in raw.get((str(row["fixtureId"]), book, "ML"), []):
            clock = float(market.get("_observedAt") or timestamp(market.get("updatedAt")))
            if (not now - 86400 <= clock <= now or market.get("_kickoff") != match["kickoff"]
                    or market.get("_home") != match.get("providerHomeTeam")
                    or market.get("_away") != match.get("providerAwayTeam")):
                continue
            for quote in market.get("odds", []):
                for side in ("home", "draw", "away"):
                    try:
                        odd = float(quote[side])
                    except (TypeError, ValueError, KeyError):
                        continue
                    if math.isfinite(odd) and 1 < odd < 1000:
                        groups[clock][side].add(odd)
        if not groups:
            continue
        clock = max(groups)
        sides = groups[clock]
        if any(len(sides[side]) != 1 for side in ("home", "draw", "away")):
            continue
        prices = {side: next(iter(sides[side])) for side in ("home", "draw", "away")}
        if not .85 <= sum(1 / odd for odd in prices.values()) <= 1.25:
            continue
        return dict(prices, bookmaker=book, observedAt=iso(clock),
                    apiFixtureId=row["apiFixtureId"], kickoff=row["kickoff"])
    return None


def market_name(row):
    if row["metric"] not in ("shots", "sot"):
        return None
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
        players = []; seen = set()
        for player in values.get("unavailablePlayers", []):
            key = ("id", str(player["id"])) if isinstance(player, dict) and player.get("id") else (
                "name", normalized(player.get("name") if isinstance(player, dict) else player))
            if key[1] and key not in seen:
                players.append(player); seen.add(key)
        sides[side] = dict(injuriesCovered=bool(values.get("injuriesCovered")),
            lineupConfirmed=bool(values.get("lineupConfirmed")),
            unavailablePlayers=players, formation=values.get("formation"))
    return dict(asOf=iso(clock) if clock and clock <= now else None, fresh=fresh, teams=sides,
                usedByFrozenProbability=False, motivation="NOT_ESTABLISHED_FROM_VERIFIED_DATA")


class MarketHistory:
    """Read-only descriptive facts for each market, with the same 24h availability lag."""
    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda r:float(r['_date']))
        self.index = 0
        self.teams = defaultdict(lambda:deque(maxlen=80))

    def advance(self, cutoff):
        while self.index < len(self.rows) and float(self.rows[self.index]['_date']) + 86400 <= cutoff:
            row = self.rows[self.index]; self.index += 1
            self.teams[row['_home']].append((row, 0))
            self.teams[row['_away']].append((row, 1))

    def facts(self, issue, home, away, elo):
        from betting_v2.reliable_counts import metric_values
        metric = issue['metric'].upper()
        if metric not in ('GOALS_FT','GOALS_1H','GOALS_2H','CORNERS','SHOTS','SOT','FOULS'):
            metric = 'GOALS_FT'
        def average(values):
            values = [finite(v) for v in values if finite(v) is not None and v >= 0]
            return sum(values)/len(values) if values else None
        def team(root, index):
            all_rows = list(self.teams[root])
            usable = [(r, i, metric_values(r, metric)) for r,i in all_rows]
            usable = [(r,i,v) for r,i,v in usable if v is not None]
            recent = usable[-8:]; venue = [(r,i,v) for r,i,v in usable if i == index][-10:]
            form = []
            for r,i in all_rows:
                goals = metric_values(r, 'GOALS_FT')
                if goals is not None:
                    form.append('W' if goals[i] > goals[1-i] else 'D' if goals[i] == goals[1-i] else 'L')
            xg = [((r.get('normalized_stats') or {}).get('HxG' if i == 0 else 'AxG'),
                   (r.get('normalized_stats') or {}).get('AxG' if i == 0 else 'HxG')) for r,i,_ in recent]
            xg = [pair for pair in xg if all(finite(v) is not None and v >= 0 for v in pair)]
            # Counts at the issued line are historical observations, never a model hit rate.
            hits = []
            for r,i,values in recent:
                if issue['scope'] == 'BTTS':
                    positive = all(v > 0 for v in values)
                    hits.append(positive if issue['side'] == 'YES' else not positive)
                elif issue.get('line') is not None and issue['scope'] in ('TOTAL','TEAM'):
                    selected = issue.get('teamSide')
                    value = sum(values) if issue['scope'] == 'TOTAL' else values[i if selected == ('HOME' if index == 0 else 'AWAY') else 1-i]
                    if value != issue['line']:
                        hits.append(value > issue['line'] if issue['side'] == 'OVER' else value < issue['line'])
            return dict(team=issue['homeTeam'] if index == 0 else issue['awayTeam'], sample=len(recent),
                forAverage=average([v[i] for _,i,v in recent]), againstAverage=average([v[1-i] for _,i,v in recent]),
                venueSample=len(venue), venueFor=average([v[i] for _,i,v in venue]),
                venueAgainst=average([v[1-i] for _,i,v in venue]), formLast5=form[-5:],
                xgFor=average([v[0] for v in xg]), xgAgainst=average([v[1] for v in xg]),
                xgSample=len(xg),
                elo=float(elo[root]), restDays=(timestamp(issue['kickoff'])-float(all_rows[-1][0]['_date']))/86400 if all_rows else None,
                lineHits=sum(hits) if hits else None, lineSample=len(hits),
                lineBasis='MATCH' if issue['scope'] != 'TEAM' else 'FOR' if issue.get('teamSide') == ('HOME' if index == 0 else 'AWAY') else 'AGAINST')
        h2h = []
        for r,i in self.teams[home]:
            if {r['_home'], r['_away']} != {home, away}:
                continue
            goals = metric_values(r, 'GOALS_FT')
            if goals is not None:
                h2h.append(dict(date=iso(float(r['_date'])), homeGoals=goals[i], awayGoals=goals[1-i]))
        return dict(metric=metric, home=team(home, 0), away=team(away, 1), headToHead=h2h[-5:])


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
    history = History(actual); factual = MarketHistory(actual); result = {}
    for row in sorted(rows, key=lambda r:timestamp(r["featureCutoff"])):
        cutoff = timestamp(row["featureCutoff"]); history.advance(cutoff); factual.advance(cutoff)
        bound, reason = identity.bind(row["match"], row["match"])
        stats = None; market_history = None
        if bound:
            _, h, a, _ = bound; pair = history.pair(h, a, row["match"]["leagueCode"], timestamp(row["kickoff"]))
            stats = dict(home=team_facts(history, h, pair[0], row["homeTeam"]),
                         away=team_facts(history, a, pair[1], row["awayTeam"]),
                         eloGapWithHomeAdvantage=pair[0][1], binding=reason)
            market_history = factual.facts(row, h, a, history.elo)
        context = contexts.get((row["apiFixtureId"], row["kickoff"]))
        info = dict(schemaVersion=1, asOf=iso(now), predictionAsOf=row["featureCutoff"], changesPrediction=False,
                    historicalFacts=stats, market=market_facts(row, raw, now), context=context_facts(row, context, now),
                    marketHistory=market_history,
                    predictedCounts={k:row.get(k) for k in ("homeShots", "awayShots", "homeSot", "awaySot")},
                    explanationSemantics="FACTUAL_INPUTS_NOT_CAUSAL_FEATURE_ATTRIBUTIONS")
        result[row["selectionId"]] = dict(row, intelligence=info)
        result_context = result_odds_context(row, raw, now)
        if result_context is not None:
            result[row["selectionId"]]["match"] = dict(row["match"], contextOneXTwo=result_context)
    return [result[r["selectionId"]] for r in rows]
