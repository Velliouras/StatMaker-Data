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