"""Read all existing app sources and exact cached fixture identities; no provider calls."""
import gzip
import hashlib
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from .features import normalized

UEFA = (("champions_league", "CL"), ("europa_league", "EL"), ("conference_league", "UECL"))


def timestamp(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.


def read(path, default):
    return json.loads(path.read_text()) if path.exists() else default


def payload_sha(league):
    return hashlib.sha256(json.dumps(league.get("matches", []), sort_keys=True,
                                    separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def poll_observations(odds, archive, source_commit=None):
    """Prove refreshed whole leagues, excluding preserved/rotated old payloads.

    The existing collector replaces an entire nonempty fresh league. Its report
    counts and clock must agree with the archive. Unselected leagues get no clock.
    """
    debug = odds.get("debug", {})
    clock = archive.get("generatedAt")
    if not clock or clock != odds.get("generatedAt") or clock != debug.get("generatedAt"):
        return {}
    selected = set(debug.get("leaguesRequested", []))
    preserved = set(archive.get("preservedAfterEmptyRefresh", [])) | set(debug.get("preservedAfterEmptyRefresh", []))
    reports = {r["leagueCode"]: r for r in debug.get("leagueReports", [])}
    result = {}
    for league in archive.get("leagues", []):
        code = league["leagueCode"]; matches = league.get("matches", []); report = reports.get(code, {})
        payloads = sum(len(m.get("providerMarkets", [])) for m in matches)
        if (code not in selected or code in preserved or not matches or
                report.get("providerMatchesArchived") != len(matches) or
                report.get("providerMarketPayloadsArchived") != payloads):
            continue
        result[code] = dict(payloadSha256=payload_sha(league), observedAt=clock,
                            sourceCommit=source_commit, evidence="VERIFIED_PROVIDER_POLL_WINDOW")
    return result


def domestic_observations(odds, archive, previous, now):
    current = poll_observations(odds, archive)
    records = {}
    for league in archive.get("leagues", []):
        code = league["leagueCode"]; sha = payload_sha(league)
        choices = [r for r in (previous.get(code), current.get(code)) if r and
                   r.get("payloadSha256") == sha and now - 86400 <= timestamp(r.get("observedAt")) <= now]
        if choices:
            records[code] = max(choices, key=lambda r: timestamp(r["observedAt"]))
    return records


def apply_observations(raw, archive, observations):
    events = {str(m["id"]): observations[l["leagueCode"]]
              for l in archive.get("leagues", []) if l["leagueCode"] in observations
              for m in l.get("matches", [])}
    for (eid, _, _), markets in raw.items():
        if eid in events:
            proof = events[eid]
            for market in markets:
                market.update(_observedAt=timestamp(proof["observedAt"]), _evidence=proof["evidence"],
                              _receiptSha256=proof["payloadSha256"], _sourceCommit=proof.get("sourceCommit"))
    return raw


class Identities:
    def __init__(self, root, history):
        self.teams = {r[k] for r in history for k in ("_home", "_away")}
        self.names = defaultdict(set); self.country_names = defaultdict(set)
        for team in self.teams:
            country, name = team.split(":", 1)
            self.names[normalized(name)].add(team)
            self.country_names[country, normalized(name)].add(team)
        index = read(root / "data/statmaker/domestic_enriched/index.json", {})
        countries = {r["league_code"]: normalized(r["country"]) for r in index.get("leagues", [])}
        aliases = read(root / "mappings/domestic_team_aliases.json", {}).get("aliases", {})
        for code, groups in aliases.items():
            country = countries.get(code)
            if not country:
                continue
            for canonical, variants in groups.items():
                names = {normalized(canonical), *(normalized(v) for v in variants)}
                known = {t for n in names for t in self.country_names.get((country, n), ())}
                if len(known) == 1:
                    for n in names:
                        self.names[n].update(known); self.country_names[country, n].update(known)
        # UEFA feed names include provider prefixes/suffixes. Reuse explicit existing
        # country-scoped aliases, never fuzzy spelling or a global first-name match.
        for filename in ("uefa_support_history.json", "uefa_team_support_history.json"):
            for participant in read(root / "data/statmaker" / filename, {}).get("participants", []):
                country = normalized(participant.get("country"))
                variants = [participant.get("providerTeam"), participant.get("canonicalTeam"), *participant.get("aliases", [])]
                names = {normalized(v) for v in variants if v}
                known = {t for n in names for t in self.country_names.get((country, n), ())}
                if len(known) == 1:
                    for n in names:
                        self.names[n].update(known); self.country_names[country, n].update(known)
        alias_maps = list(read(root / "mappings/uefa_betting_team_aliases.json", {}).get("aliases", {}).values())
        for filename in ("uefa_club_competitions.json", "uefa_simulation_2026_27.json"):
            alias_maps.extend(c.get("aliases", {}) for c in read(root / "config" / filename, {}).get("competitions", []))
        for mapping in alias_maps:
            for alias, canonical in mapping.items():
                known = self.resolve([canonical])
                if known:
                    self.names[normalized(alias)].add(known)
        self.fixtures = defaultdict(dict)
        tables = [read(path, {}) for path in (root / "data/statmaker/league_tables").glob("*.json")]
        api_teams = defaultdict(set)
        for doc in tables:
            if doc.get("leagueCode") in ("CL", "EL", "UECL"):
                continue
            for row in doc.get("fixtures", []):
                for side in ("home", "away"):
                    team_id = row.get(side + "_team_id")
                    known = self.resolve([row.get(side + "_team")], doc.get("country"))
                    if team_id and known:
                        api_teams[int(team_id)].add(known)
        for doc in tables:
            for row in doc.get("fixtures", []):
                code = row.get("league_code") or doc.get("leagueCode")
                ko = timestamp(row.get("date_utc")); api = row.get("fixture_id")
                def team(side):
                    known = api_teams.get(int(row.get(side + "_team_id") or 0), set())
                    if len(known) == 1:
                        return next(iter(known))
                    return self.resolve([row.get(side + "_team")], doc.get("country") if code not in ("CL", "EL", "UECL") else None)
                h, a = team("home"), team("away")
                if h and a and ko and api:
                    self.fixtures[code, int(ko), h, a][int(api)] = row
        cache = read(root / "data/statmaker/domestic_match_context_cache.json", {})
        for row in cache.get("matchMappings", {}).values():
            api, code, ko = row.get("fixtureId"), row.get("leagueCode"), timestamp(row.get("kickoff"))
            h = self.resolve([row.get("homeTeam")], countries.get(code))
            a = self.resolve([row.get("awayTeam")], countries.get(code))
            if api and h and a and ko:
                self.fixtures[code, int(ko), h, a].setdefault(int(api), {})

    def candidates(self, values, country=None):
        choices = set()
        for value in values:
            if value:
                key = normalized(value)
                choices.update(self.country_names.get((normalized(country), key), ()) if country else self.names.get(key, ()))
        return choices

    def resolve(self, values, country=None):
        choices = self.candidates(values, country)
        return next(iter(choices)) if len(choices) == 1 else None

    def bind(self, league, match):
        code = league["leagueCode"]
        country = league["country"] if code not in ("CL", "EL", "UECL") else None
        hs, ass = [self.candidates([match.get("canonical" + s + "Team"), match.get(s.lower() + "Team"), match.get("provider" + s + "Team")], country) for s in ("Home", "Away")]
        ko = int(timestamp(match.get("kickoff")))
        compatible = [(key, rows) for key, rows in self.fixtures.items() if key[0] == code and key[1] == ko and key[2] in hs and key[3] in ass]
        if len(compatible) == 1:
            key, candidates = compatible[0]; h, a = key[2:]
        elif len(compatible) > 1:
            return None, "ambiguousApiFixtureId"
        else:
            h = next(iter(hs)) if len(hs) == 1 else None
            a = next(iter(ass)) if len(ass) == 1 else None
            candidates = {}
        if not h or not a or h == a:
            return None, "unknownTeam"
        supplied = (match.get("squadContext") or {}).get("apiFootballFixtureId")
        if len(candidates) > 1 or supplied and candidates and int(supplied) not in candidates:
            return None, "ambiguousApiFixtureId"
        api = int(supplied) if supplied else next(iter(candidates)) if len(candidates) == 1 else None
        if not api:
            return None, "missingApiFixtureId"
        if not candidates and not (match.get("usableForStats") and match.get("teamMappingStatus") == "matched"):
            return None, "unverifiedMapping"
        bound = dict(match, squadContext=dict(match.get("squadContext") or {}, apiFootballFixtureId=api),
                     teamMappingStatus="matched", usableForStats=True)
        proof = candidates.get(api) or {}
        for side in ("home", "away"):
            if proof.get(side + "_logo") and not bound.get(side + "TeamLogo"):
                bound[side + "TeamLogo"] = proof[side + "_logo"]
        return (bound, h, a, api), "cachedExactSchedule" if candidates else "existingMatchedContext"


def receipts(root, now):
    """Positive evidence of observation, independent of the market's last-change time."""
    from scripts.betting_v2.quote_receipt_capture import verify_receipt
    result = defaultdict(list); events = {}
    for path in sorted((root / "reports/betting_v2/quote_receipts").glob("*.json.gz")):
        # Filename is only an efficient bound; authenticated document time decides validity.
        if path.name[:8] < datetime.fromtimestamp(now - 86400).strftime("%Y%m%d"):
            continue
        raw = path.read_bytes()
        try:
            doc = json.loads(gzip.decompress(raw)); status = verify_receipt(doc, allow_partial=True)
            observed = timestamp(status["clientReceivedAtUTC"])
        except (ValueError, KeyError, TypeError, OSError):
            continue
        if not now - 86400 <= observed <= now:
            continue
        sha = hashlib.sha256(raw).hexdigest()
        for item in doc["boundedMarketSnapshot"]:
            event = item["event"]; eid = str(event.get("id") or event.get("eventId"))
            events[eid] = event
            for book in item.get("bookmakers", []):
                for market in book.get("markets", []):
                    name = market.get("name")
                    if name != "ML" and "Shots" not in str(name):
                        continue
                    result[eid, book["bookmaker"], name].append(dict(market, odds=market.get("outcomes", []),
                        _kickoff=event.get("date") or event.get("kickoff"), _home=event.get("home"), _away=event.get("away"),
                        _observedAt=observed, _receiptSha256=sha, _evidence="CLIENT_RESPONSE_CAPTURE"))
    return result


def latest_markets(raw, extra):
    """Replace an observed complete market, rather than merging old and new prices."""
    merged = defaultdict(list)
    for source in (raw, extra):
        for key, markets in source.items():
            merged[key].extend(markets)
    for key, markets in merged.items():
        latest = max(float(m.get("_observedAt") or timestamp(m.get("updatedAt"))) for m in markets)
        merged[key] = [m for m in markets if float(m.get("_observedAt") or timestamp(m.get("updatedAt"))) == latest]
    return merged


def uefa_packet(path, competition_id):
    raw = path.read_bytes(); doc = json.loads(raw); debug = doc.get("debug", {})
    current = set()
    if debug.get("oddsRefreshSkippedByRateLimit") is False and debug.get("preservedPreviousExactMarketRows") == 0:
        for call in debug.get("apiCalls", []):
            if call.get("status") == 200 and str(call.get("path", "")).startswith("/odds"):
                params = call.get("params") or {}
                current.update(str(params.get("eventIds") or params.get("eventId") or "").split(","))
    markets = defaultdict(list); observed = timestamp(doc.get("generatedAt"))
    for match in doc.get("matches", []):
        if str(match["id"]) not in current:
            continue
        by = defaultdict(list)
        for q in match.get("markets", []):
            if not q.get("exactBookmakerOdds"):
                continue
            market = q.get("providerMarketName"); book = q.get("bookmaker")
            if not market or not book:
                continue
            name = q.get("market"); selection = q.get("selection", "").lower()
            if name == "1X2":
                side = {"home": "home", "away": "away", "draw": "draw"}.get(selection)
            elif name in ("MATCH_SHOTS", "TEAM_SHOTS", "MATCH_SHOTS_ON_TARGET", "TEAM_SHOTS_ON_TARGET"):
                side = "over" if "over" in selection else "under" if "under" in selection else None
            else:
                continue
            if side:
                outcome = {side: str(q["odds"])}
                if q.get("line") is not None:
                    outcome["hdp"] = q["line"]
                by[book, market].append(outcome)
        for (book, name), odds in by.items():
            markets[str(match["id"]), book, name].append(dict(name=name, odds=odds, _observedAt=observed,
                _kickoff=match["kickoff"], _home=match.get("providerHomeTeam"), _away=match.get("providerAwayTeam"),
                _receiptSha256=hashlib.sha256(raw).hexdigest(), _evidence="FRESH_NORMALIZED_PROVIDER_SNAPSHOT"))
    league = {k: doc[k] for k in ("leagueCode", "country", "competition", "season", "matches")}
    league["competitionId"] = competition_id
    return league, markets, dict(path=path.name, sha256=hashlib.sha256(raw).hexdigest(), observedAt=doc.get("generatedAt"),
                                freshOddsEvents=len(current), totalFixtures=len(doc.get("matches", [])))
