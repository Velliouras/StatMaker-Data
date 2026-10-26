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
from .model import Bank, conservative, favourite_guard, POLICY_ID, MIN_WIN_PROBABILITY, eligible_quote
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
        def identity(r):
            api = r.get("fixture_id")
            return ("api", str(api)) if api else ("teams", r["_date"], r["_home"], r["_away"])
        merged = {identity(r): r for r in rows}
        for r in current:
            merged[identity(r)] = r
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
                    matches.append(float(market.get("_observedAt") or timestamp(market.get("updatedAt"))))
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
                                       providerMarketName=name, exactBookmakerOdds=True,
                                       providerMarketUpdatedAt=market.get("updatedAt"),
                                       quoteObservationEvidence=market.get("_evidence", "PROVIDER_LAST_CHANGE_LOWER_BOUND"),
                                       quoteObservationSha256=market.get("_receiptSha256"),
                                       quoteObservationCommit=market.get("_sourceCommit")))
    return result


def favourite(match, bookmaker, raw, now):
    result = {}
    for market in raw.get((str(match["id"]), bookmaker, "ML"), []):
        if (market.get("_kickoff") != match.get("kickoff") or market.get("_home") != match.get("providerHomeTeam")
                or market.get("_away") != match.get("providerAwayTeam")):
            continue
        if not now - 86400 <= float(market.get("_observedAt") or timestamp(market.get("updatedAt"))) <= now:
            continue
        for q in market.get("odds", []):
            for side in ("HOME", "AWAY", "DRAW"):
                if q.get(side.lower()) is not None:
                    result.setdefault(side, set()).add(float(q[side.lower()]))
    if set(result) != {"HOME", "AWAY", "DRAW"} or any(len(v) != 1 for v in result.values()):
        return None
    odds = {k: next(iter(v)) for k, v in result.items()}
    if not all(1 < odd < 1000 for odd in odds.values()) or not .85 <= sum(1 / odd for odd in odds.values()) <= 1.25:
        return None
    side = min(odds, key=odds.get)
    return side if side in ("HOME", "AWAY") and list(odds.values()).count(odds[side]) == 1 else None


def load_quote_sources(now):
    odds_path = ROOT / "odds/odds_api_io/domestic_odds.json"
    raw_path = ROOT / "odds/odds_api_io/domestic_provider_markets.json"
    odds_bytes, raw_bytes = odds_path.read_bytes(), raw_path.read_bytes()
    odds, raw_doc = json.loads(odds_bytes), json.loads(raw_bytes)
    from .sources import (UEFA, latest_markets, receipts, uefa_packet,
                          read, domestic_observations, apply_observations)
    observation_path = OUT / "observations.json"
    observations = domestic_observations(odds, raw_doc, read(observation_path, {}).get("leagues", {}), now)
    write(observation_path, dict(schemaVersion=1, leagues=observations))
    raw = latest_markets(apply_observations(raw_offer_index(raw_doc), raw_doc, observations), receipts(ROOT, now))
    leagues = [dict(l, competitionId="domestic", _sourceOddsSha256=digest(odds_bytes)) for l in odds["leagues"]]
    source_packets = []
    for competition_id, code in UEFA:
        source_root = ROOT / ".sources/uefa" if (ROOT / ".sources/uefa").exists() else ROOT
        path = source_root / f"odds/odds_api_io/{competition_id}_odds.json"
        if path.exists():
            league, markets, proof = uefa_packet(path, competition_id)
            league["_sourceOddsSha256"] = proof["sha256"]
            leagues.append(league); raw = latest_markets(raw, markets); source_packets.append(proof)
    from .primary_quotes import merge as merge_primary
    raw = merge_primary(raw, odds_path, now)
    for competition_id, _code in UEFA:
        source_root = ROOT / ".sources/uefa" if (ROOT / ".sources/uefa").exists() else ROOT
        raw = merge_primary(raw, source_root / f"odds/odds_api_io/{competition_id}_odds.json", now)
    return leagues, raw, dict(oddsGeneratedAt=odds["generatedAt"], oddsSha256=digest(odds_bytes),
        providerSha256=digest(raw_bytes), observedDomesticLeagues=len(observations), uefa=source_packets)


def new_candidates(history_rows, now, known):
    from .sources import Identities
    from zoneinfo import ZoneInfo
    leagues, raw, sources = load_quote_sources(now)
    audit = Counter(); identity = Identities(ROOT, history_rows)
    model = Bank(OUT / "model")
    if now < model.meta["calibrationBefore"]:
        raise ValueError("Model calibration is not prior")
    history = History(history_rows); history.advance(now)
    options = defaultdict(list); coverage_comp = defaultdict(Counter); coverage_day = defaultdict(Counter)
    for league in leagues:
        if not league.get("enabledForBetting", True):
            continue
        for original in league.get("matches", []):
            ko = timestamp(original.get("kickoff"))
            if not now + 900 < ko:
                continue
            comp = league.get("competitionId", "domestic")
            day = datetime.fromtimestamp(ko, ZoneInfo("Europe/Athens")).date().isoformat()
            counters = (coverage_comp[comp], coverage_day[day])
            for counter in counters: counter["fixtures"] += 1
            quotes = raw_quotes(original, raw)
            if not quotes:
                continue
            for counter in counters: counter["withShotsOdds"] += 1
            resolved, reason = identity.bind(league, original)
            if resolved is None:
                audit[reason] += 1
                for counter in counters: counter[reason] += 1
                continue
            m, h, a, api = resolved
            for counter in counters: counter["mapped"] += 1
            features = history.pair(h, a, league["leagueCode"], ko)
            if min(features[0][4:6]) < 8:
                audit["insufficientHistory"] += 1
                for counter in counters: counter["insufficientHistory"] += 1
                continue
            for counter in counters: counter["forecastable"] += 1
            replicas = model.predict(features)
            fixture = f"api:{api}:{ko:.0f}"
            for q in quotes:
                if q.get("market") not in MARKETS:
                    continue
                metric, scope = MARKETS[q["market"]]
                # A rescheduled kickoff must not issue a second pick for the same API fixture/metric.
                group = f"api:{api}:{metric}"
                if group in known:
                    continue
                audit["supportedQuotes"] += 1
                for counter in counters: counter["quotes"] += 1
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
                if not eligible_quote(p, push, float(q["odds"])):
                    audit["probabilityOrEv"] += 1; continue
                for counter in counters: counter["probabilityEvPassing"] += 1
                fav = favourite(m, q["bookmaker"], raw, now)
                if not favourite_guard(features, replicas, fav, metric, scope, team, side):
                    audit["favouriteGuard"] += 1; continue
                for counter in counters: counter["eligibleOptions"] += 1
                match = {k: v for k, v in m.items() if k != "markets"}
                match.update({k: league[k] for k in ("leagueCode", "country", "competition", "season")})
                sid = digest(json.dumps([MODEL, group, q["market"], team, side, line, q["bookmaker"], q["odds"]]).encode())
                options[group].append(dict(selectionId=sid, groupKey=group, fixtureKey=fixture, fixtureId=str(m["id"]), apiFixtureId=int(api),
                    competitionId=comp, homeTeam=m["homeTeam"], awayTeam=m["awayTeam"], kickoff=m["kickoff"],
                    match=match, market=q["market"], selection=q["selection"], team=q.get("team"), teamSide=team,
                    line=line, side=side, odd=float(q["odds"]), bookmaker=q["bookmaker"], winProbability=p,
                    pushProbability=push, indicativeEV=ev, model=MODEL, policyId=POLICY_ID, probabilityModel="CATBOOST_NB_BINOMIAL",
                    modelBankSha256=digest((OUT / "model/bank.json").read_bytes()), metric=metric, scope=scope,
                    featureCutoff=iso(now), quoteUpdatedAt=iso(updated), issuedAt=iso(now), publishedAt=None,
                    publicationCommit=None, outcome="PENDING", settlementReturn=None, settledAt=None,
                    sourceOddsSha256=league["_sourceOddsSha256"], sourceProviderSha256=sources["providerSha256"],
                    quoteObservationSemantics="OBSERVED_AT_OR_PROVIDER_LAST_CHANGE_LOWER_BOUND",
                    providerMarketUpdatedAt=q.get("providerMarketUpdatedAt"),
                    quoteObservationEvidence=q["quoteObservationEvidence"],
                    quoteObservationSha256=q.get("quoteObservationSha256"),
                    quoteObservationCommit=q.get("quoteObservationCommit"),
                    identityBinding=reason,
                    bookmakerBinding="EXACT_PROVIDER_QUOTE", certified=False, category="UAT_SHOTS_EXPERIMENT",
                    historyHome=int(features[0][4]), historyAway=int(features[0][5]),
                    homeShots=replicas[0]["shots"][0], awayShots=replicas[0]["shots"][1],
                    homeSot=replicas[0]["sot"][0], awaySot=replicas[0]["sot"][1]))
    # One issued selection per fixture/metric. Tie-breaks do not use actual outcomes.
    selected = [sorted(v, key=lambda r: (-r["winProbability"], -r["indicativeEV"], r["selectionId"]))[0] for v in options.values()]
    for row in selected:
        day = datetime.fromtimestamp(timestamp(row["kickoff"]), ZoneInfo("Europe/Athens")).date().isoformat()
        coverage_comp[row["competitionId"]]["newSelections"] += 1
        coverage_day[day]["newSelections"] += 1
    coverage = dict(byCompetition={k: dict(v) for k, v in coverage_comp.items()}, byDay={k: dict(v) for k, v in coverage_day.items()})
    return selected, dict(audit), dict(sources, coverage=coverage)


def observe_issued_quote(row, raw, now):
    """Refresh display evidence only, leaving the first-issued ledger immutable."""
    names = {"MATCH_SHOTS": "Total Shots", "MATCH_SHOTS_ON_TARGET": "Total Shots on Target",
             "TEAM_SHOTS": "Total Shots", "TEAM_SHOTS_ON_TARGET": "Total Shots on Target"}
    name = names[row["market"]]
    if row["scope"] == "TEAM":
        name += " Home" if row["teamSide"] == "HOME" else " Away"
    quote = dict(selection=row["selection"], line=row["line"], odds=row["odd"], bookmaker=row["bookmaker"],
                 providerMarketName=name, exactBookmakerOdds=True)
    binding = bind_quote(row["match"], quote, raw)
    observed = binding[1] if binding else 0.
    recent = now - 86400 <= observed <= now and observed >= timestamp(row["quoteUpdatedAt"])
    return dict(row, latestQuoteObservedAt=iso(observed) if recent else None)


def settle(row, actual, now):
    if row.get("publishedAt") is None or timestamp(row["publishedAt"]) >= timestamp(row["kickoff"]):
        return row
    if row["outcome"] != "PENDING" or now < timestamp(row["kickoff"]) + 90 * 60:
        return row
    from .results import exact_result
    result = exact_result(row, actual, ROOT, now)
    if result is None:
        return row
    stats = result.get("normalized_stats") or {}
    from .features import number
    keys = ("HS", "AS") if row["metric"] == "shots" else ("HST", "AST")
    values = [number(stats.get(k)) for k in keys]
    if any(v is None or not v.is_integer() for v in values):
        return row
    h, a = values
    for total, target in (("HS", "HST"), ("AS", "AST")):
        s, t = number(stats.get(total)), number(stats.get(target))
        if s is not None and t is not None and t > s:
            return row
    value = h + a if row["scope"] == "TOTAL" else h if row["teamSide"] == "HOME" else a
    push = value == row["line"]
    won = value > row["line"] if row["side"] == "OVER" else value < row["line"]
    return dict(row, outcome="VOID" if push else "WON" if won else "LOST",
                settlementReturn=1. if push else row["odd"] if won else 0., settledAt=iso(now),
                actualCount=value, settlementSource=result.get("_settlementSource", "CACHED_FT_API_FIXTURE_STATS"),
                settlementEvidenceGeneratedAt=iso(result["_evidenceAt"]) if result.get("_evidenceAt") else None)


def verify_publication(rows, ledger_path=None):
    relative = (ledger_path or OUT / "ledger.json").relative_to(ROOT).as_posix()
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
    paper_path = ROOT / "data/statmaker/uat/market_watch/paper_ledger.json"
    paper = json.loads(paper_path.read_text()) if paper_path.exists() else dict(schemaVersion=1, modelId=MODEL, rows=[])
    if ledger["modelId"] != MODEL:
        raise ValueError("Foreign model ledger")
    if args.stage_issues or args.preview:
        selected, audit, source = new_candidates(actual, now, {r["groupKey"] for r in ledger["rows"] + paper["rows"]})
        print("SHOTS_UAT_SELECTION", len(selected), json.dumps(audit), flush=True)
        if args.preview:
            write(OUT / "preview.json", dict(selected=selected, audit=audit, source=source)); return
        from scripts.market_watch.build import allowed
        paper["rows"] += selected
        ledger["rows"] += [r for r in selected if allowed(r["market"], now)]
        # A previously frozen paper prediction can become an app issue while the
        # match is still ahead and the same exact price is freshly observed.
        _, raw, _ = load_quote_sources(now)
        known = {r["groupKey"] for r in ledger["rows"]}
        for row in paper["rows"]:
            if row["groupKey"] in known or not allowed(row["market"], now) or timestamp(row["kickoff"]) <= now + 900:
                continue
            current = observe_issued_quote(row, raw, now)
            if current.get("latestQuoteObservedAt"):
                ledger["rows"].append(dict(row, issuedAt=iso(now), quoteUpdatedAt=current["latestQuoteObservedAt"],
                    publishedAt=None, publicationCommit=None, monitorPaperSelectionId=row["selectionId"]))
                known.add(row["groupKey"])
        write(paper_path, paper)
        if (ROOT / "config/market_watch.json").exists():
            from scripts.market_watch.router import stage as stage_markets
            watch_leagues, watch_raw, _ = load_quote_sources(now)
            stage_markets(actual, now, watch_leagues, watch_raw)
        ledger["audit"] = audit; ledger["source"] = source; ledger["policyId"] = POLICY_ID
        write(OUT / "coverage.json", dict(generatedAt=iso(now), audit=audit, source=source))
        write(ledger_path, ledger); return
    ledger["rows"] = [settle(r, actual, now) for r in verify_publication(ledger["rows"])]
    write(ledger_path, ledger)
    if paper_path.exists():
        paper["rows"] = [settle(r, actual, now) for r in verify_publication(paper["rows"], paper_path)]
        write(paper_path, paper)
    if (ROOT / "config/market_watch.json").exists():
        subprocess.run(["python", "-m", "scripts.market_watch.build"], cwd=ROOT, check=True)
    leagues, raw, _ = load_quote_sources(now)
    issued = [observe_issued_quote(r, raw, now) for r in ledger["rows"] if r.get("publishedAt")]
    from .intelligence import annotate
    market_issued, market_live = [], []
    if (ROOT / "config/market_watch.json").exists():
        from scripts.market_watch.router import publish as publish_markets
        market_issued, market_live = publish_markets(actual, now, raw)
    # Every issued market receives factual analysis. Candidate/issued copies must
    # remain identical, and the immutable numerical ledger is never rewritten.
    shot_count = len(issued)
    all_issued = annotate(issued + market_issued, actual, raw, leagues, now)
    issued, market_issued = all_issued[:shot_count], all_issued[shot_count:]
    live = current_recommendations(issued, now)
    live_ids = {r['selectionId'] for r in market_live}
    market_live = [r for r in market_issued if r['selectionId'] in live_ids]
    generation = digest(json.dumps([MODEL, ledger], sort_keys=True).encode())
    payload = dict(schemaVersion=1, status="UAT_SHOTS_MODEL", modelId=MODEL, generationId=generation,
                   generatedAt=iso(now), apiCalls=0, prodWrites=0, probabilitiesNotEmpiricallyCertified=True,
                   selectionPolicyId=POLICY_ID, minimumWinProbability=MIN_WIN_PROBABILITY,
                   tierSemantics="MODEL_PROBABILITY_BANDS_NOT_EMPIRICAL_CERTIFICATION",
                   candidateCount=len(live), candidates=live, issued=issued, audit=ledger.get("audit", {}), source=ledger.get("source", {}))
    payload.update(marketIssued=market_issued, marketCandidates=market_live, marketCandidateCount=len(market_live),
                   marketRoutingPolicy="MARKET_WATCH_UAT_V1")
    from .repricing import price_updates, validate_price_updates
    from scripts.market_watch.build import allowed
    from scripts.market_watch.quotes import current_quotes
    updates = price_updates(all_issued, raw, current_quotes(now), now, allowed, eligible_quote)
    payload.update(priceUpdates=updates, priceUpdateCount=len(updates))
    validate_price_updates(payload)
    from .daily import attach_daily
    attach_daily(payload, OUT / "p55", OUT / "daily_membership.json")
    publish_read_model(OUT / "p55", payload)
    # 0.1.66 and earlier require p>=.60 and must retain a valid independent feed.
    publish_read_model(OUT, compatible_p60_payload(payload))
    print("SHOTS_UAT_PUBLISHED", len(live), "future", len(issued), "issued", flush=True)


def compatible_p60_payload(payload):
    legacy = dict(payload, selectionPolicyId="SHOTS_SOT_SCOPE_GUARD_V2", minimumWinProbability=.60,
                  candidates=[r for r in payload["candidates"] if r["winProbability"] >= .60],
                  issued=[r for r in payload["issued"] if r["winProbability"] >= .60])
    legacy["candidateCount"] = len(legacy["candidates"])
    admitted = {r["selectionId"] for r in legacy["issued"] + legacy.get("marketIssued", [])}
    legacy["priceUpdates"] = [r for r in payload.get("priceUpdates", []) if r["selectionId"] in admitted]
    legacy["priceUpdateCount"] = len(legacy["priceUpdates"])
    if "dailySelectionIds" in payload:
        legacy["dailySelectionIds"] = [key for key in payload["dailySelectionIds"] if key in admitted]
        legacy["dailySelectionCount"] = len(legacy["dailySelectionIds"])
    return legacy


def current_recommendations(issued, now):
    # Admission changes apply to current recommendations; issued history remains
    # intact for Performance, including the previously published P55 cohort.
    from scripts.market_watch.build import allowed
    return [r for r in issued if timestamp(r["kickoff"]) > now and r.get("latestQuoteObservedAt") and allowed(r["market"], now)
            and eligible_quote(r["winProbability"], r["pushProbability"], r["odd"])]


def publish_read_model(directory, payload):
    directory.mkdir(parents=True, exist_ok=True)
    content = (json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()
    sha = digest(content); artifact = f"candidates-{sha}.json"
    (directory / artifact).write_bytes(content)
    write(directory / "latest.json", dict(schemaVersion=1, channel="uat", artifact=artifact, sha256=sha,
        generationId=payload["generationId"], generatedAt=payload["generatedAt"]))


if __name__ == "__main__":
    main()


