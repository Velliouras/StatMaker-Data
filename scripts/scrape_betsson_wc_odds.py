#!/usr/bin/env python3
"""
StatMaker Betsson World Cup odds probe.

Purpose:
- Runs outside the Android app.
- Reads StatMaker's World Cup fixture JSON.
- Opens Betsson's World Cup football page with Playwright.
- Tries to extract pre-match odds for upcoming fixtures.
- Writes a stable odds JSON schema consumed by the Android app.
- Writes a debug report so we can see what matched and what failed.

This is intentionally a probe scraper, not a hard-coded production scraper.
Bookmaker pages are dynamic and can change structure without notice, so this
script uses several conservative extraction strategies and never invents odds.
If it cannot confidently map a market to a StatMaker fixture, it reports that
in debug_report.json and leaves the market out.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, date
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

BETSSON_WC_URL = os.getenv(
    "BETSSON_WC_URL",
    "https://www.betsson.com/en/sportsbook/football/world-cup",
)
FIXTURES_PATH = Path(os.getenv("STATMAKER_WC_FIXTURES", "world-cup/world_cup_2026.json"))
OUTPUT_PATH = Path(os.getenv("STATMAKER_WC_ODDS_OUTPUT", "odds/betsson/world_cup_odds.json"))
DEBUG_PATH = Path(os.getenv("STATMAKER_WC_ODDS_DEBUG", "odds/betsson/debug_report.json"))
LOCAL_TZ = os.getenv("STATMAKER_LOCAL_TZ", "Europe/Athens")
BOOKMAKER = "Betsson"
SOURCE = "betsson_probe"
COMPETITION = "World Cup"
SEASON = "2026"
SCRIPT_VERSION = "betsson-wc-odds-probe-v1"
MAX_FIXTURES = int(os.getenv("STATMAKER_WC_ODDS_MAX_FIXTURES", "64"))
MIN_ODD = float(os.getenv("STATMAKER_MIN_VALID_ODD", "1.01"))
MAX_ODD = float(os.getenv("STATMAKER_MAX_VALID_ODD", "1000"))

# A small, explicit alias set for names that often differ between feeds.
TEAM_ALIASES = {
    "usa": "united states",
    "u s a": "united states",
    "u.s.a": "united states",
    "us": "united states",
    "czech republic": "czechia",
    "czech rep": "czechia",
    "ivory coast": "ivory coast",
    "cote d ivoire": "ivory coast",
    "côte d ivoire": "ivory coast",
    "dr congo": "dr congo",
    "d r congo": "dr congo",
    "congo dr": "dr congo",
    "bosnia": "bosnia and herzegovina",
    "bosnia herzegovina": "bosnia and herzegovina",
    "bosnia & herzegovina": "bosnia and herzegovina",
    "south korea": "south korea",
    "korea republic": "south korea",
    "korea rep": "south korea",
    "curacao": "curacao",
    "curaçao": "curacao",
    "turkiye": "turkey",
    "türkiye": "turkey",
}

SUPPORTED_MARKETS = {
    "1X2",
    "MATCH_GOALS",
    "BTTS",
    "TEAM_GOALS",
}


@dataclass(frozen=True)
class Fixture:
    date: str
    time: str | None
    home: str
    away: str
    match_id: str | None
    status: str | None

    @property
    def title(self) -> str:
        return f"{self.home} vs {self.away}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_name(value: str) -> str:
    value = clean_text(value).lower()
    value = value.replace("&", " and ")
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return TEAM_ALIASES.get(value, value)


def contains_team(text: str, team: str) -> bool:
    text_norm = normalize_name(text)
    team_norm = normalize_name(team)
    if not text_norm or not team_norm:
        return False
    return team_norm in text_norm


def parse_decimal_odd(value: str) -> float | None:
    value = clean_text(value)
    if not value:
        return None

    # Decimal odds: 1.72 / 2,10
    decimal = re.fullmatch(r"(\d{1,3})[\.,](\d{1,3})", value)
    if decimal:
        odd = float(f"{decimal.group(1)}.{decimal.group(2)}")
        return odd if MIN_ODD <= odd <= MAX_ODD else None

    # Fractional odds occasionally appear in crawled text: 5/6, 13/5.
    fractional = re.fullmatch(r"(\d{1,4})\s*/\s*(\d{1,4})", value)
    if fractional:
        numerator = int(fractional.group(1))
        denominator = int(fractional.group(2))
        if denominator == 0:
            return None
        odd = 1.0 + numerator / denominator
        return round(odd, 2) if MIN_ODD <= odd <= MAX_ODD else None

    return None


def decimal_odd_tokens(text: str) -> list[float]:
    tokens: list[float] = []
    for raw in re.findall(r"(?<!\d)(?:\d{1,3}[\.,]\d{1,3}|\d{1,4}\s*/\s*\d{1,4})(?!\d)", text):
        odd = parse_decimal_odd(raw)
        if odd is not None:
            tokens.append(round(odd, 2))
    return tokens


def load_fixtures(path: Path) -> list[Fixture]:
    root = json.loads(path.read_text(encoding="utf-8"))
    fixtures: list[Fixture] = []
    for item in root.get("matches", []):
        status = clean_text(item.get("status")).lower() or None
        home_goals = item.get("homeGoals")
        away_goals = item.get("awayGoals")
        is_finished = status in {"finished", "complete", "completed", "ft", "after penalties"}
        if is_finished or home_goals is not None or away_goals is not None:
            continue
        home = clean_text(item.get("homeTeam"))
        away = clean_text(item.get("awayTeam"))
        match_date = clean_text(item.get("date"))
        if not home or not away or not match_date:
            continue
        fixtures.append(
            Fixture(
                date=match_date,
                time=clean_text(item.get("time")) or None,
                home=home,
                away=away,
                match_id=clean_text(item.get("matchId")) or None,
                status=status,
            )
        )
    fixtures.sort(key=lambda f: (f.date, f.time or "99:99", f.home, f.away))
    return fixtures[:MAX_FIXTURES]


def accept_cookies_if_present(page: Page) -> None:
    labels = [
        "Accept all",
        "Accept All",
        "I Accept",
        "Accept",
        "Agree",
        "Consent",
        "Allow all",
    ]
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I)).first
            if button.count() > 0:
                button.click(timeout=1500)
                page.wait_for_timeout(800)
                return
        except Exception:
            continue


def goto(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    accept_cookies_if_present(page)
    page.wait_for_timeout(2500)


def visible_text(page: Page) -> str:
    try:
        return clean_text(page.locator("body").inner_text(timeout=10000))
    except Exception:
        return ""


def extract_jsonish_objects(page: Page) -> list[Any]:
    """Best-effort extraction from JSON script tags.

    Many sportsbook frontends hydrate state through script tags. We only keep
    successfully parsed JSON roots and leave detailed interpretation to a
    conservative recursive collector.
    """
    roots: list[Any] = []
    try:
        script_texts = page.locator("script").evaluate_all("nodes => nodes.map(n => n.textContent || '')")
    except Exception:
        return roots

    for text in script_texts:
        text = text.strip()
        if not text or len(text) < 20:
            continue
        candidates: list[str] = []
        if text.startswith("{") or text.startswith("["):
            candidates.append(text)
        next_data = re.search(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", text, re.S)
        if next_data:
            candidates.append(next_data.group(1))
        for candidate in candidates:
            try:
                roots.append(json.loads(candidate))
            except Exception:
                continue
    return roots


def walk_json(value: Any) -> Iterable[Any]:
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def text_window(full_text: str, home: str, away: str, radius: int = 600) -> str:
    if not full_text:
        return ""
    lower = full_text.lower()
    home_norm = normalize_name(home)
    away_norm = normalize_name(away)

    # Search on a normalized copy, but slice original text by approximate position.
    normalized = normalize_name(full_text)
    h = normalized.find(home_norm)
    a = normalized.find(away_norm)
    if h < 0 or a < 0:
        return ""
    start_norm = max(0, min(h, a) - radius)
    end_norm = min(len(normalized), max(h, a) + radius)

    # Fallback: direct raw search often works for clean team names.
    raw_positions = [p for p in [lower.find(home.lower()), lower.find(away.lower())] if p >= 0]
    if raw_positions:
        start = max(0, min(raw_positions) - radius)
        end = min(len(full_text), max(raw_positions) + radius)
        return full_text[start:end]

    return normalized[start_norm:end_norm]


def build_1x2_from_odds(home: str, away: str, odds: list[float]) -> list[dict[str, Any]]:
    if len(odds) < 3:
        return []
    home_odd, draw_odd, away_odd = odds[0], odds[1], odds[2]
    return [
        {"market": "1X2", "selection": home, "team": home, "line": None, "odd": home_odd},
        {"market": "1X2", "selection": "Draw", "team": None, "line": None, "odd": draw_odd},
        {"market": "1X2", "selection": away, "team": away, "line": None, "odd": away_odd},
    ]


def try_extract_from_json_roots(fixture: Fixture, roots: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Conservative JSON-state extraction.

    This does not assume a Betsson internal schema. It looks for dictionaries
    that contain both team names and then tries to identify market/outcome-like
    children. If confidence is low, it returns no markets and the debug report
    explains why.
    """
    notes: list[str] = []
    markets: list[dict[str, Any]] = []

    for root in roots:
        for node in walk_json(root):
            if not isinstance(node, dict):
                continue
            node_text = json.dumps(node, ensure_ascii=False)[:5000]
            if not contains_team(node_text, fixture.home) or not contains_team(node_text, fixture.away):
                continue

            odds = decimal_odd_tokens(node_text)
            if len(odds) >= 3 and not markets:
                # This is only a fallback, but JSON nodes are generally smaller
                # than the full visible text, so the first three odds are more
                # likely to be 1X2.
                markets.extend(build_1x2_from_odds(fixture.home, fixture.away, odds[:3]))
                notes.append("1X2 extracted from JSON-like state node")
                break
        if markets:
            break

    return markets, notes


def try_extract_from_visible_text(fixture: Fixture, page_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    notes: list[str] = []
    markets: list[dict[str, Any]] = []
    window = text_window(page_text, fixture.home, fixture.away)
    if not window:
        notes.append("fixture teams not found together in visible Betsson text")
        return markets, notes

    odds = decimal_odd_tokens(window)
    if len(odds) >= 3:
        markets.extend(build_1x2_from_odds(fixture.home, fixture.away, odds[:3]))
        notes.append("1X2 extracted from visible text window")
    else:
        notes.append(f"fixture found in visible text, but only {len(odds)} odds tokens found nearby")

    # Conservative market snippets. These only add a market if the line label
    # and odd are close in the same text window.
    for line in (1.5, 2.5, 3.5):
        pattern = re.compile(rf"(?:over|o)\s*{line}\D{{0,40}}(\d{{1,3}}[\.,]\d{{1,3}}|\d{{1,4}}\s*/\s*\d{{1,4}})", re.I)
        match = pattern.search(window)
        if match:
            odd = parse_decimal_odd(match.group(1))
            if odd:
                markets.append({"market": "MATCH_GOALS", "selection": f"Over {line} Goals", "team": None, "line": line, "odd": odd})
                notes.append(f"Over {line} extracted from visible text window")

    btts = re.search(r"(?:both teams to score|btts)\D{0,80}(?:yes)\D{0,40}(\d{1,3}[\.,]\d{1,3}|\d{1,4}\s*/\s*\d{1,4})", window, re.I)
    if btts:
        odd = parse_decimal_odd(btts.group(1))
        if odd:
            markets.append({"market": "BTTS", "selection": "Both Teams to Score - Yes", "team": None, "line": None, "odd": odd})
            notes.append("BTTS Yes extracted from visible text window")

    return markets, notes


def dedupe_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[Any, ...]] = set()
    clean: list[dict[str, Any]] = []
    for item in markets:
        market = clean_text(item.get("market"))
        selection = clean_text(item.get("selection"))
        team = clean_text(item.get("team")) or None
        line = item.get("line")
        odd = item.get("odd")
        if market not in SUPPORTED_MARKETS or not selection:
            continue
        try:
            odd = round(float(odd), 2)
        except Exception:
            continue
        if odd <= MIN_ODD or odd > MAX_ODD:
            continue
        key = (market, selection.lower(), team.lower() if team else None, line, odd)
        if key in seen:
            continue
        seen.add(key)
        row = {"market": market, "selection": selection, "odd": odd}
        if team:
            row["team"] = team
        if line is not None:
            row["line"] = line
        clean.append(row)
    return clean


def empty_feed(generated_at: str) -> dict[str, Any]:
    return {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "generatedAt": generated_at,
        "country": "International",
        "competition": COMPETITION,
        "season": SEASON,
        "matches": [],
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    generated_at = utc_now()
    fixtures = load_fixtures(FIXTURES_PATH)
    debug: dict[str, Any] = {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "scriptVersion": SCRIPT_VERSION,
        "generatedAt": generated_at,
        "betssonUrl": BETSSON_WC_URL,
        "fixturesPath": str(FIXTURES_PATH),
        "outputPath": str(OUTPUT_PATH),
        "debugPath": str(DEBUG_PATH),
        "fixturesLoaded": len(fixtures),
        "matchesMatched": 0,
        "matchesWithMarkets": 0,
        "marketsFound": 0,
        "marketCounts": {},
        "unmatchedFixtures": [],
        "matchedFixtures": [],
        "errors": [],
        "notes": [
            "Probe scraper: output is only as good as Betsson visible/embedded page structure.",
            "No odds are invented. Low-confidence markets are omitted.",
        ],
    }

    if not fixtures:
        feed = empty_feed(generated_at)
        debug["errors"].append("No upcoming fixtures found in StatMaker World Cup JSON")
        write_json(OUTPUT_PATH, feed)
        write_json(DEBUG_PATH, debug)
        return 0

    feed = empty_feed(generated_at)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1400},
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                locale="en-GB",
                timezone_id=LOCAL_TZ,
            )
            page = context.new_page()
            goto(page, BETSSON_WC_URL)
            page_text = visible_text(page)
            json_roots = extract_jsonish_objects(page)
            debug["visibleTextLength"] = len(page_text)
            debug["jsonRootsFound"] = len(json_roots)

            for fixture in fixtures:
                json_markets, json_notes = try_extract_from_json_roots(fixture, json_roots)
                text_markets, text_notes = try_extract_from_visible_text(fixture, page_text)
                markets = dedupe_markets(json_markets + text_markets)
                notes = json_notes + text_notes

                if markets:
                    feed["matches"].append(
                        {
                            "date": fixture.date,
                            "homeTeam": fixture.home,
                            "awayTeam": fixture.away,
                            "markets": markets,
                        }
                    )
                    debug["matchesMatched"] += 1
                    debug["matchesWithMarkets"] += 1
                    debug["marketsFound"] += len(markets)
                    for market in markets:
                        key = market["market"]
                        debug["marketCounts"][key] = debug["marketCounts"].get(key, 0) + 1
                    debug["matchedFixtures"].append(
                        {
                            "matchId": fixture.match_id,
                            "date": fixture.date,
                            "time": fixture.time,
                            "homeTeam": fixture.home,
                            "awayTeam": fixture.away,
                            "markets": len(markets),
                            "notes": notes,
                        }
                    )
                else:
                    debug["unmatchedFixtures"].append(
                        {
                            "matchId": fixture.match_id,
                            "date": fixture.date,
                            "time": fixture.time,
                            "homeTeam": fixture.home,
                            "awayTeam": fixture.away,
                            "notes": notes,
                        }
                    )

            context.close()
            browser.close()
    except PlaywrightTimeoutError as exc:
        debug["errors"].append(f"Playwright timeout: {exc}")
    except Exception as exc:
        debug["errors"].append(f"Unexpected scraper error: {type(exc).__name__}: {exc}")

    write_json(OUTPUT_PATH, feed)
    write_json(DEBUG_PATH, debug)

    # Do not fail the workflow on zero markets during probe phase. The debug
    # report is the artifact we need to decide whether Betsson is scrapeable.
    print(json.dumps({
        "matches": len(feed["matches"]),
        "markets": debug["marketsFound"],
        "debug": str(DEBUG_PATH),
        "output": str(OUTPUT_PATH),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
