#!/usr/bin/env python3
"""UAT Betting V2: direct fixture->historical matchup model->priced forecast.

No legacy prepared_simulations, certification, holdout approval, selection
quotas, implied-probability features or calls to external APIs. Fitted
count likelihoods share a competitive club/opponent graph and real UEFA bridges.
This job adds an independent table: existing Match/League Simulation and PROD
recommendations retain their own tables and behaviour.
"""
from __future__ import annotations

import argparse
import bisect
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sqlite3
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from betting_v2.opponent_matchup import OpponentMatchup, load_competitive_history, normalized
from odds_api_io_market_audit import classify_provider_market

METRIC_COLUMNS = {
    "GOALS_FT": ("home_goals", "away_goals"),
    "GOALS_1H": ("hthg", "htag"),
    "GOALS_2H": ("home_goals", "away_goals"),
    "CORNERS": ("HC", "AC"),
    "SHOTS": ("HS", "AS"),
    "SOT": ("HST", "AST"),
    "YELLOW_CARDS": ("HY", "AY"),
    "CARDS": ("HY", "AY"),
    "FOULS": ("HF", "AF"),
}
MODEL = "betting-v2-opponent-adjusted-count-v2"
RUNS = 10_000
# Exact fixture/provider aliases used solely for cross-competition identity.
UEFA_ALIASES = {}


def name(value):
    return normalized(value)


def league_code(value):
    code = str(value or "").upper()
    return "UECL" if code == "CONF" else code


def kickoff(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def metric_values(row, metric):
    if metric == "GOALS_FT":
        raw = (row.get("home_goals"), row.get("away_goals"))
    elif metric == "GOALS_2H":
        try:
            raw = (int(row["home_goals"]) - int(row["hthg"]),
                   int(row["away_goals"]) - int(row["htag"]))
        except (ValueError, TypeError, KeyError):
            return None
    elif metric == "GOALS_1H":
        raw = (row.get("hthg"), row.get("htag"))
    elif metric == "CARDS":
        stats = row.get("normalized_stats") or {}
        try:
            raw = (float(stats["HY"]) + float(stats["HR"]),
                   float(stats["AY"]) + float(stats["AR"]))
        except (TypeError, KeyError, ValueError):
            return None
    else:
        keys = METRIC_COLUMNS[metric]
        stats = row.get("normalized_stats") or {}
        raw = (stats.get(keys[0]), stats.get(keys[1]))
    try:
        vals = tuple(float(v) for v in raw)
        if all(v >= 0 and math.isfinite(v) for v in vals):
            return vals
    except (TypeError, ValueError):
        pass
    return None


def load_actual_results(cutoff=None):
    cutoff = cutoff or datetime.now(timezone.utc).timestamp()
    history,aliases = load_competitive_history(ROOT,cutoff)
    UEFA_ALIASES.clear();UEFA_ALIASES.update(aliases)
    return {'GLOBAL':history}

class MatchupLeague(OpponentMatchup):
    def __init__(self,matches,cutoff):
        super().__init__(matches,cutoff,metric_values)

def poisson_cdf(rate):
    p = math.exp(-rate)
    cdf = [p]
    for i in range(1, 160):
        p *= rate / i
        cdf.append(min(1.0, cdf[-1] + p))
        if cdf[-1] > 1 - 1e-11 and i >= rate + 8 * math.sqrt(rate):
            break
    return cdf


def samples(home_rate, away_rate, seed):
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16], 16))
    h, a = poisson_cdf(home_rate), poisson_cdf(away_rate)
    return [(bisect.bisect_left(h, rng.random()), bisect.bisect_left(a, rng.random()))
            for _ in range(RUNS)]


def split_goal_periods(pairs,home_share,away_share,seed):
    """Allocate the same 10k FT goals to 1H/2H; no independent extra goals."""
    rng = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:16],16))
    first,second = [],[]
    for h,a in pairs:
        hh = sum(rng.random()<home_share for _ in range(h))
        ah = sum(rng.random()<away_share for _ in range(a))
        first.append((hh,ah));second.append((h-hh,a-ah))
    return first,second

def quote_market(row, home, away):
    home_aliases = {name(row[k]) for k in ("home_team", "canonical_home_team", "provider_home_team")}
    away_aliases = {name(row[k]) for k in ("away_team", "canonical_away_team", "provider_away_team")}
    raw = name(row["market"]).upper()
    # Map named bookmaker markets to a SINGLE actual count family.
    targets = {
        "1X2": ("GOALS_FT", "RESULT"),
        "FULLTIME1X2": ("GOALS_FT", "RESULT"),
        "MATCHRESULT": ("GOALS_FT", "RESULT"),
        "FULLTIMERESULT3WAY": ("GOALS_FT", "RESULT"),
        "MATCHRESULT3WAY": ("GOALS_FT", "RESULT"),
        "MATCHWINNER": ("GOALS_FT", "RESULT"),
        "FULLTIMERESULT": ("GOALS_FT", "RESULT"),
        "DOUBLECHANCE": ("GOALS_FT", "DC"),
        "DC": ("GOALS_FT", "DC"),
        "DRAWNOBET": ("GOALS_FT", "DNB"),
        "HALFTIME1X2": ("GOALS_1H", "RESULT"),
        "HALFTIMEDOUBLECHANCE": ("GOALS_1H", "DC"),
        "BTTS": ("GOALS_FT", "BTTS"),
        "MATCHGOALS": ("GOALS_FT", "TOTAL"),
        "TEAMGOALS": ("GOALS_FT", "TEAM"),
        "TEAMTOTALGOALS": ("GOALS_FT", "TEAM"),
        "FIRSTHALFGOALS": ("GOALS_1H", "TOTAL"),
        "MATCHFIRSTHALFGOALS": ("GOALS_1H", "TOTAL"),
        "TEAMFIRSTHALFGOALS": ("GOALS_1H", "TEAM"),
        "SECONDHALFGOALS": ("GOALS_2H", "TOTAL"),
        "MATCHSECONDHALFGOALS": ("GOALS_2H", "TOTAL"),
        "TEAMSECONDHALFGOALS": ("GOALS_2H", "TEAM"),
        "MATCHCORNERS": ("CORNERS", "TOTAL"),
        "TEAMCORNERS": ("CORNERS", "TEAM"),
        "MOSTCORNERS": ("CORNERS", "RESULT"),
        "MATCHSHOTS": ("SHOTS", "TOTAL"),
        "TEAMSHOTS": ("SHOTS", "TEAM"),
        "MOSTSHOTS": ("SHOTS", "RESULT"),
        "MATCHSHOTSONTARGET": ("SOT", "TOTAL"),
        "TEAMSHOTSONTARGET": ("SOT", "TEAM"),
        "MOSTSHOTSONTARGET": ("SOT", "RESULT"),
        "MATCHYELLOWCARDS": ("YELLOW_CARDS", "TOTAL"),
        "TEAMYELLOWCARDS": ("YELLOW_CARDS", "TEAM"),
        "MATCHCARDS": ("CARDS", "TOTAL"),
        "TEAMCARDS": ("CARDS", "TEAM"),
        "MATCHFOULS": ("FOULS", "TOTAL"),
        "TEAMFOULS": ("FOULS", "TEAM"),
    }
    # No Asian/handicap, no unknown settlement contract.
    found = targets.get(raw)
    if not found:
        return None
    metric, scope = found
    text = str(row["selection"] or "").strip().casefold()
    if scope in ("TOTAL", "TEAM"):
        # Real provider selections are frequently "Corners Over 9.5" or
        # "Shots on Target Under 4.5", not just "Over".
        tokens = re.findall(r"\b(over|under)\b", text, flags=re.IGNORECASE)
        if len(tokens) != 1 or row["line"] is None:
            return None
        side = tokens[0].upper()
        line = float(row["line"])
        if line < 0 or line * 2 != int(line * 2):
            return None
        team_side = ""
        if scope == "TEAM":
            team = name(row["team"])
            if not team or (team in home_aliases) == (team in away_aliases):
                return None
            team_side = "HOME" if team in home_aliases else "AWAY"
        return metric, scope, side, team_side, line
    side = {
        "1": "HOME", "home": "HOME", "x": "DRAW", "draw": "DRAW",
        "2": "AWAY", "away": "AWAY",
        "1x": "HOME_OR_DRAW", "x2": "AWAY_OR_DRAW",
        "home or draw": "HOME_OR_DRAW", "draw or away": "AWAY_OR_DRAW",
        "home or away": "HOME_OR_AWAY", "12": "HOME_OR_AWAY",
        "yes": "YES", "no": "NO"
    }.get(text, "")
    if not side:
        if name(text) in home_aliases and name(text) not in away_aliases:
            side = "HOME"
        elif name(text) in away_aliases and name(text) not in home_aliases:
            side = "AWAY"
    allowed = {
        "RESULT": {"HOME", "DRAW", "AWAY"},
        "DC": {"HOME_OR_DRAW", "AWAY_OR_DRAW", "HOME_OR_AWAY"},
        "DNB": {"HOME", "AWAY"},
        "BTTS": {"YES", "NO"},
    }[scope]
    return (metric, scope, side, "", None) if side in allowed else None


def evaluate(counts, spec):
    _, scope, side, team_side, line = spec
    if isinstance(counts, dict):
        total = counts["runs"]
        if scope == "RESULT":
            return counts[side] / total, 0.0
        if scope == "DC":
            wins = {"HOME_OR_DRAW": counts["HOME"] + counts["DRAW"],
                    "AWAY_OR_DRAW": counts["AWAY"] + counts["DRAW"],
                    "HOME_OR_AWAY": counts["HOME"] + counts["AWAY"]}[side]
            return wins / total, 0.0
        if scope == "DNB":
            return counts[side] / total, counts["DRAW"] / total
        if scope == "BTTS":
            yes = counts["BTTS"]
            return (yes if side == "YES" else total - yes) / total, 0.0
        values = counts[team_side + "_COUNTS" if team_side else "TOTAL"]
        below, through = bisect.bisect_left(values, line), bisect.bisect_right(values, line)
        wins = total - through if side == "OVER" else below
        return wins / total, (through - below) / total
    wins = pushes = 0
    for h, a in counts:
        if scope == "RESULT":
            win = side == ("HOME" if h > a else "AWAY" if a > h else "DRAW")
            push = False
        elif scope == "DC":
            win = (side == "HOME_OR_DRAW" and h >= a or
                   side == "AWAY_OR_DRAW" and a >= h or
                   side == "HOME_OR_AWAY" and h != a)
            push = False
        elif scope == "DNB":
            push = h == a
            win = h > a if side == "HOME" else a > h
        elif scope == "BTTS":
            push = False
            win = (h > 0 and a > 0) == (side == "YES")
        else:
            value = h if team_side == "HOME" else a if team_side == "AWAY" else h + a
            push = value == line
            win = value > line if side == "OVER" else value < line
        wins += bool(win and not push)
        pushes += bool(push)
    return wins / len(counts), pushes / len(counts)


def sampled_distribution(pairs):
    """Index the SAME 10,000 sampled pairs once for every actual market line."""
    return {
        "runs": len(pairs),
        "HOME": sum(h > a for h, a in pairs),
        "DRAW": sum(h == a for h, a in pairs),
        "AWAY": sum(a > h for h, a in pairs),
        "BTTS": sum(h > 0 and a > 0 for h, a in pairs),
        "HOME_COUNTS": sorted(h for h, _ in pairs),
        "AWAY_COUNTS": sorted(a for _, a in pairs),
        "TOTAL": sorted(h + a for h, a in pairs),
    }


def stage_betting_v2_quotes(con,db_path,uefa_exact_feed_dir):
    """Independent period/source/price bindings; leave legacy tables intact."""
    directory = Path(db_path).resolve().parent.parent/'files/app_ready_odds'
    con.executescript("""
        DROP TABLE IF EXISTS prepared_betting_v2_quotes;
        CREATE TABLE prepared_betting_v2_quotes (
            competition_id TEXT NOT NULL,snapshot_version TEXT NOT NULL,match_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL,market TEXT NOT NULL,selection TEXT NOT NULL,
            team TEXT,line REAL,odd REAL NOT NULL,bookmaker TEXT NOT NULL,
            provider_market_name TEXT,provider_market_family TEXT,
            PRIMARY KEY(competition_id,snapshot_version,match_key,ordinal));
        CREATE INDEX idx_betting_v2_quotes_scope ON prepared_betting_v2_quotes
            (competition_id,snapshot_version,match_key);
        DROP TABLE IF EXISTS prepared_betting_v2_feed_meta;
        CREATE TABLE prepared_betting_v2_feed_meta (
            competition_id TEXT PRIMARY KEY,generated_at TEXT,source_sha256 TEXT NOT NULL,staged_quotes INTEGER NOT NULL);
    """)
    totals = {}
    for competition in ('domestic','champions_league','europa_league','conference_league'):
        path = directory/'domestic.json' if competition=='domestic' else Path(uefa_exact_feed_dir)/(competition+'_odds.json')
        if not path.is_file():
            raise ValueError(f'Exact current V2 odds input missing: {path}')
        payload = json.loads(path.read_text())
        matches = list(payload.get('matches',[]))
        matches.extend(m for g in payload.get('leagues',[]) for m in g.get('matches',[]))
        stored = defaultdict(list)
        for f in con.execute('SELECT * FROM prepared_fixture_matches WHERE competition_id=?',(competition,)):
            if f['id']:
                stored[str(f['id'])].append(dict(f))
        pending = []
        for match in matches:
            for f in stored.get(str(match.get('id') or ''),[]):
                if kickoff(match.get('kickoff'))!=kickoff(f['kickoff']):
                    continue
                offers = {}
                for q in match.get('markets',[]):
                    book = str(q.get('bookmaker') or '').strip()
                    if q.get('exactBookmakerOdds') is not True or not book:
                        continue
                    original = str(q.get('providerMarketName') or '').strip()
                    if competition!='domestic' and not original:
                        continue
                    if original:
                        audit = classify_provider_market(original)
                        if audit['status']!='supported':
                            continue
                        family = q.get('providerMarketFamily')
                        if family and family!=audit['family']:
                            continue
                        periods = {'HALF_TIME_1X2','HALF_TIME_DOUBLE_CHANCE','FIRST_HALF_GOALS',
                            'TEAM_FIRST_HALF_GOALS','SECOND_HALF_GOALS','TEAM_SECOND_HALF_GOALS'}
                        if audit['family'] in periods and q.get('market')!=audit['family']:
                            continue
                    try:
                        odd = float(q.get('odds',q.get('odd')))
                        line = None if q.get('line') is None else float(q['line'])
                    except (ValueError,TypeError):
                        continue
                    if not math.isfinite(odd) or odd<=1 or line is not None and not math.isfinite(line):
                        continue
                    market,selection = str(q.get('market') or '').strip(),str(q.get('selection') or '').strip()
                    team = str(q.get('team') or '').strip() or None
                    offer = dict(f,market=market,selection=selection,team=team,line=line,odd=odd)
                    if quote_market(offer,'','') is None:
                        continue
                    identity = market,selection,team or '',line
                    value = odd,book,original,str(q.get('providerMarketFamily') or '')
                    previous = offers.get(identity)
                    # Select a real named bookmaker's price; persist that source.
                    if previous is None or (odd,book)>(previous[0],previous[1]):
                        offers[identity]=value
                for ordinal,(identity,source) in enumerate(sorted(offers.items(),key=lambda v:repr(v[0]))):
                    market,selection,team,line = identity
                    odd,book,original,family = source
                    pending.append((competition,f['snapshot_version'],f['match_key'],ordinal,
                        market,selection,team or None,line,odd,book,original or None,family or None))
        con.executemany('INSERT INTO prepared_betting_v2_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',pending)
        totals[competition] = len(pending)
        con.execute('INSERT INTO prepared_betting_v2_feed_meta VALUES (?,?,?,?)',
            (competition,payload.get('generatedAt'),hashlib.sha256(path.read_bytes()).hexdigest(),len(pending)))
    return totals


def run(db_path,uefa_exact_feed_dir,as_of=None):
    now = as_of or datetime.now(timezone.utc).timestamp()
    history = load_actual_results(now)
    counts = defaultdict(int)
    with sqlite3.connect(db_path) as con:
        con.row_factory = sqlite3.Row
        restored = stage_betting_v2_quotes(con,db_path,uefa_exact_feed_dir)
        print("BETTING_V2_EXACT_QUOTES_STAGED", json.dumps(restored, sort_keys=True), flush=True)
        con.executescript("""
            DROP TABLE IF EXISTS prepared_betting_v2_forecasts;
            CREATE TABLE prepared_betting_v2_forecasts (
                competition_id TEXT NOT NULL,
                snapshot_version TEXT NOT NULL,
                match_key TEXT NOT NULL,
                market_ordinal INTEGER NOT NULL,
                market TEXT NOT NULL, selection TEXT NOT NULL,
                team TEXT, line REAL, odd REAL NOT NULL,
                win_probability REAL NOT NULL, push_probability REAL NOT NULL,
                prediction_model TEXT NOT NULL, metric_key TEXT NOT NULL,
                actual_runs INTEGER NOT NULL,
                home_history_sample INTEGER NOT NULL,
                away_history_sample INTEGER NOT NULL,
                league_training_sample INTEGER NOT NULL,
                prediction_home_mean REAL NOT NULL,
                prediction_away_mean REAL NOT NULL,
                PRIMARY KEY (competition_id,snapshot_version,match_key,market_ordinal)
            );
            CREATE INDEX idx_prepared_betting_v2_scope
              ON prepared_betting_v2_forecasts(competition_id,snapshot_version,match_key);
        """)
        matches = con.execute("""
            SELECT f.competition_id,f.snapshot_version,f.match_key,
                   f.league_code,f.date,f.kickoff,f.home_team,f.away_team,
                   f.canonical_home_team,f.canonical_away_team,
                   f.provider_home_team,f.provider_away_team,
                   q.ordinal,q.market,q.selection,q.team,q.line,q.odd
            FROM prepared_fixture_matches f
            JOIN prepared_betting_v2_quotes q ON q.competition_id=f.competition_id
              AND q.snapshot_version=f.snapshot_version AND q.match_key=f.match_key
            WHERE f.usable_for_stats=1
               OR (f.competition_id IN
                      ('champions_league','europa_league','conference_league')
                   AND f.team_mapping_status='provider_identity'
                   AND TRIM(f.id)<>'' AND TRIM(f.home_team)<>''
                   AND TRIM(f.away_team)<>'')
            ORDER BY f.competition_id,f.snapshot_version,f.match_key,q.ordinal
        """).fetchall()
        models = {}
        draws = {}
        predictions = {}
        pending = []
        for row in matches:
            time = kickoff(row["kickoff"]) or kickoff(row["date"])
            # Fixture dates can be date-only; Android applies the exact
            # local kickoff cutoff at query time. Include today's quotes.
            if time < now - 86400:
                continue
            league = league_code(row["league_code"])
            # All quoted fixtures are future relative to this publication.
            # Train once per league using only results already completed now.
            model_key = "GLOBAL"
            if model_key not in models:
                models[model_key] = MatchupLeague(history["GLOBAL"], now)
            m = models[model_key]
            # Prefer an exact observed team alias in historical fixtures.
            # This is identity matching, never a fallback model prediction.
            def history_name(keys):
                choices = [name(row[k]) for k in keys]
                aliases = UEFA_ALIASES.get(league, {})
                choices = [aliases.get(v, v) for v in choices]
                return next((v for v in choices if v in m.history),
                            next((v for v in choices if v), ""))
            h = history_name(("canonical_home_team", "home_team", "provider_home_team"))
            a = history_name(("canonical_away_team", "away_team", "provider_away_team"))
            spec = quote_market(row, h, a)
            if not spec:
                counts["unsupported"] += 1
                continue
            metric = spec[0]
            key = (row["competition_id"], row["snapshot_version"], row["match_key"], metric)
            prediction_key = (h,a,metric)
            if prediction_key not in predictions:
                predictions[prediction_key] = m.predict(h,a,metric)
            prediction = predictions[prediction_key]
            if prediction is None:
                counts["no_actual_history_or_fitted_model"] += 1
                continue
            hr, ar, hs, ass, training = prediction
            if metric in ('GOALS_FT','GOALS_1H','GOALS_2H'):
                full_key = (*key[:3],'GOALS_FT')
                if full_key not in draws:
                    full = m.predict(h,a,'GOALS_FT')
                    if full is None:
                        continue
                    seed = "|".join(map(str,full_key))+"|"+MODEL
                    pairs = samples(full[0],full[1],seed)
                    draws[full_key] = sampled_distribution(pairs)
                    half = m.predict(h,a,'GOALS_1H')
                    if half is not None:
                        first,second = split_goal_periods(pairs,half[0]/full[0],half[1]/full[1],seed+'|periods')
                        draws[(*key[:3],'GOALS_1H')] = sampled_distribution(first)
                        draws[(*key[:3],'GOALS_2H')] = sampled_distribution(second)
                if key not in draws:
                    continue
            elif key not in draws:
                draws[key] = sampled_distribution(samples(hr,ar,"|".join(map(str,key))+"|"+MODEL))
            win, push = evaluate(draws[key], spec)
            if not (math.isfinite(win) and math.isfinite(push) and win + push <= 1.000001):
                continue
            pending.append((
                row["competition_id"], row["snapshot_version"], row["match_key"], row["ordinal"],
                row["market"], row["selection"], row["team"], row["line"], row["odd"],
                win, push, MODEL, metric, RUNS, hs, ass, training,hr,ar
            ))
        con.executemany("""
            INSERT INTO prepared_betting_v2_forecasts VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, pending)
        con.commit()
        counts["fixtures_from_quotes"] = len({(r[0], r[1], r[2]) for r in pending})
        counts["forecasts"] = len(pending)
        counts["staged_exact_quotes"] = sum(restored.values())
    return dict(counts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("prepared_db", type=Path)
    parser.add_argument("--uefa-exact-feed-dir",required=True,type=Path)
    parser.add_argument("--as-of",type=float)
    args = parser.parse_args()
    print("BETTING_V2_DIRECT_MODEL",json.dumps(run(args.prepared_db,args.uefa_exact_feed_dir,args.as_of),sort_keys=True))

