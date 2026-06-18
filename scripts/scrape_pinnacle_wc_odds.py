#!/usr/bin/env python3
"""
StatMaker Pinnacle World Cup odds scraper.

Purpose:
- Runs outside the Android app, from GitHub Actions.
- Reads StatMaker's World Cup fixture JSON.
- Opens Pinnacle football / FIFA World Cup matchups pages.
- Extracts conservative pre-match odds only when a fixture and market can be matched.
- Writes app-compatible odds JSON plus a debug report and snapshot.

This is intentionally conservative. It never invents odds. If a match/market cannot be
mapped with confidence, it is left out and explained in debug_report.json.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

PINNACLE_URLS = [
    "https://www.pinnacle.com/en/soccer/fifa-world-cup/matchups",
    "https://www.pinnacle.com/en/soccer/matchups",
]

FIXTURES_PATH = Path(os.getenv("STATMAKER_WC_FIXTURES", "world-cup/world_cup_2026.json"))
OUTPUT_PATH = Path(os.getenv("STATMAKER_PINNACLE_WC_ODDS_OUTPUT", "odds/pinnacle/world_cup_odds.json"))
DEBUG_PATH = Path(os.getenv("STATMAKER_PINNACLE_WC_ODDS_DEBUG", "odds/pinnacle/debug_report.json"))
SNAPSHOT_PATH = Path(os.getenv("STATMAKER_PINNACLE_PROBE_SNAPSHOT", "odds/pinnacle/pinnacle_probe_snapshot.txt"))
TIMEOUT_MS = int(os.getenv("STATMAKER_PINNACLE_TIMEOUT_MS", "22000"))
MAX_FIXTURES = int(os.getenv("STATMAKER_WC_ODDS_MAX_FIXTURES", "64"))
MIN_ODD = float(os.getenv("STATMAKER_MIN_VALID_ODD", "1.01"))
MAX_ODD = float(os.getenv("STATMAKER_MAX_VALID_ODD", "1000"))

BOOKMAKER = "Pinnacle"
SOURCE = "pinnacle"
COMPETITION = "World Cup"
SEASON = "2026"
SCRIPT_VERSION = "pinnacle-wc-odds-v1"

KEYWORDS = [
    "football",
    "soccer",
    "world cup",
    "fifa",
    "odds",
    "matchups",
    "spread",
    "money line",
    "total",
    "over",
    "under",
    "both teams",
]

TEAM_ALIASES = {
    "usa": ["united states", "usa", "u.s.a", "u.s.", "us"],
    "united states": ["united states", "usa", "u.s.a", "u.s.", "us"],
    "czech republic": ["czech republic", "czechia", "czech rep"],
    "czechia": ["czechia", "czech republic", "czech rep"],
    "ivory coast": ["ivory coast", "cote d'ivoire", "cote d ivoire", "côte d’ivoire", "côte d'ivoire"],
    "bosnia & herzegovina": ["bosnia & herzegovina", "bosnia and herzegovina", "bosnia herzegovina", "bosnia"],
    "bosnia and herzegovina": ["bosnia & herzegovina", "bosnia and herzegovina", "bosnia herzegovina", "bosnia"],
    "south korea": ["south korea", "korea republic", "republic of korea", "korea rep"],
    "dr congo": ["dr congo", "congo dr", "d.r. congo", "democratic republic of congo"],
    "cape verde": ["cape verde", "cabo verde"],
    "curacao": ["curacao", "curaçao"],
    "turkey": ["turkey", "turkiye", "türkiye"],
}


@dataclass(frozen=True)
class Fixture:
    match_id: str
    date: str
    time: str
    home_team: str
    away_team: str
    status: str

    @property
    def title(self) -> str:
        return f"{self.home_team} vs {self.away_team}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_text(value: str) -> str:
    value = clean_text(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def team_variants(team: str) -> list[str]:
    norm = normalize_text(team)
    variants = TEAM_ALIASES.get(norm, [team])
    normalized = [normalize_text(item) for item in variants]
    if norm not in normalized:
        normalized.append(norm)
    return [item for item in normalized if item]


def contains_team(text: str, team: str) -> bool:
    norm_text = normalize_text(text)
    return any(variant in norm_text for variant in team_variants(team))


def line_has_team(line: str, team: str) -> bool:
    return any(variant in normalize_text(line) for variant in team_variants(team))


def load_fixtures() -> list[Fixture]:
    if not FIXTURES_PATH.exists():
        return []
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    fixtures: list[Fixture] = []
    for item in data.get("matches", []):
        status = clean_text(item.get("status")).lower()
        if status in {"finished", "complete", "completed", "ft"}:
            continue
        home = clean_text(item.get("homeTeam") or item.get("team1"))
        away = clean_text(item.get("awayTeam") or item.get("team2"))
        if not home or not away:
            continue
        fixtures.append(
            Fixture(
                match_id=clean_text(item.get("matchId") or item.get("id") or f"{home}_{away}"),
                date=clean_text(item.get("date")),
                time=clean_text(item.get("time")),
                home_team=home,
                away_team=away,
                status=status,
            )
        )
    return fixtures[:MAX_FIXTURES]


def parse_decimal_odd(value: str) -> float | None:
    token = clean_text(value)
    match = re.fullmatch(r"(\d{1,3})[\.,](\d{2,3})", token)
    if not match:
        return None
    odd = float(f"{match.group(1)}.{match.group(2)}")
    return round(odd, 2) if MIN_ODD <= odd <= MAX_ODD else None


def odds_tokens(text: str) -> list[float]:
    seen: list[float] = []
    # Decimal odds. This deliberately excludes dates/times and integers.
    for raw in re.findall(r"(?<!\d)(?:1[\.,]0[1-9]|1[\.,][1-9]\d|[2-9][\.,]\d{2}|[1-9]\d[\.,]\d{2})(?!\d)", text or ""):
        odd = parse_decimal_odd(raw)
        if odd is not None:
            seen.append(odd)
    return seen


def unique_market(markets: list[dict[str, Any]], candidate: dict[str, Any]) -> None:
    key = (candidate.get("market"), candidate.get("selection"), candidate.get("team"), candidate.get("line"))
    for existing in markets:
        existing_key = (existing.get("market"), existing.get("selection"), existing.get("team"), existing.get("line"))
        if existing_key == key:
            return
    markets.append(candidate)


def extract_candidate_urls(html: str, base_url: str) -> list[str]:
    candidates: set[str] = set()
    for raw in re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html or "", re.IGNORECASE):
        url = urljoin(base_url, raw)
        lower = url.lower()
        if any(token in lower for token in ["api", "event", "market", "coupon", "odds", "sports", "prematch", "matchup", "graphql", "json", "chunk", "js"]):
            candidates.add(url)
    for raw in re.findall(r"https?://[^\"'<>\\\s]+", html or ""):
        lower = raw.lower()
        if any(token in lower for token in ["api", "event", "market", "coupon", "odds", "sports", "prematch", "matchup", "graphql", "json"]):
            candidates.add(raw)
    return sorted(candidates)[:100]


def keyword_presence(visible_text: str, html: str) -> dict[str, dict[str, bool]]:
    vt = normalize_text(visible_text)
    ht = normalize_text(html)
    return {
        keyword: {
            "visibleText": keyword in vt,
            "html": keyword in ht,
        }
        for keyword in KEYWORDS
    }


def split_lines(text: str) -> list[str]:
    return [clean_text(line) for line in (text or "").splitlines() if clean_text(line)]


def find_fixture_window(visible_text: str, html: str, fixture: Fixture) -> tuple[str, str | None]:
    """Return a local text window that appears to contain both teams."""
    lines = split_lines(visible_text)
    home_indices = [idx for idx, line in enumerate(lines) if line_has_team(line, fixture.home_team)]
    away_indices = [idx for idx, line in enumerate(lines) if line_has_team(line, fixture.away_team)]

    best_pair: tuple[int, int] | None = None
    best_distance = 999999
    for hi in home_indices:
        for ai in away_indices:
            distance = abs(hi - ai)
            if distance < best_distance:
                best_distance = distance
                best_pair = (hi, ai)

    if best_pair and best_distance <= 28:
        start = max(0, min(best_pair) - 14)
        end = min(len(lines), max(best_pair) + 30)
        return "\n".join(lines[start:end]), f"visible lines {start}-{end}, team distance={best_distance}"

    # Fallback: inspect normalized HTML/text, useful when teams are in embedded JSON/script text.
    combined = clean_text(visible_text + "\n" + html)
    norm_combined = normalize_text(combined)
    positions_home = [norm_combined.find(v) for v in team_variants(fixture.home_team) if norm_combined.find(v) >= 0]
    positions_away = [norm_combined.find(v) for v in team_variants(fixture.away_team) if norm_combined.find(v) >= 0]
    if positions_home and positions_away:
        best_home = min(positions_home)
        best_away = min(positions_away)
        if abs(best_home - best_away) <= 6000:
            center = min(best_home, best_away)
            start = max(0, center - 1800)
            end = min(len(combined), center + 4200)
            return combined[start:end], f"combined text/html window, distance={abs(best_home - best_away)}"

    return "", None


def extract_1x2_markets(window: str, fixture: Fixture) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    tokens = odds_tokens(window)
    if len(tokens) < 3:
        return markets

    # Conservative assumption: in a matched fixture block, the first three decimal odds are 1-X-2.
    # Pinnacle pages often expose Matchups with the main prices near team names. Debug report keeps the window for verification.
    home_odd, draw_odd, away_odd = tokens[0], tokens[1], tokens[2]
    unique_market(markets, {
        "market": "1X2",
        "selection": f"{fixture.home_team} Win",
        "team": fixture.home_team,
        "line": None,
        "odd": home_odd,
    })
    unique_market(markets, {
        "market": "1X2",
        "selection": "Draw",
        "team": None,
        "line": None,
        "odd": draw_odd,
    })
    unique_market(markets, {
        "market": "1X2",
        "selection": f"{fixture.away_team} Win",
        "team": fixture.away_team,
        "line": None,
        "odd": away_odd,
    })
    return markets


def extract_total_markets(window: str) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    text = clean_text(window)

    patterns = [
        # Over 2.5 1.91 / Under 2.5 1.94
        r"\b(Over|Under)\s+([0-9]+(?:\.[05])?)\s+((?:1[\.,][0-9]{2})|(?:[2-9][\.,][0-9]{2})|(?:[1-9][0-9][\.,][0-9]{2}))",
        # O 2.5 1.91 / U 2.5 1.94
        r"\b(O|U)\s*([0-9]+(?:\.[05])?)\s+((?:1[\.,][0-9]{2})|(?:[2-9][\.,][0-9]{2})|(?:[1-9][0-9][\.,][0-9]{2}))",
    ]
    for pattern in patterns:
        for side, line_raw, odd_raw in re.findall(pattern, text, re.IGNORECASE):
            odd = parse_decimal_odd(odd_raw)
            if odd is None:
                continue
            line = float(line_raw)
            side_label = "Over" if side.lower().startswith("o") else "Under"
            unique_market(markets, {
                "market": "MATCH_GOALS",
                "selection": f"{side_label} {line:g} Goals",
                "team": None,
                "line": line,
                "odd": odd,
            })
    return markets


def extract_btts_markets(window: str) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    lowered = clean_text(window).lower()
    if "both teams" not in lowered and "btts" not in lowered:
        return markets
    # Look in a short BTTS context if possible.
    match = re.search(r"(both teams(?: to score)?|btts).{0,260}", window, re.IGNORECASE | re.DOTALL)
    context = match.group(0) if match else window
    for label in ["Yes", "No"]:
        m = re.search(label + r"\s+((?:1[\.,][0-9]{2})|(?:[2-9][\.,][0-9]{2})|(?:[1-9][0-9][\.,][0-9]{2}))", context, re.IGNORECASE)
        if not m:
            continue
        odd = parse_decimal_odd(m.group(1))
        if odd is None:
            continue
        unique_market(markets, {
            "market": "BTTS",
            "selection": label,
            "team": None,
            "line": None,
            "odd": odd,
        })
    return markets


def build_markets_from_window(window: str, fixture: Fixture) -> list[dict[str, Any]]:
    markets: list[dict[str, Any]] = []
    for candidate in extract_1x2_markets(window, fixture):
        unique_market(markets, candidate)
    for candidate in extract_total_markets(window):
        unique_market(markets, candidate)
    for candidate in extract_btts_markets(window):
        unique_market(markets, candidate)
    return markets


def count_market_types(markets: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for market in markets:
        key = str(market.get("market") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return counts


def select_best_page() -> tuple[dict[str, Any], str, str]:
    attempts: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_visible = ""
    best_html = ""

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1365, "height": 900},
            locale="en-GB",
            timezone_id="Europe/London",
        )
        page = context.new_page()

        for url in PINNACLE_URLS:
            response_status: int | None = None
            final_url = url
            title = ""
            visible_text = ""
            html = ""
            error: str | None = None
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                response_status = response.status if response else None
                page.wait_for_timeout(3500)
                title = page.title() or ""
                final_url = page.url or url
                visible_text = page.locator("body").inner_text(timeout=7000) if page.locator("body").count() else ""
                html = page.content()
            except PlaywrightTimeoutError as exc:
                error = f"timeout: {exc}"
            except Exception as exc:  # noqa: BLE001 - diagnostics should capture site-specific failures.
                error = f"{type(exc).__name__}: {exc}"

            result = {
                "requestedUrl": url,
                "finalUrl": final_url,
                "httpStatus": response_status,
                "title": title,
                "visibleTextLength": len(visible_text or ""),
                "htmlLength": len(html or ""),
                "oddsLikeNumbersInVisibleText": len(odds_tokens(visible_text)),
                "oddsLikeNumbersInHtml": len(odds_tokens(html)),
                "scriptApiCandidatesCount": len(extract_candidate_urls(html, final_url)),
                "error": error,
            }
            score = 0
            if response_status == 200:
                score += 40
            score += min(result["visibleTextLength"] // 100, 40)
            score += min(result["oddsLikeNumbersInVisibleText"], 80)
            score += min(result["scriptApiCandidatesCount"], 25)
            result["score"] = score
            attempts.append(result)

            if best is None or score > int(best.get("score") or 0):
                best = result
                best_visible = visible_text
                best_html = html

        context.close()
        browser.close()

    # Do not attach the attempts list to the original `best` dict object when it
    # is also one of the entries inside `attempts`; that creates a circular
    # reference and json.dumps(debug) fails on GitHub Actions.
    if best is None:
        best_summary: dict[str, Any] = {"error": "No Pinnacle candidate page could be inspected."}
    else:
        best_summary = dict(best)
    best_summary["attempts"] = [dict(item) for item in attempts]
    return best_summary, best_visible, best_html


def make_empty_feed(generated_at: str) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "generatedAt": generated_at,
        "country": "International",
        "competition": COMPETITION,
        "season": SEASON,
        "matches": [],
    }


def write_snapshot(best_page: dict[str, Any], visible_text: str, html: str, fixtures: list[Fixture], matched_debug: list[dict[str, Any]]) -> None:
    SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    fixture_presence = []
    for fixture in fixtures[:16]:
        fixture_presence.append({
            "matchId": fixture.match_id,
            "date": fixture.date,
            "time": fixture.time,
            "homeTeam": fixture.home_team,
            "awayTeam": fixture.away_team,
            "homeInVisibleText": contains_team(visible_text, fixture.home_team),
            "awayInVisibleText": contains_team(visible_text, fixture.away_team),
            "homeInHtml": contains_team(html, fixture.home_team),
            "awayInHtml": contains_team(html, fixture.away_team),
        })

    lines = [
        "StatMaker Pinnacle probe snapshot",
        json.dumps({
            "generatedAt": utc_now(),
            "scriptVersion": SCRIPT_VERSION,
            "source": SOURCE,
            "bookmaker": BOOKMAKER,
            "bestPage": best_page,
            "fixturesLoaded": len(fixtures),
            "snapshotNote": "Diagnostic only. Do not consume this file from the Android app.",
        }, ensure_ascii=False, indent=2),
        "",
        "===== Keyword presence =====",
        json.dumps(keyword_presence(visible_text, html), ensure_ascii=False, indent=2),
        "",
        "===== Fixture presence sample =====",
        json.dumps(fixture_presence, ensure_ascii=False, indent=2),
        "",
        "===== Matched fixture extraction sample =====",
        json.dumps(matched_debug[:12], ensure_ascii=False, indent=2),
        "",
        "===== Script/API candidates =====",
        "\n".join(extract_candidate_urls(html, best_page.get("finalUrl") or "")) or "None found.",
        "",
        "===== Visible text sample =====",
        (visible_text or "")[:14000] or "<empty>",
        "",
        "===== HTML sample =====",
        (html or "")[:14000] or "<empty>",
        "",
    ]
    SNAPSHOT_PATH.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    generated_at = utc_now()
    fixtures = load_fixtures()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)

    errors: list[str] = []
    best_page, visible_text, html = select_best_page()

    output = make_empty_feed(generated_at)
    matched_debug: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []

    for fixture in fixtures:
        window, window_reason = find_fixture_window(visible_text, html, fixture)
        if not window:
            unmatched.append({
                "matchId": fixture.match_id,
                "date": fixture.date,
                "time": fixture.time,
                "homeTeam": fixture.home_team,
                "awayTeam": fixture.away_team,
                "reason": "fixture teams not found close enough in Pinnacle visible text/html",
            })
            continue

        markets = build_markets_from_window(window, fixture)
        debug_item = {
            "matchId": fixture.match_id,
            "date": fixture.date,
            "time": fixture.time,
            "homeTeam": fixture.home_team,
            "awayTeam": fixture.away_team,
            "windowReason": window_reason,
            "oddsTokensInWindow": odds_tokens(window)[:24],
            "marketsExtracted": len(markets),
            "marketCounts": count_market_types(markets),
            "windowSample": window[:1400],
        }
        matched_debug.append(debug_item)

        if not markets:
            unmatched.append({
                "matchId": fixture.match_id,
                "date": fixture.date,
                "time": fixture.time,
                "homeTeam": fixture.home_team,
                "awayTeam": fixture.away_team,
                "reason": "fixture found, but no conservative odds market could be extracted",
                "windowReason": window_reason,
                "oddsTokensInWindow": odds_tokens(window)[:12],
            })
            continue

        output["matches"].append({
            "date": fixture.date,
            "homeTeam": fixture.home_team,
            "awayTeam": fixture.away_team,
            "markets": markets,
        })

    debug = {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "generatedAt": generated_at,
        "scriptVersion": SCRIPT_VERSION,
        "fixturesPath": str(FIXTURES_PATH),
        "outputPath": str(OUTPUT_PATH),
        "debugPath": str(DEBUG_PATH),
        "snapshotPath": str(SNAPSHOT_PATH),
        "bestPage": best_page,
        "fixturesLoaded": len(fixtures),
        "matchesMatched": len(output["matches"]),
        "matchesWithMarkets": len(output["matches"]),
        "marketsFound": sum(len(match["markets"]) for match in output["matches"]),
        "marketCounts": count_market_types([market for match in output["matches"] for market in match["markets"]]),
        "matchedFixtureDebug": matched_debug,
        "unmatchedFixtures": unmatched,
        "errors": errors,
        "notes": [
            "This is a first Pinnacle scraper, not final production logic.",
            "Only conservative markets are emitted. Inspect matchedFixtureDebug/windowSample after each run.",
            "If 1X2 ordering is wrong for Pinnacle visible text, switch to API candidate extraction before consuming in Android.",
        ],
    }

    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    DEBUG_PATH.write_text(json.dumps(debug, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_snapshot(best_page, visible_text, html, fixtures, matched_debug)


if __name__ == "__main__":
    main()
