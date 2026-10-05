#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import random
from bisect import bisect_left
from typing import Any, Callable

SPECIAL_CODES = {"ARG", "COL", "MEX", "PER", "USA", "URU"}

# 2026 competition boundaries used only for the current tournament phase.
ARG_CLAUSURA_START = "2026-07-23"
COL_II_START = "2026-07-24"
PER_CLAUSURA_START = "2026-07-17"
URU_CLAUSURA_START = "2026-08-07"
URU_INTERMEDIO_FINAL = "2026-08-05"

ARG_ZONE_A = (
    ("Platense",),
    ("Defensa Y Justicia", "Defensa y Justicia"),
    ("Central Cordoba de Santiago", "Central Cordoba"),
    ("Lanus",),
    ("Deportivo Riestra", "Dep. Riestra"),
    ("Talleres Cordoba", "Talleres"),
    ("Boca Juniors", "Boca"),
    ("Estudiantes L.P.", "Estudiantes de La Plata"),
    ("Instituto Cordoba", "Instituto"),
    ("Gimnasia M.", "Gimnasia Mendoza"),
    ("San Lorenzo",),
    ("Independiente",),
    ("Newells Old Boys", "Newell's Old Boys", "Newells"),
    ("Union Santa Fe", "Union"),
    ("Velez Sarsfield", "Velez"),
)
ARG_ZONE_B = (
    ("Argentinos JRS", "Argentinos Juniors"),
    ("Aldosivi",),
    ("Atletico Tucuman", "Atl. Tucuman"),
    ("Banfield",),
    ("Barracas Central",),
    ("Belgrano Cordoba", "Belgrano"),
    ("River Plate", "River"),
    ("Gimnasia L.P.", "Gimnasia La Plata"),
    ("Estudiantes de Rio Cuarto", "Estudiantes Rio Cuarto"),
    ("Independ. Rivadavia", "Independiente Rivadavia"),
    ("Huracan",),
    ("Racing Club", "Racing"),
    ("Rosario Central",),
    ("Sarmiento Junin", "Sarmiento"),
    ("Tigre",),
)
ARG_KNOWN_FUTURE_INTERZONALS = (
    (("Platense",), ("Argentinos JRS", "Argentinos Juniors")),
    (("Racing Club", "Racing"), ("Independiente",)),
    (("Banfield",), ("Lanus",)),
    (("Boca Juniors", "Boca"), ("River Plate", "River")),
    (("Gimnasia M.", "Gimnasia Mendoza"), ("Independ. Rivadavia", "Independiente Rivadavia")),
)

MLS_EAST = (
    ("Atlanta United FC", "Atlanta United"),
    ("Charlotte FC",),
    ("Chicago Fire", "Chicago Fire FC"),
    ("FC Cincinnati", "Cincinnati"),
    ("Columbus Crew",),
    ("DC United", "D.C. United"),
    ("Inter Miami", "Inter Miami CF"),
    ("Montreal Impact", "CF Montreal", "CF Montréal"),
    ("Nashville SC",),
    ("New England Revolution",),
    ("New York City FC", "New York City"),
    ("Orlando City SC", "Orlando City"),
    ("Philadelphia Union",),
    ("New York Red Bulls", "Red Bull New York"),
    ("Toronto FC",),
)
MLS_WEST = (
    ("Austin", "Austin FC"),
    ("Colorado Rapids",),
    ("FC Dallas",),
    ("Houston Dynamo", "Houston Dynamo FC"),
    ("Los Angeles FC", "Los Angeles Football Club", "LAFC"),
    ("Los Angeles Galaxy", "LA Galaxy"),
    ("Minnesota United FC", "Minnesota United"),
    ("Portland Timbers",),
    ("Real Salt Lake",),
    ("St. Louis City", "St. Louis CITY SC", "St Louis City"),
    ("San Diego", "San Diego FC"),
    ("San Jose Earthquakes",),
    ("Seattle Sounders", "Seattle Sounders FC"),
    ("Sporting Kansas City",),
    ("Vancouver Whitecaps", "Vancouver Whitecaps FC"),
)


def _date(ctx: dict[str, Any], row: dict[str, Any]) -> str:
    return ctx["fixture_date"](row)


def _norm(ctx: dict[str, Any], value: Any) -> str:
    return ctx["norm"](value)


def _resolve_aliases(
    ctx: dict[str, Any],
    aliases: tuple[str, ...],
    keys: list[str] | set[str],
    names: dict[str, str],
) -> str | None:
    keyset = set(keys)
    normalized = [_norm(ctx, value) for value in aliases if value]
    for alias in normalized:
        if alias in keyset:
            return alias
    # Exact normalized team name comparison is the safe fallback.
    for key in sorted(keyset):
        n = _norm(ctx, names.get(key, key))
        if n in normalized:
            return key
    # Conservative unique containment fallback for provider spelling differences.
    candidates: set[str] = set()
    for key in sorted(keyset):
        n = _norm(ctx, names.get(key, key))
        for alias in normalized:
            if len(alias) >= 6 and (alias in n or n in alias):
                candidates.add(key)
    return next(iter(candidates)) if len(candidates) == 1 else None


def _resolve_group(
    ctx: dict[str, Any],
    specs: tuple[tuple[str, ...], ...],
    keys: list[str],
    names: dict[str, str],
) -> list[str] | None:
    out: list[str] = []
    for aliases in specs:
        key = _resolve_aliases(ctx, aliases, keys, names)
        if key is None or key in out:
            return None
        out.append(key)
    return out


def _state_from_table(table: dict[str, Any], keys: list[str]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    return (
        {k: int(table[k].points) for k in keys},
        {k: int(table[k].goals_for) for k in keys},
        {k: int(table[k].goals_against) for k in keys},
    )


def _empty_state(keys: list[str]) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    return ({k: 0 for k in keys}, {k: 0 for k in keys}, {k: 0 for k in keys})


def _rank(
    ctx: dict[str, Any],
    keys: list[str],
    points: dict[str, int],
    gf: dict[str, int],
    ga: dict[str, int],
    names: dict[str, str],
) -> list[str]:
    return ctx["ranking_for_values"](keys, points, gf, ga, names)


def _unique_order(prefix: list[str], base: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for key in [*prefix, *base]:
        if key not in seen:
            out.append(key)
            seen.add(key)
    return out


def _elo_win_probability(a: str, b: str, elo_scope: Any, home_for_a: bool = False) -> float:
    ea = elo_scope.teams[a].rating
    eb = elo_scope.teams[b].rating
    if home_for_a:
        ea += elo_scope.home_advantage
    scale = max(1.0, float(elo_scope.rating_scale))
    return 1.0 / (1.0 + 10.0 ** ((eb - ea) / scale))


def _single_winner(a: str, b: str, model: Any, elo_scope: Any, rng: random.Random, neutral: bool = False) -> str:
    if neutral and rng.random() < 0.5:
        home, away = b, a
    else:
        home, away = a, b
    score = model.draw(home, away, rng)
    if score is None:
        return a if rng.random() < _elo_win_probability(a, b, elo_scope, not neutral) else b
    hg, ag = score
    if hg > ag:
        return home
    if ag > hg:
        return away
    # Extra-time / shootout proxy uses neutral Elo after a draw.
    return a if rng.random() < _elo_win_probability(a, b, elo_scope, False) else b


def _two_leg_winner(
    high: str,
    low: str,
    model: Any,
    elo_scope: Any,
    rng: random.Random,
    higher_seed_tie: bool = False,
) -> str:
    first = model.draw(low, high, rng)
    second = model.draw(high, low, rng)
    if first is None or second is None:
        return high if rng.random() < _elo_win_probability(high, low, elo_scope, False) else low
    high_goals = first[1] + second[0]
    low_goals = first[0] + second[1]
    if high_goals > low_goals:
        return high
    if low_goals > high_goals:
        return low
    if higher_seed_tie:
        return high
    return high if rng.random() < _elo_win_probability(high, low, elo_scope, False) else low


def _two_leg_points_winner(
    high: str,
    low: str,
    model: Any,
    elo_scope: Any,
    rng: random.Random,
) -> str:
    first = model.draw(low, high, rng)
    second = model.draw(high, low, rng)
    if first is None or second is None:
        return high if rng.random() < _elo_win_probability(high, low, elo_scope, False) else low
    high_points = (3 if first[1] > first[0] else 1 if first[1] == first[0] else 0) + (3 if second[0] > second[1] else 1 if second[0] == second[1] else 0)
    low_points = (3 if first[0] > first[1] else 1 if first[0] == first[1] else 0) + (3 if second[1] > second[0] else 1 if second[1] == second[0] else 0)
    if high_points > low_points:
        return high
    if low_points > high_points:
        return low
    high_goals = first[1] + second[0]
    low_goals = first[0] + second[1]
    if high_goals > low_goals:
        return high
    if low_goals > high_goals:
        return low
    return high if rng.random() < _elo_win_probability(high, low, elo_scope, False) else low


def _best_of_three(high: str, low: str, model: Any, elo_scope: Any, rng: random.Random) -> str:
    high_wins = 0
    low_wins = 0
    for home, away in ((high, low), (low, high), (high, low)):
        winner = _single_winner(home, away, model, elo_scope, rng, neutral=False)
        if winner == high:
            high_wins += 1
        else:
            low_wins += 1
        if high_wins == 2 or low_wins == 2:
            break
    return high if high_wins > low_wins else low


def _prepare_schedule(
    ctx: dict[str, Any],
    schedule: list[tuple[str, str]],
    model: Any,
    runs: int,
    seed_prefix: str,
) -> tuple[list[tuple[str, str, list[int], list[int]]], list[float], int] | None:
    prepared: list[tuple[str, str, list[int], list[int]]] = []
    weights: list[float] = []
    mature = 0
    for occurrence, (home, away) in enumerate(schedule):
        pack = model.cdfs(home, away)
        if pack is None:
            return None
        home_cdf, away_cdf, is_mature, weight = pack
        mature += 1 if is_mature else 0
        weights.append(float(weight))
        seed = f"{seed_prefix}|{home}|{away}|{occurrence}|{runs}"
        rng = random.Random(int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16], 16))
        prepared.append((
            home,
            away,
            ctx["simulate_counts"](rng, home_cdf, runs),
            ctx["simulate_counts"](rng, away_cdf, runs),
        ))
    return prepared, weights, mature


def _apply_prepared(
    ctx: dict[str, Any],
    prepared: list[tuple[str, str, list[int], list[int]]],
    run_idx: int,
    points: dict[str, int],
    gf: dict[str, int],
    ga: dict[str, int],
) -> None:
    for home, away, home_draws, away_draws in prepared:
        ctx["apply_score"](home, away, home_draws[run_idx], away_draws[run_idx], points, gf, ga)


def _rows_and_meta(
    ctx: dict[str, Any],
    entry: dict[str, Any],
    fixtures: list[dict[str, Any]],
    runs: int,
    elo_scope: Any,
    current_table: dict[str, Any],
    team_keys: list[str],
    names: dict[str, str],
    position_counts: dict[str, list[int]],
    point_totals: dict[str, int],
    weights: list[float],
    mature: int,
    format_label: str,
    regular_target: int,
    future_count: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    current_order = ctx["current_ranking"](current_table)
    current_positions = {k: idx + 1 for idx, k in enumerate(current_order)}
    rows: list[dict[str, Any]] = []
    for key in team_keys:
        counts = position_counts[key]
        team = current_table[key]
        rows.append({
            "team_key": key,
            "team_name": names[key],
            "team_logo": team.logo,
            "elo_rating": elo_scope.teams[key].rating,
            "current_position": current_positions[key],
            "current_played": team.played,
            "current_points": team.points,
            "current_goal_difference": team.goal_difference,
            "current_goals_for": team.goals_for,
            "expected_position": sum((idx + 1) * count for idx, count in enumerate(counts)) / runs,
            "expected_points": point_totals[key] / runs,
            "title_probability": counts[0] / runs,
            "top2_probability": sum(counts[:min(2, len(counts))]) / runs,
            "top4_probability": sum(counts[:min(4, len(counts))]) / runs,
            "position_probabilities_json": json.dumps([count / runs for count in counts], separators=(",", ":")),
            "split_group_probabilities_json": "[]",
        })
    season = str(entry.get("app_season") or entry.get("target_app_season") or "")
    meta = {
        "league_code": str(entry.get("league_code") or ""),
        "country": str(entry.get("country") or ""),
        "league_name": str(entry.get("league") or ""),
        "season": season,
        "team_count": len(team_keys),
        "current_completed_matches": len(fixtures),
        "remaining_matches": int(future_count),
        "simulation_runs": runs,
        "model_version": ctx["model_version"],
        "elo_model_version": elo_scope.model_version,
        "average_elo_weight": sum(weights) / max(1, len(weights)),
        "tie_break_model": ctx["tie_break_model"],
        "mature_fixture_count": mature,
        "total_simulated_fixtures": int(future_count),
        "format_label": format_label,
        "regular_stage_target_matches": int(regular_target),
        "split_group_count": 0,
        "split_group_keys_json": "[]",
    }
    return meta, rows


def _init_counts(team_keys: list[str]) -> tuple[dict[str, list[int]], dict[str, int]]:
    return ({k: [0] * len(team_keys) for k in team_keys}, {k: 0 for k in team_keys})


def _record_run(
    order: list[str],
    team_keys: list[str],
    points: dict[str, int],
    position_counts: dict[str, list[int]],
    point_totals: dict[str, int],
) -> None:
    order = _unique_order(order, team_keys)
    if len(order) != len(team_keys):
        raise RuntimeError("special league final order does not cover every team")
    for pos, key in enumerate(order):
        position_counts[key][pos] += 1
        point_totals[key] += points[key]


def _argentina(entry: dict[str, Any], fixtures: list[dict[str, Any]], runs: int, elo_scope: Any, ctx: dict[str, Any]):
    phase = [f for f in fixtures if _date(ctx, f) >= ARG_CLAUSURA_START]
    table = ctx["build_table"](phase)
    if len(table) != 30:
        return None
    keys = sorted(table)
    if any(k not in elo_scope.teams for k in keys):
        return None
    names = {k: table[k].name for k in keys}
    zone_a = _resolve_group(ctx, ARG_ZONE_A, keys, names)
    zone_b = _resolve_group(ctx, ARG_ZONE_B, keys, names)
    if zone_a is None or zone_b is None or set(zone_a) | set(zone_b) != set(keys):
        return None

    intra = [
        *ctx["remaining_stage_schedule"](zone_a, ctx["UNORDERED"], 1, [f for f in phase if _norm(ctx, f.get("home_team")) in zone_a and _norm(ctx, f.get("away_team")) in zone_a]),
        *ctx["remaining_stage_schedule"](zone_b, ctx["UNORDERED"], 1, [f for f in phase if _norm(ctx, f.get("home_team")) in zone_b and _norm(ctx, f.get("away_team")) in zone_b]),
    ]
    ordered_counts, _, home_games = ctx["count_pairs"](phase)
    cross_existing: set[tuple[str, str]] = set()
    cross_degree = {k: 0 for k in keys}
    for f in phase:
        h, a = _norm(ctx, f.get("home_team")), _norm(ctx, f.get("away_team"))
        if not h or not a:
            continue
        if (h in zone_a and a in zone_b) or (h in zone_b and a in zone_a):
            pair = tuple(sorted((h, a)))
            if pair not in cross_existing:
                cross_existing.add(pair)
                cross_degree[h] += 1
                cross_degree[a] += 1

    cross_future: list[tuple[str, str]] = []
    for left_alias, right_alias in ARG_KNOWN_FUTURE_INTERZONALS:
        left = _resolve_aliases(ctx, left_alias, keys, names)
        right = _resolve_aliases(ctx, right_alias, keys, names)
        if left is None or right is None:
            return None
        pair = tuple(sorted((left, right)))
        if pair in cross_existing:
            continue
        if cross_degree[left] >= 2 or cross_degree[right] >= 2:
            continue
        cross_future.append((left, right))
        cross_existing.add(pair)
        cross_degree[left] += 1
        cross_degree[right] += 1
        ordered_counts[(left, right)] = ordered_counts.get((left, right), 0) + 1
        home_games[left] = home_games.get(left, 0) + 1

    # Fill any postponement/remaining interzonal deficits deterministically.
    while any(cross_degree[k] < 2 for k in keys):
        candidates_a = sorted((k for k in zone_a if cross_degree[k] < 2), key=lambda k: (cross_degree[k], names[k]))
        if not candidates_a:
            return None
        a = candidates_a[0]
        candidates_b = [k for k in zone_b if cross_degree[k] < 2 and tuple(sorted((a, k))) not in cross_existing]
        if not candidates_b:
            return None
        b = sorted(candidates_b, key=lambda k: (cross_degree[k], names[k]))[0]
        home, away = ctx["choose_home"](a, b, ordered_counts, home_games)
        cross_future.append((home, away))
        pair = tuple(sorted((a, b)))
        cross_existing.add(pair)
        cross_degree[a] += 1
        cross_degree[b] += 1
        ordered_counts[(home, away)] = ordered_counts.get((home, away), 0) + 1
        home_games[home] = home_games.get(home, 0) + 1

    schedule = [*intra, *cross_future]
    history = ctx["LeagueHistory"](fixtures)
    model = ctx["MatchModel"](history, elo_scope, "ARG", "2026")
    packed = _prepare_schedule(ctx, schedule, model, runs, "ARG|2026|clausura")
    if packed is None:
        return None
    prepared, weights, mature = packed
    base_p, base_gf, base_ga = _state_from_table(table, keys)
    position_counts, point_totals = _init_counts(keys)
    rng = random.Random(2026100501)

    for run_idx in range(runs):
        points, gf, ga = dict(base_p), dict(base_gf), dict(base_ga)
        _apply_prepared(ctx, prepared, run_idx, points, gf, ga)
        rank_a = _rank(ctx, zone_a, points, gf, ga, names)
        rank_b = _rank(ctx, zone_b, points, gf, ga, names)
        r16_pairs = [
            (rank_a[0], rank_b[7]), (rank_b[0], rank_a[7]),
            (rank_a[1], rank_b[6]), (rank_b[1], rank_a[6]),
            (rank_a[2], rank_b[5]), (rank_b[2], rank_a[5]),
            (rank_a[3], rank_b[4]), (rank_b[3], rank_a[4]),
        ]
        r16_winners, r16_losers = [], []
        for high, low in r16_pairs:
            winner = _single_winner(high, low, model, elo_scope, rng, neutral=False)
            r16_winners.append(winner)
            r16_losers.append(low if winner == high else high)
        q_pairs = [(r16_winners[0], r16_winners[7]), (r16_winners[1], r16_winners[6]), (r16_winners[2], r16_winners[5]), (r16_winners[3], r16_winners[4])]
        q_winners, q_losers = [], []
        for a, b in q_pairs:
            # Host the better original zone seed.
            seed_a = (rank_a.index(a) + 1) if a in rank_a else (rank_b.index(a) + 1)
            seed_b = (rank_a.index(b) + 1) if b in rank_a else (rank_b.index(b) + 1)
            high, low = (a, b) if seed_a <= seed_b else (b, a)
            winner = _single_winner(high, low, model, elo_scope, rng, neutral=False)
            q_winners.append(winner)
            q_losers.append(low if winner == high else high)
        s_pairs = [(q_winners[0], q_winners[3]), (q_winners[1], q_winners[2])]
        s_winners, s_losers = [], []
        for a, b in s_pairs:
            seed_a = (rank_a.index(a) + 1) if a in rank_a else (rank_b.index(a) + 1)
            seed_b = (rank_a.index(b) + 1) if b in rank_a else (rank_b.index(b) + 1)
            high, low = (a, b) if seed_a <= seed_b else (b, a)
            winner = _single_winner(high, low, model, elo_scope, rng, neutral=False)
            s_winners.append(winner)
            s_losers.append(low if winner == high else high)
        champion = _single_winner(s_winners[0], s_winners[1], model, elo_scope, rng, neutral=True)
        runner = s_winners[1] if champion == s_winners[0] else s_winners[0]
        base_rank = _rank(ctx, keys, points, gf, ga, names)
        order = _unique_order([champion, runner, *s_losers, *q_losers, *r16_losers], base_rank)
        _record_run(order, keys, points, position_counts, point_totals)

    return _rows_and_meta(
        ctx, entry, fixtures, runs, elo_scope, table, keys, names,
        position_counts, point_totals, weights, mature,
        "Clausura: two 15-team zones + top-8 knockout",
        240, len(schedule) + 15,
    )


def _colombia(entry: dict[str, Any], fixtures: list[dict[str, Any]], runs: int, elo_scope: Any, ctx: dict[str, Any]):
    phase = [f for f in fixtures if _date(ctx, f) >= COL_II_START]
    table = ctx["build_table"](p