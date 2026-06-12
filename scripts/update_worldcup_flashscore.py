#!/usr/bin/env python3
# STATMAKER_FIXED_SCRAPER_V9 - robust existing JSON fallback + scheduled fixtures feed
"""
StatMaker World Cup JSON updater.

Reads the public Flashscore World Cup results page with a headless browser,
extracts recent finished matches, match statistics, and upcoming fixtures,
and writes the stable StatMaker JSON schema consumed by the Android app.

Important:
- This script does not run inside the Android app.
- It does not use tokens or private credentials.
- It never invents missing stats; missing values remain null.
- It deliberately does NOT expand the full Flashscore archive, because that can
  produce hundreds of historical/qualifier links and make the GitHub Action hang.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

RESULTS_URL = os.getenv(
    "FLASHSCORE_RESULTS_URL",
    "https://www.flashscore.com/football/world/world-championship/results/",
)
FIXTURES_URL = os.getenv(
    "FLASHSCORE_FIXTURES_URL",
    "https://www.flashscore.com/football/world/world-championship/fixtures/",
)
OUTPUT_PATH = Path(os.getenv("STATMAKER_OUTPUT", "world-cup/world_cup_2026.json"))
COMPETITION = "world_cup"
SEASON = "2026"
SOURCE = "flashscore"
SCRIPT_VERSION = "stats-plus-fixtures-v8"

# Only inspect the recent visible/current result links. This prevents the Action
# from crawling the whole Flashscore archive.
MAX_CANDIDATE_LINKS = int(os.getenv("MAX_CANDIDATE_LINKS", "8"))
MAX_FIXTURE_LINKS = int(os.getenv("MAX_FIXTURE_LINKS", "32"))
MATCH_DELAY_SECONDS = float(os.getenv("MATCH_DELAY_SECONDS", "0.4"))
TOURNAMENT_START = os.getenv("TOURNAMENT_START", "2026-06-11")
TOURNAMENT_END = os.getenv("TOURNAMENT_END", "2026-07-20")

STAT_LABEL_ALIASES = {
    "homePossession": ["ball possession", "possession"],
    "homeShots": ["goal attempts", "total shots", "shots"],
    "homeShotsOnTarget": ["shots on goal", "shots on target"],
    "homeCorners": ["corner kicks", "corners", "corner"],
    "homeYellowCards": ["yellow cards", "yellow card"],
    "homeRedCards": ["red cards", "red card"],
}

EMPTY_STATS = {
    "homeCorners": None,
    "awayCorners": None,
    "homeShots": None,
    "awayShots": None,
    "homeShotsOnTarget": None,
    "awayShotsOnTarget": None,
    "homeYellowCards": None,
    "awayYellowCards": None,
    "homeRedCards": None,
    "awayRedCards": None,
    "homePossession": None,
    "awayPossession": None,
}

AWAY_KEYS = {
    "homePossession": "awayPossession",
    "homeShots": "awayShots",
    "homeShotsOnTarget": "awayShotsOnTarget",
    "homeCorners": "awayCorners",
    "homeYellowCards": "awayYellowCards",
    "homeRedCards": "awayRedCards",
}


@dataclass(frozen=True)
class MatchHeader:
    match_id: str
    url: str
    date: str | None
    time: str | None
    stage: str | None
    home_team: str
    away_team: str
    home_goals: int | None
    away_goals: int | None
    status: str
    venue: str | None


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_multiline(value: str | None) -> str:
    """
    Normalize whitespace but preserve row line boundaries.

    This matters because Flashscore statistic rows often render as:
      3
      Corner Kicks
      1

    V4 collapsed that into one line, so row parsing lost home/label/away order.
    """
    if not value:
        return ""
    value = str(value).replace("\t", "\n")
    lines = [clean_text(line) for line in value.splitlines() if clean_text(line)]
    return "\n".join(lines)


def parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    value = clean_text(value).replace("%", "")
    match = re.search(r"-?\d+", value)
    if not match:
        return None
    return int(match.group(0))


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "unknown"


def first_text(page: Page, selectors: Iterable[str]) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                text = clean_text(locator.inner_text(timeout=2500))
                if text:
                    return text
        except Exception:
            continue
    return ""


def accept_cookies_if_present(page: Page) -> None:
    labels = ["Accept all", "I Accept", "Accept", "Agree", "Consent"]
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
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)


def normalize_match_url(href: str) -> str | None:
    """
    Return the match URL as Flashscore gives it, cleaned but NOT rewritten to
    /summary/.

    The previous V2 script produced broken URLs such as:
      .../?mid=CGdvIm6K/summary/
    because it appended /summary/ after the query string. That made parsing fail.
    """
    href = clean_text(str(href or ""))
    if not href:
        return None
    href = href.split("#")[0]
    if "/match/football/" not in href:
        return None

    # Remove accidental tab suffixes if they have already been appended.
    href = re.sub(r"/(summary|match-summary|stats|standings|h2h)/?$", "", href.rstrip("/"))

    # If a bad previous query value has something like ?mid=ID/summary/, clean it.
    if "?" in href:
        path, query = href.split("?", 1)
        query = re.sub(r"/(summary|match-summary|stats|standings|h2h).*$", "", query)
        href = path.rstrip("/") + "?" + query

    return href


def collect_visible_match_links(page: Page, require_score: bool, limit: int) -> list[str]:
    """Collect match links from the currently visible Flashscore page only.

    require_score=True is used for the results page. It keeps only played rows.
    require_score=False is used for the fixtures page. It keeps scheduled rows too.
    We deliberately do not click 'Show more', to avoid crawling old qualifiers/archive pages.
    """
    page.wait_for_timeout(2500)
    data = page.evaluate(
        """
        () => {
          const rows = Array.from(document.querySelectorAll('.event__match, [id^="g_1_"]'));
          const out = [];
          for (const row of rows) {
            const text = (row.innerText || '').trim();
            const homeScore = row.querySelector('.event__score--home')?.textContent?.trim() || '';
            const awayScore = row.querySelector('.event__score--away')?.textContent?.trim() || '';
            const link = row.querySelector('a[href*="/match/"]')?.href || '';
            out.push({ text, homeScore, awayScore, link });
          }
          if (out.length === 0) {
            return Array.from(document.querySelectorAll('a[href*="/match/"]')).map(a => ({
              text: a.innerText || '',
              homeScore: '',
              awayScore: '',
              link: a.href || ''
            }));
          }
          return out;
        }
        """
    )

    links: list[str] = []
    seen: set[str] = set()
    for item in data:
        href = normalize_match_url(str(item.get("link") or ""))
        if not href or href in seen:
            continue

        home_score = clean_text(str(item.get("homeScore") or ""))
        away_score = clean_text(str(item.get("awayScore") or ""))
        row_text = clean_text(str(item.get("text") or ""))
        has_score_pair = bool(re.search(r"\d", home_score) and re.search(r"\d", away_score))
        has_inline_score = bool(re.search(r"\b\d+\s*-\s*\d+\b", row_text))
        if require_score and not has_score_pair and not has_inline_score:
            continue

        seen.add(href)
        links.append(href)
        if len(links) >= limit:
            break

    return links


def collect_visible_finished_match_links(page: Page) -> list[str]:
    return collect_visible_match_links(page, require_score=True, limit=MAX_CANDIDATE_LINKS)


def collect_visible_fixture_match_links(page: Page) -> list[str]:
    return collect_visible_match_links(page, require_score=False, limit=MAX_FIXTURE_LINKS)


def parse_score(score_text: str) -> tuple[int | None, int | None]:
    nums = re.findall(r"\d+", score_text)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None, None


def parse_date_time(header_text: str) -> tuple[str | None, str | None]:
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}:\d{2})", header_text)
    if not match:
        return None, None
    day, month, year, hhmm = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}", hhmm


def in_tournament_window(date_value: str | None) -> bool:
    if not date_value:
        # Do not drop a match only because Flashscore date markup changed.
        return True
    return TOURNAMENT_START <= date_value <= TOURNAMENT_END


def parse_match_header(page: Page, url: str, require_score: bool = True) -> MatchHeader | None:
    goto(page, url)

    title_text = ""
    try:
        title_text = clean_text(page.title())
    except Exception:
        title_text = ""

    body_text = clean_text(page.locator("body").inner_text(timeout=10000))
    if not body_text and not title_text:
        return None

    home_team = first_text(
        page,
        [
            ".duelParticipant__home .participant__participantName",
            "[class*='duelParticipant__home'] [class*='participant__participantName']",
            "[class*='homeParticipant'] [class*='participantName']",
        ],
    )
    away_team = first_text(
        page,
        [
            ".duelParticipant__away .participant__participantName",
            "[class*='duelParticipant__away'] [class*='participant__participantName']",
            "[class*='awayParticipant'] [class*='participantName']",
        ],
    )
    score_text = first_text(
        page,
        [
            ".detailScore__wrapper",
            "[class*='detailScore__wrapper']",
            "[class*='detailScore']",
        ],
    )
    start_time_text = first_text(
        page,
        [
            ".duelParticipant__startTime",
            "[class*='duelParticipant__startTime']",
            "[class*='startTime']",
        ],
    )

    if not home_team or not away_team:
        match = re.search(r"(.+?)\s+v\s+(.+?)(?:\s+\||,|$)", title_text, re.I)
        if match:
            home_team = home_team or clean_text(match.group(1))
            away_team = away_team or clean_text(match.group(2))

    if not home_team or not away_team:
        # Last-resort fallback from Flashscore URL slug. It is not perfect, but
        # prevents a full failure if CSS class names change.
        m = re.search(r"/match/football/([^/?#]+)/([^/?#]+)", url)
        if m:
            def team_from_slug(slug: str) -> str:
                slug = re.sub(r"-[A-Za-z0-9]{6,}$", "", slug)
                return " ".join(part.capitalize() for part in slug.split("-") if part)
            home_team = home_team or team_from_slug(m.group(1))
            away_team = away_team or team_from_slug(m.group(2))

    home_goals, away_goals = parse_score(score_text)
    date_value, time_value = parse_date_time(start_time_text or body_text)

    if not home_team or not away_team:
        print(f"Skipping match; could not parse teams: {url}", file=sys.stderr)
        return None

    if require_score and (home_goals is None or away_goals is None):
        print(f"Skipping match; could not parse finished score: {url}", file=sys.stderr)
        return None

    if not in_tournament_window(date_value):
        print(f"Skipping outside tournament window: {date_value} {home_team} - {away_team}")
        return None

    match_id = "wc2026_" + slugify(f"{home_team}_{away_team}")

    return MatchHeader(
        match_id=match_id,
        url=url,
        date=date_value,
        time=time_value,
        stage=None,
        home_team=home_team,
        away_team=away_team,
        home_goals=home_goals,
        away_goals=away_goals,
        status="finished" if home_goals is not None and away_goals is not None else "scheduled",
        venue=None,
    )


def normalize_label(label: str) -> str:
    label = clean_text(label).lower()
    label = label.replace("%", "")
    return re.sub(r"[^a-z0-9 ]+", " ", label).strip()


def match_stat_key(label: str) -> str | None:
    normalized = normalize_label(label)

    # Avoid false positives from combined Flashscore text blocks such as:
    #   "1 Yellow Cards 2 Red Cards"
    # These are not a single reliable stat row. Red cards must come from an
    # explicit "Red Cards" row with its own numeric values; otherwise they stay null.
    has_yellow = "yellow card" in normalized or "yellow cards" in normalized
    has_red = "red card" in normalized or "red cards" in normalized
    if has_yellow and has_red:
        return None

    # Match more specific labels first. "shots on target" must win before "shots".
    priority = [
        "homePossession",
        "homeShotsOnTarget",
        "homeShots",
        "homeCorners",
        "homeYellowCards",
        "homeRedCards",
    ]
    for key in priority:
        for alias in STAT_LABEL_ALIASES[key]:
            if alias in normalized:
                return key
    return None


def default_stats() -> dict[str, int | None]:
    return dict(EMPTY_STATS)


def parse_stat_row_text(text: str) -> tuple[str, int | None, int | None] | None:
    lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
    if len(lines) < 3:
        return None

    # Flashscore rows usually render as one of these forms:
    #   home value / label / away value
    #   label / home value / away value
    # The CSS classes change often, so this parser is intentionally text-based.
    candidates = [
        (lines[1], lines[0], lines[2]),
        (lines[0], lines[1], lines[2]),
        (lines[-2], lines[0], lines[-1]),
    ]
    for label, home, away in candidates:
        if match_stat_key(label):
            return label, parse_int(home), parse_int(away)
    return None


def merge_stat_value(
    stats: dict[str, int | None],
    label: str,
    home_value: int | None,
    away_value: int | None,
) -> None:
    home_key = match_stat_key(label)
    if not home_key:
        return
    away_key = AWAY_KEYS[home_key]
    if home_value is not None:
        stats[home_key] = home_value
    if away_value is not None:
        stats[away_key] = away_value


def parse_stats_from_visible_rows(page: Page) -> dict[str, int | None]:
    stats = default_stats()

    row_texts: list[str] = []
    selectors = [
        "[class*='stat__row']",
        "[class*='wcl-row']",
        "[class*='wcl-category']",
        "[class*='matchStats'] [class*='row']",
        "[class*='matchStatsRow']",
        "[data-testid*='stat']",
        "[class*='statistics'] [class*='row']",
    ]

    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = min(loc.count(), 250)
            for index in range(count):
                text = normalize_multiline(loc.nth(index).inner_text(timeout=1500))
                if text and text not in row_texts:
                    row_texts.append(text)
        except Exception:
            continue

    # Last-resort DOM scan: take elements whose text contains a known stat label
    # and parse them as home/label/away mini-blocks.
    try:
        extra_rows = page.evaluate(
            """
            () => {
              const aliases = [
                'Ball Possession', 'Possession', 'Goal Attempts', 'Total Shots',
                'Shots', 'Shots on Goal', 'Shots on Target', 'Corner Kicks',
                'Corners', 'Yellow Cards', 'Red Cards'
              ];
              const out = [];
              const els = Array.from(document.querySelectorAll('div, span'));
              for (const el of els) {
                const txt = (el.innerText || '').trim();
                if (!txt || txt.length > 180) continue;
                const lower = txt.toLowerCase();
                if (aliases.some(a => lower.includes(a.toLowerCase()))) out.push(txt);
              }
              return out;
            }
            """
        )
        for text in extra_rows:
            cleaned = normalize_multiline(str(text))
            if cleaned and cleaned not in row_texts:
                row_texts.append(cleaned)
    except Exception:
        pass

    if row_texts:
        print(f"Collected {len(row_texts)} candidate stat row texts")

    for text in row_texts:
        parsed = parse_stat_row_text(text)
        if not parsed:
            continue
        label, home_value, away_value = parsed
        merge_stat_value(stats, label, home_value, away_value)

    return stats


def parse_stats_from_body_text(page: Page) -> dict[str, int | None]:
    stats = default_stats()

    try:
        body = page.locator("body").inner_text(timeout=8000)
    except Exception:
        return stats

    # Form 1: home value / label / away value across lines.
    lines = [clean_text(x) for x in body.splitlines() if clean_text(x)]
    for i in range(1, len(lines) - 1):
        label = lines[i]
        key = match_stat_key(label)
        if not key:
            continue
        # Red cards often appear as navigation/filter text near yellow-card numbers.
        # Do not infer them from the whole body text; accept them only from actual
        # visible stat rows parsed by parse_stats_from_visible_rows().
        if key == "homeRedCards":
            continue
        home_value = parse_int(lines[i - 1])
        away_value = parse_int(lines[i + 1])
        merge_stat_value(stats, label, home_value, away_value)

    # Form 2: after whitespace collapse, "57% Ball Possession 43%".
    flat = clean_text(body)
    for home_key, aliases in STAT_LABEL_ALIASES.items():
        if home_key == "homeRedCards":
            continue
        for alias in aliases:
            pattern = re.compile(
                rf"(\d+%?)\s+{re.escape(alias)}\s+(\d+%?)",
                re.IGNORECASE,
            )
            match = pattern.search(flat)
            if match:
                away_key = AWAY_KEYS[home_key]
                stats[home_key] = parse_int(match.group(1))
                stats[away_key] = parse_int(match.group(2))
                break

    return stats


def combine_stats(*parts: dict[str, int | None]) -> dict[str, int | None]:
    combined = default_stats()
    for part in parts:
        for key, value in part.items():
            if value is not None:
                combined[key] = value
    return combined


def stats_found_count(stats: dict[str, int | None]) -> int:
    return sum(1 for value in stats.values() if value is not None)


def click_statistics_tab(page: Page) -> None:
    """Try to force Flashscore onto the detailed statistics panel."""
    candidates = [
        "a[href*='match-statistics']",
        "button:has-text('Stats')",
        "a:has-text('Stats')",
        "[role='tab']:has-text('Stats')",
        "text=/^Stats$/",
        "text=/^Statistics$/",
        "text=/^Match statistics$/i",
    ]
    for selector in candidates:
        try:
            loc = page.locator(selector).first
            if loc.count() > 0 and loc.is_visible(timeout=1000):
                loc.click(timeout=2500)
                page.wait_for_timeout(2500)
                return
        except Exception:
            continue


def stats_urls_for(match_url: str) -> list[str]:
    """
    Flashscore stat tabs are hash-routed. Try a few stable variants.
    """
    clean_url = match_url.split("#")[0].rstrip("/")
    if "?" in clean_url:
        path, query = clean_url.split("?", 1)
        path = path.rstrip("/")
        with_query = path + "?" + query
    else:
        path = clean_url.rstrip("/")
        with_query = path

    candidates = [
        with_query + "#/match-summary/match-statistics/0",
        with_query + "#/match-summary/match-statistics",
        path + "/#/match-summary/match-statistics/0",
        path + "/#/match-summary/match-statistics",
        with_query + "#/match-summary",
        path + "/#/match-summary",
    ]

    out: list[str] = []
    for item in candidates:
        if item not in out:
            out.append(item)
    return out


def parse_match_stats(page: Page, match_url: str) -> dict[str, int | None]:
    best = default_stats()

    for url in stats_urls_for(match_url):
        try:
            goto(page, url)
            page.wait_for_timeout(1500)
            click_statistics_tab(page)
            row_stats = parse_stats_from_visible_rows(page)
            body_stats = parse_stats_from_body_text(page)
            stats = combine_stats(row_stats, body_stats)
            if stats_found_count(stats) > stats_found_count(best):
                best = stats
            if stats_found_count(best) >= 8:
                return best
        except PlaywrightTimeoutError:
            print(f"Stats page timeout: {url}", file=sys.stderr)
        except Exception as exc:
            print(f"Stats page parse failed: {url}: {exc}", file=sys.stderr)

    return best


def build_match_json(header: MatchHeader, stats: dict[str, int | None]) -> dict[str, Any]:
    return {
        "matchId": header.match_id,
        "date": header.date,
        "time": header.time,
        "stage": header.stage,
        "status": header.status,
        "homeTeam": header.home_team,
        "awayTeam": header.away_team,
        "homeGoals": header.home_goals,
        "awayGoals": header.away_goals,
        "homeCorners": stats.get("homeCorners"),
        "awayCorners": stats.get("awayCorners"),
        "homeShots": stats.get("homeShots"),
        "awayShots": stats.get("awayShots"),
        "homeShotsOnTarget": stats.get("homeShotsOnTarget"),
        "awayShotsOnTarget": stats.get("awayShotsOnTarget"),
        "homeYellowCards": stats.get("homeYellowCards"),
        "awayYellowCards": stats.get("awayYellowCards"),
        "homeRedCards": stats.get("homeRedCards"),
        "awayRedCards": stats.get("awayRedCards"),
        "homePossession": stats.get("homePossession"),
        "awayPossession": stats.get("awayPossession"),
        "venue": header.venue,
    }


def empty_document() -> dict[str, Any]:
    return {
        "competition": COMPETITION,
        "season": SEASON,
        "updatedAt": None,
        "source": SOURCE,
        "matches": [],
    }


def load_existing() -> dict[str, Any]:
    if not OUTPUT_PATH.exists():
        return empty_document()

    try:
        if OUTPUT_PATH.stat().st_size == 0:
            print(f"Existing {OUTPUT_PATH} is empty. Rebuilding it from scraped data.", file=sys.stderr)
            return empty_document()
        with OUTPUT_PATH.open("r", encoding="utf-8") as fp:
            data = json.load(fp)
        if not isinstance(data, dict):
            print(f"Existing {OUTPUT_PATH} is not a JSON object. Rebuilding it from scraped data.", file=sys.stderr)
            return empty_document()
        if not isinstance(data.get("matches"), list):
            data["matches"] = []
        return data
    except json.JSONDecodeError as exc:
        print(f"Existing {OUTPUT_PATH} is invalid JSON: {exc}. Rebuilding it from scraped data.", file=sys.stderr)
        return empty_document()


def merge_matches(existing: dict[str, Any], scraped_matches: list[dict[str, Any]]) -> dict[str, Any]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in existing.get("matches", []):
        if isinstance(item, dict) and item.get("matchId"):
            by_id[str(item["matchId"])] = item
    for item in scraped_matches:
        by_id[str(item["matchId"])] = item

    def sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(item.get("date") or "9999-99-99"),
            str(item.get("time") or "99:99"),
            str(item.get("matchId") or ""),
        )

    return {
        "competition": COMPETITION,
        "season": SEASON,
        "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": SOURCE,
        "matches": sorted(by_id.values(), key=sort_key),
    }


def main() -> int:
    scraped_matches: list[dict[str, Any]] = []
    scraped_fixture_count = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="UTC",
        )
        page = context.new_page()

        print(f"Opening results page: {RESULTS_URL}")
        goto(page, RESULTS_URL)
        accept_cookies_if_present(page)

        links = collect_visible_finished_match_links(page)
        print(f"Found {len(links)} recent finished candidate match links")

        if not links:
            print("No recent finished match links found. Failing without changing JSON.", file=sys.stderr)
            browser.close()
            return 2

        for index, link in enumerate(links, start=1):
            print(f"[{index}/{len(links)} finished] {link}")
            try:
                header = parse_match_header(page, link, require_score=True)
                if header is None:
                    continue
                stats = parse_match_stats(page, link)
                print(
                    f"Parsed stats for {header.home_team} - {header.away_team}: "
                    f"corners={stats.get('homeCorners')}-{stats.get('awayCorners')}, "
                    f"shots={stats.get('homeShots')}-{stats.get('awayShots')}, "
                    f"sot={stats.get('homeShotsOnTarget')}-{stats.get('awayShotsOnTarget')}, "
                    f"yc={stats.get('homeYellowCards')}-{stats.get('awayYellowCards')}, "
                    f"rc={stats.get('homeRedCards')}-{stats.get('awayRedCards')}, "
                    f"pos={stats.get('homePossession')}-{stats.get('awayPossession')}"
                )
                scraped_matches.append(build_match_json(header, stats))
                time.sleep(MATCH_DELAY_SECONDS)
            except Exception as exc:
                print(f"Failed to parse finished match {link}: {exc}", file=sys.stderr)
                continue

        print(f"Opening fixtures page: {FIXTURES_URL}")
        goto(page, FIXTURES_URL)
        accept_cookies_if_present(page)
        fixture_links = collect_visible_fixture_match_links(page)
        print(f"Found {len(fixture_links)} visible fixture candidate links")

        existing_ids = {str(item.get("matchId")) for item in scraped_matches if item.get("matchId")}
        for index, link in enumerate(fixture_links, start=1):
            print(f"[{index}/{len(fixture_links)} fixture] {link}")
            try:
                header = parse_match_header(page, link, require_score=False)
                if header is None:
                    continue
                item = build_match_json(header, default_stats())
                if str(item.get("matchId")) in existing_ids:
                    continue
                scraped_matches.append(item)
                existing_ids.add(str(item.get("matchId")))
                scraped_fixture_count += 1
                time.sleep(MATCH_DELAY_SECONDS)
            except Exception as exc:
                print(f"Failed to parse fixture {link}: {exc}", file=sys.stderr)
                continue

        browser.close()

    if not scraped_matches:
        print("No finished matches parsed. Failing without changing JSON.", file=sys.stderr)
        return 3

    existing = load_existing()
    updated = merge_matches(existing, scraped_matches)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as fp:
        json.dump(updated, fp, ensure_ascii=False, indent=2)
        fp.write("\n")

    print(f"Wrote {OUTPUT_PATH} with {len(updated['matches'])} total matches")
    finished_count = sum(1 for item in scraped_matches if item.get("status") == "finished")
    print(f"Finished matches parsed this run: {finished_count}")
    print(f"Fixtures parsed this run: {scraped_fixture_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
