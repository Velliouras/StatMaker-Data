#!/usr/bin/env python3
"""Idempotently add supported Double Chance and conservative team-name mapping.

This migration edits the existing production modules rather than introducing a
second odds engine. It is intentionally marker-based and fails closed whenever
the expected source shape changes.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AUDIT = ROOT / "scripts" / "odds_api_io_market_audit.py"
ODDS = ROOT / "scripts" / "update_domestic_odds_api_io.py"
PIPELINE = ROOT / "scripts" / "domestic_live_july_pipeline.py"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if new in text:
        return text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"{label}: expected one marker, found {count}")
    return text.replace(old, new, 1)


def replace_function(text: str, name: str, replacement: str) -> str:
    marker = f"def {name}("
    start = text.find(marker)
    if start < 0:
        raise RuntimeError(f"function not found: {name}")
    next_def = text.find("\ndef ", start + len(marker))
    next_class = text.find("\nclass ", start + len(marker))
    candidates = [value for value in (next_def, next_class) if value >= 0]
    end = min(candidates) + 1 if candidates else len(text)
    current = text[start:end].rstrip()
    desired = replacement.rstrip()
    if current == desired:
        return text
    return text[:start] + desired + "\n\n" + text[end:]


def patch_audit() -> None:
    text = AUDIT.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '    "TEAM_SHOTS_ON_TARGET",\n}',
        '    "TEAM_SHOTS_ON_TARGET",\n    "DOUBLE_CHANCE",\n}',
        "audit supported set",
    )
    text = replace_once(
        text,
        '    "DOUBLE_CHANCE",\n    "DRAW_NO_BET",',
        '    "DRAW_NO_BET",',
        "audit-only Double Chance removal",
    )
    text = replace_once(
        text,
        '        ({"name": "Double Chance"}, "DOUBLE_CHANCE", "audit_only"),',
        '        ({"name": "Double Chance"}, "DOUBLE_CHANCE", "supported"),',
        "Double Chance self-check",
    )
    AUDIT.write_text(text, encoding="utf-8")


def patch_odds_generator() -> None:
    text = ODDS.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '    "TEAM_SHOTS_ON_TARGET",\n}',
        '    "TEAM_SHOTS_ON_TARGET",\n    "DOUBLE_CHANCE",\n}',
        "generator supported set",
    )
    text = replace_once(
        text,
        '    "TEAM_SHOTS_ON_TARGET",\n]',
        '    "TEAM_SHOTS_ON_TARGET",\n    "DOUBLE_CHANCE",\n]',
        "generator emitted-count set",
    )

    helper_marker = "def record_unmatched_team("
    helpers = '''TEAM_NAME_PREFIX_TOKENS = {
    "club", "clube", "deportivo", "deportes", "sporting", "atletico",
    "association", "asociacion", "fotbal", "fotboll", "football",
    "sociedad", "racing", "royal", "real",
}


def simplified_team_name(value: Any) -> str:
    words = normalize_text(value, drop_suffixes=True).split()
    while len(words) > 1 and words[0] in TEAM_NAME_PREFIX_TOKENS:
        words.pop(0)
    return " ".join(words)


'''
    if "def simplified_team_name(" not in text:
        index = text.find(helper_marker)
        if index < 0:
            raise RuntimeError("team simplification insertion marker missing")
        text = text[:index] + helpers + text[index:]

    canonical_function = '''def canonical_team_info(name: str, league_code: str, aliases: Dict[str, Dict[str, str]], debug: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    normalized = normalize_text(name, drop_suffixes=True)
    simplified = simplified_team_name(normalized)
    league_aliases = aliases.get(league_code, {})
    for candidate in dict.fromkeys([normalized, simplified]):
        if candidate and candidate in league_aliases:
            canonical = league_aliases[candidate]
            return canonical, canonical
    record_unmatched_team(debug, league_code, str(name or "").strip(), normalized)
    provider_name = str(name or "").strip()
    return provider_name, None
'''
    text = replace_function(text, "canonical_team_info", canonical_function)

    double_chance = '''    if family == "DOUBLE_CHANCE":
        for row in rows:
            direct_1x = to_float(row.get("1X") or row.get("1x"))
            direct_12 = to_float(row.get("12"))
            direct_x2 = to_float(row.get("X2") or row.get("x2") or row.get("2X") or row.get("2x"))
            if direct_1x is not None or direct_12 is not None or direct_x2 is not None:
                add_market(out, "DOUBLE_CHANCE", "1X", direct_1x, bookmaker)
                add_market(out, "DOUBLE_CHANCE", "12", direct_12, bookmaker)
                add_market(out, "DOUBLE_CHANCE", "X2", direct_x2, bookmaker)
                continue

            label = normalize_text(row_name(row), drop_suffixes=True)
            price = to_float(row.get("under") or row.get("over")) or row_price(row)
            if not label or price is None:
                continue
            if label in {"1x", "home or draw", "home draw"} or label.endswith(" or draw"):
                add_market(out, "DOUBLE_CHANCE", "1X", price, bookmaker)
            elif label in {"x2", "2x", "draw or away", "away or draw"} or label.startswith("draw or "):
                add_market(out, "DOUBLE_CHANCE", "X2", price, bookmaker)
            elif label in {"12", "home or away", "no draw"} or (" or " in label and "draw" not in label):
                add_market(out, "DOUBLE_CHANCE", "12", price, bookmaker)
            else:
                record_skipped_market(debug, raw_name, "unrecognized Double Chance row", row_name(row))
        return out

'''
    marker = '    if family == "BTTS":\n'
    if double_chance not in text:
        index = text.find(marker)
        if index < 0:
            raise RuntimeError("Double Chance insertion marker missing")
        text = text[:index] + double_chance + text[index:]

    ODDS.write_text(text, encoding="utf-8")


def patch_pipeline_aliases() -> None:
    text = PIPELINE.read_text(encoding="utf-8")
    replacement = '''def generated_aliases(registry: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, str]]:
    aliases = odds_fetch.load_aliases()
    for league in registry:
        code = str(league.get("leagueCode") or "")
        bucket = aliases.setdefault(code, {})
        cache = load_json(stats_fetch.cache_path_for(league), {})
        canonical_names = {
            str(fixture.get(key) or "").strip()
            for fixture in cache.get("fixtures", []) or []
            if isinstance(fixture, dict)
            for key in ("home_team", "away_team")
            if str(fixture.get(key) or "").strip()
        }
        owners: Dict[str, set[str]] = {}
        for canonical in canonical_names:
            variants = {
                odds_fetch.normalize_text(canonical, drop_suffixes=True),
                odds_fetch.simplified_team_name(canonical),
            }
            for variant in variants:
                if variant:
                    owners.setdefault(variant, set()).add(canonical)
        for variant, candidates in owners.items():
            if len(candidates) == 1:
                bucket.setdefault(variant, next(iter(candidates)))
    return aliases
'''
    text = replace_function(text, "generated_aliases", replacement)
    PIPELINE.write_text(text, encoding="utf-8")


def main() -> int:
    patch_audit()
    patch_odds_generator()
    patch_pipeline_aliases()
    print("Domestic market expansion applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
