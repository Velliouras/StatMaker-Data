"""UAT-only issues and settlement from cached exact bookmaker offers.

Two-phase publication: commit issues first, then expose only rows whose commit
was public before kickoff. No historical replay enters the prospective ledger.
"""
import argparse
import gzip
import hashlib
import json
import re
import subprocess
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from .features import History, counts, normalized
from .model import Bank, conservative, favourite_guard
from .train import ROOT, HISTORY

OUT = ROOT / "data/statmaker/uat/shots_v1"
MODEL = "SHOTS_SOT_CATBOOST_V1"
MARKETS = {"MATCH_SHOTS": ("shots", "TOTAL"), "TEAM_SHOTS": ("shots", "TEAM"),
           "MATCH_SHOTS_ON_TARGET": ("sot", "TOTAL"), "TEAM_SHOTS_ON_TARGET": ("sot", "TEAM")}


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n")


def load_history(fresh=True):
    rows = json.loads(gzip.decompress(HISTORY.read_bytes()))
    if fresh:
        # Existing repository cache loader only: never calls a provider.
        from scripts.betting_v2.opponent_matchup import load_competitive_history
        current, _ = load_competitive_history(ROOT, datetime.now(timezone.utc).timestamp())
        merged = {(r["_date"], r["_home"], r["_away"]): r for r in rows}
        for r in current:
            merged[r["_date"], r["_home"], r["_away"]] = r
        rows = list(merged.values())
    return rows


def raw_offer_index(doc):
    out = defaultdict(list)
    for league in doc["leagues"]:
        for match in league.get("matches", []):
            for source in match.get("providerMarkets", []):
                if not source.get("exactProviderPayload"):
                    continue
                market = dict(source.get("market") or {}, _kickoff=match.get("kickoff"),
                              _home=match.get("providerHomeTeam"), _away=match.get("providerAwayTeam"))
                out[str(match["id"]), source["bookmaker"], source["providerMarket"]].append(market)
    return out


def bind_quote(match, quote, raw):
    """Exact event/book/provider-market/line/side/price, rejecting conflicts."""
    side = "over" if re.search(r"\bOver\b", quote["selection"], re.I) else "under" if re.search(r"\bUnder\b", quote["selection"], re.I) else None
    if side is None or quote.get("line") is None or not quote.get("exactBookmakerOdds"):
        return None
    choices = raw.get((str(match["id"]), quote["bookmaker"], quote.get("providerMarketName")), [])
    matches = []
    prices = set()
    for market in choices:
        if (market.get("_kickoff") != match.get("kickoff") or
                market.get("_home") != match.get("providerHomeTeam") or market.get("_away") != match.get("providerAwayTeam")):
            continue
        for odd in market.get("odds", []):
            try:
                if float(odd.get("hdp")) != float(quote["line"]) or side not in odd:
                    continue
                price = float(odd[side]); prices.add(price)
                if price == float(quote["odds"]):
                    matches.append(timestamp(market.get("updatedAt")))
            except (ValueError, TypeError):
                continue
    if len(prices) != 1 or not matches or len(set(matches)) != 1 or matches[0] <= 0:
        return None
    return side.upper(), matches[0]


def raw_quotes(match, raw):
    """All exact bookmaker lines, including fresh quotes hidden by best-price merging."""
    names = {"Total Shots": ("MATCH_SHOTS", None), "Total Shots Home": ("TEAM_SHOTS", "homeTeam"),
             "Total Shots Away": ("TEAM_SHOTS", "awayTeam"), "Total Shots on Target": ("MATCH_SHOTS_ON_TARGET", None),
             "Total Shots on Target Home": ("TEAM_SHOTS_ON_TARGET", "homeTeam"),
             "Total Shots on Target Away": ("TEAM_SHOTS_ON_TARGET", "awayTeam")}
    result = []
    for (event, book, name), markets in raw.items():
        if event != str(match["id"]) or name not in names:
            continue
        family, side = names[name]
        for market in markets:
            for item in market.get("odds", []):
                for direction in ("over", "under"):
                    if direction not in item or item.get("hdp") is None:
                        continue
                    try:
                        odd, line = float(item[direction]), float(item["hdp"])
                    except (TypeError, ValueError):
                        continue
                    team = match[side] if side else None
                    result.append(dict(market=family, selection=f"{team or name} {direction.title()} {line:g}",
                                       team=team, line=line, odds=odd, bookmaker=book,
                                       providerMarketName=name, exactBookmakerOdds=True))
    return result


def favourite(match, bookmaker, raw):
    result = {}
    for market in raw.get((str(match["id"]), bookmaker, "ML"), []):
        if (market.get("_kickoff") != match.get("kickoff") or market.get("_home") != match.get("providerHomeTeam")
                or market.get("_away") != match.get("providerAwayTeam")):
            continue
        for q in market.get("odds", []):
            for side in ("HOME", "AWAY", "DRAW"):
                if q.get(side.lower()) is not None:
                    result.setdefault(side, set()).add(float(q[side.lower()]))
    if set(result) != {"HOME", "AWAY", "DRAW"} or any(len(v) != 1 for v in result.values()):
        return None
    odds = {k: next(iter(v)) for k, v in result.items()}
    side = min(odds, key=odds.get)
    return side if side in ("HOME", "AWAY") and list(odds.values()).count(odds[side]) == 1 else None


def new_candidates(history_rows, now, known):
    odds_path = ROOT / "odds/odds_api_io/domestic_odds.json"
    raw_path = ROOT / "odds/odds_api_io/domestic_provider_markets.json"
    odds_bytes, raw_bytes = odds_path.read_bytes(), raw_path.read_bytes()
    odds, raw_doc = json.loads(odds_bytes), json.loads(raw_bytes)
    raw = raw_offer_index(raw_doc); audit = Counter()
    model = Bank(OUT / "model")
    if now < model.meta["calibrationBefore"]:
        raise ValueError("Model calibration is not prior")
    history = History(history_rows); history.advance(now)
    teams = set(history.teams); options = defaultdict(list)
    for league in odds["leagues"]:
        if not league.get("enabledForBetting", True):
            continue
        for m in league.get("matches", []):
            ko = timestamp(m.get("kickoff"))
            if not now + 900 < ko < now + 7 * 86400 or not m.get("usableForStats") or m.get("teamMappingStatus") != "matched":
                continue
            country = normalized(league["country"])
            h, a = [country + ":" + normalized(m.get("canonical" + s + "Team") or m.get(s.lower() + "Team")) for s in ("Home", "Away")]
            if h not in teams or a not in teams:
                audit["unknownTeam"] += 1; continue
            features = history.pair(h, a, league["leagueCode"], ko)
            if min(features[0][4:6]) < 8:
                audit["insufficientHistory"] += 1; continue
            replicas = model.predict(features)
            quotes = raw_quotes(m, raw)
            if not quotes:
                continue
            api = (m.get("squadContext") or {}).get("apiFootballFixtureId")
            # API ID supports unambiguous automated result grading; provider IDs stay distinct.
            if not api:
                audit["missingApiFixtureId"] += 1; continue
            fixture = f"api:{api}:{ko:.0f}"
            for q in quotes:
                if q.get("market") not in MARKETS:
                    continue
                metric, scope = MARKETS[q["market"]]
                group = fixture + ":" + metric
                if group in known:
                    continue
                audit["supportedQuotes"] += 1
                if not 1.8 < float(q["odds"]) < 3:
                    audit["oddsBounds"] += 1; continue
                binding = bind_quote(m, q, raw)
                if not binding:
                    audit["unboundQuote"] += 1; continue
                side, updated = binding
                if not now - 86400 <= updated <= now:
                    audit["staleQuote"] += 1; continue
                team = "HOME" if normalized(q.get("team")) == normalized(m["homeTeam"]) else "AWAY" if normalized(q.get("team")) == normalized(m["awayTeam"]) else ""
                if scope == "TEAM" and not team:
                    audit["unboundTeam"] += 1; continue
                line = float(q["line"])
                if line < 0 or (line * 2) % 1:
                    continue
                p, push = conservative(replicas, metric, scope, team, side, line)
                ev = p * float(q["odds"]) + push - 1
                if p < .6 or ev <= 0:
                    audit["probabilityOrEv"] += 1; continue
                fav = favourite(m, q["bookmaker"], raw)
                if not favourite_guard(features, replicas, fav, metric, scope, team, side):
                    audit["favouriteGuard"] += 1; continue
                match = {k: v for k, v in m.items() if k != "markets"}
                match.update({k: league[k] for k in ("leagueCode", "country", "competition", "season")})
                sid = digest(json.dumps([MODEL, group, q["market"], team, side, line, q["bookmaker"], q["odds"]]).encode())
                options[group].append(dict(selectionId=sid, groupKey=group, fixtureKey=fixture, fixtureId=str(m["id"]), apiFixtureId=int(api),
                    competitionId="domestic", homeTeam=m["homeTeam"], awayTeam=m["awayTeam"], kickoff=m["kickoff"],
                    match=match, market=q["market"], selection=q["selection"], team=q.get("team"), teamSide=team,
                    line=line, side=side, odd=float(q["odds"]), bookmaker=q["bookmaker"], winProbability=p,
                    pushProbability=push, indicativeEV=ev, model=MODEL, probabilityModel="CATBOOST_NB_BINOMIAL",
                    modelBankSha256=digest((OUT / "model/bank.json").read_bytes()), metric=metric, scope=scope,
                    featureCutoff=iso(now), quoteUpdatedAt=iso(updated), issuedAt=iso(now), publishedAt=None,
                    publicationCommit=None, outcome="PENDING", settlementReturn=None, settledAt=None,
                    sourceOddsSha256=digest(odds_bytes), sourceProviderSha256=digest(raw_bytes),
                    bookmakerBinding="EXACT_PROVIDER_QUOTE", certified=False, category="UAT_SHOTS_EXPERIMENT",
                    historyHome=int(features[0][4]), historyAway=int(features[0][5]),
                    homeShots=replicas[0]["shots"][0], awayShots=replicas[0]["shots"][1],
                    homeSot=replicas[0]["sot"][0], awaySot=replicas[0]["sot"][1]))
    # One issued selection per fixture/metric. Tie-breaks do not use actual outcomes.
    selected = [sorted(v, key=lambda r: (-r["winProbability"], -r["indicativeEV"], r["selectionId"]))[0] for v in options.values()]
    return selected, dict(audit), dict(oddsGeneratedAt=odds["generatedAt"], oddsSha256=digest(odds_bytes), providerSha256=digest(raw_bytes))


def settle(row, actual, now):
    if row.get("publishedAt") is None or timestamp(row["publishedAt"]) >= timestamp(row["kickoff"]):
        return row
    if row["outcome"] != "PENDING" or now < timestamp(row["kickoff"]) + 3 * 3600:
        return row
    choices = [r for r in actual if str(r.get("fixture_id")) == str(row["apiFixtureId"]) and abs(float(r["_date"]) - timestamp(row["kickoff"])) <= 300]
    if len(choices) != 1:
        return row
    stats = choices[0].get("normalized_stats") or {}
    from .features import number
    keys = ("HS", "AS") if row["metric"] == "shots" else ("HST", "AST")
    values = [number(stats.get(k)) for k in keys]
    if any(v is None or not v.is_integer() for v in values):
        return row
    h, a = values
    value = h + a if row["scope"] == "TOTAL" else h if row["teamSide"] == "HOME" else a
    push = value == row["line"]
    won = value > row["line"] if row["side"] == "OVER" else value < row["line"]
    return dict(row, outcome="VOID" if push else "WON" if won else "LOST",
                settlementReturn=1. if push else row["odd"] if won else 0., settledAt=iso(now),
                actualCount=value, settlementSource="CACHED_FT_API_FIXTURE_STATS")


def verify_publication(rows):
    relative = (OUT / "ledger.json").relative_to(ROOT).as_posix()
    # Prove presence in an already pushed commit; workflow invokes this only after push.
    sha, committed_at = subprocess.check_output(["git", "log", "-1", "--format=%H %cI", "--", relative], cwd=ROOT, text=True).strip().split(" ")
    committed = json.loads(subprocess.check_output(["git", "show", f"{sha}:{relative}"], cwd=ROOT, text=True))
    known = {r["selectionId"]: r for r in committed["rows"]}
    verified = []
    for row in rows:
        if row.get("publishedAt") is None:
            source = known.get(row["selectionId"])
            if source is None or source != row:
                raise ValueError("Issue differs from public committed row")
            if timestamp(committed_at) >= timestamp(row["kickoff"]):
                row = dict(row, publicationLate=True)
            else:
                row = dict(row, publishedAt=committed_at, publicationCommit=sha)
        verified.append(row)
    return verified


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--stage-issues", action="store_true")
    parser.add_argument("--publish", action="store_true"); parser.add_argument("--preview", action="store_true")
    parser.add_argument("--cached-history-only", action="store_true")
    args = parser.parse_args()
    if sum((args.stage_issues, args.publish, args.preview)) != 1:
        parser.error("Choose one publication phase")
    now = datetime.now(timezone.utc).timestamp(); actual = load_history(not args.cached_history_only)
    ledger_path = OUT / "ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else dict(schemaVersion=1, modelId=MODEL, rows=[])
    if ledger["modelId"] != MODEL:
        raise ValueError("Foreign model ledger")
    if args.stage_issues or args.preview:
        selected, audit, source = new_candidates(actual, now, {r["groupKey"] for r in ledger["rows"]})
        print("SHOTS_UAT_SELECTION", len(selected), json.dumps(audit), flush=True)
        if args.preview:
            write(OUT / "preview.json", dict(selected=selected, audit=audit, source=source)); return
        ledger["rows"] += selected
        ledger["audit"] = audit; ledger["source"] = source
        write(ledger_path, ledger); return
    ledger["rows"] = [settle(r, actual, now) for r in verify_publication(ledger["rows"])]
    write(ledger_path, ledger)
    issued = [r for r in ledger["rows"] if r.get("publishedAt")]
    live = [r for r in issued if timestamp(r["kickoff"]) > now and now - timestamp(r["quoteUpdatedAt"]) <= 86400]
    generation = digest(json.dumps([MODEL, ledger], sort_keys=True).encode())
    payload = dict(schemaVersion=1, status="UAT_SHOTS_MODEL", modelId=MODEL, generationId=generation,
                   generatedAt=iso(now), apiCalls=0, prodWrites=0, probabilitiesNotEmpiricallyCertified=True,
                   candidateCount=len(live), candidates=live, issued=issued, audit=ledger.get("audit", {}), source=ledger.get("source", {}))
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    sha = digest(content); artifact = f"candidates-{sha}.json"
    (OUT / artifact).write_bytes(content)
    write(OUT / "latest.json", dict(schemaVersion=1, channel="uat", artifact=artifact, sha256=sha,
                                  generationId=generation, generatedAt=payload["generatedAt"]))
    print("SHOTS_UAT_PUBLISHED", len(live), "future", len(issued), "issued", flush=True)


if __name__ == "__main__":
    main()
