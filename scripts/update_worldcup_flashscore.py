#!/usr/bin/env python3
"""
StatMaker World Cup JSON updater.

Reads the public Flashscore World Cup results pages with a headless browser,
extracts finished matches and match statistics, and writes the stable
StatMaker JSON schema consumed by the Android app.

Important:
- This script does not run inside the Android app.
- It does not use tokens or private credentials.
- If Flashscore changes markup or blocks headless browsing, the script should
  fail cleanly rather than inventing data.
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
OUTPUT_PATH = Path(os.getenv("STATMAKER_OUTPUT", "world-cup/world_cup_2026.json"))
COMPETITION = "world_cup"
SEASON = "2026"
SOURCE = "flashscore"

# Flashscore labels vary slightly by locale/site revision. Keep this explicit.
STAT_LABEL_ALIASES = {
    "homePossession": ["ball possession", "possession"],
    "homeShots": ["goal attempts", "total shots", "shots"],
    "homeShotsOnTarget": ["shots on goal", "shots on target"],
    "homeCorners": ["corner kicks", "corners"],
    "homeYellowCards": ["yellow cards", "yellow card"],
    "homeRedCards": ["red cards", "red card"],
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


def first_attr(page: Page, selectors: Iterable[str], attr: str) -> str:
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count() > 0:
                value = locator.get_attribute(attr, timeout=2500)
                if value:
                    return value
        except Exception:
            continue
    return ""


def accept_cookies_if_present(page: Page) -> None:
    labels = [
        "Accept all",
        "I Accept",
        "Accept",
        "Agree",
        "Consent",
    ]
    for label in labels:
        try:
            button = page.get_by_role("button", name=re.compile(label, re.I)).first
            if button.count() > 0:
                button.click(timeout=1500)
                page.wait_for_timeout(1000)
                return
        except Exception:
            continue


def load_all_result_rows(page: Page) -> None:
    """Click Show more matches until the results page is fully expanded."""
    for _ in range(20):
        try:
            show_more = page.get_by_text(re.compile(r"show more", re.I)).first
            if show_more.count() == 0:
                break
            show_more.click(timeout=2500)
            page.wait_for_timeout(1500)
        except Exception:
            break


def collect_match_links(page: Page) -> list[str]:
    links = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a[href*="/match/"]'))
          .map(a => a.href)
          .filter(Boolean)
        """
    )
    normalized: list[str] = []
    seen: set[str] = set()
    for href in links:
        href = str(href).split("#")[0]
        # Keep only football match pages and strip tabs to the summary base.
        if "/match/football/" not in href:
            continue
        href = re.sub(r"/summary.*$", "/summary/", href)
        href = re.sub(r"/match-summary.*$", "/summary/", href)
        if href not in seen:
            seen.add(href)
            normalized.append(href)
    return normalized


def goto(page: Page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)


def parse_score(score_text: str) -> tuple[int | None, int | None]:
    # Handles "2 - 0", "2-0", and multiline detail score wrappers.
    nums = re.findall(r"\d+", score_text)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None, None


def parse_date_time(header_text: str) -> tuple[str | None, str | None]:
    # Flashscore commonly uses DD.MM.YYYY HH:MM. Convert to YYYY-MM-DD.
    match = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})\s+(\d{1,2}:\d{2})", header_text)
    if not match:
        return None, None
    day, month, year, hhmm = match.groups()
    return f"{year}-{int(month):02d}-{int(day):02d}", hhmm


def parse_match_header(page: Page, url: str) -> MatchHeader | None:
    goto(page, url)

    body_text = clean_text(page.locator("body").inner_text(timeout=10000))
    if not body_text:
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

    # Fallback: parse title, e.g. "Mexico v South Africa | ..."
    if not home_team or not away_team:
        title = first_text(page, ["title"])
        match = re.search(r"(.+?)\s+v\s+(.+?)(?:\s+\||,|$)", title, re.I)
        if match:
            home_team = home_team or clean_text(match.group(1))
            away_team = away_team or clean_text(match.group(2))

    home_goals, away_goals = parse_score(score_text)
    date_value, time_value = parse_date_time(start_time_text or body_text)

    if not home_team or not away_team or home_goals is None or away_goals is None:
        print(f"Skipping match; could not parse finished header: {url}", file=sys.stderr)
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
        status="finished",
        venue=None,
    )


def normalize_label(label: str) -> str:
    label = clean_text(label).lower()
    label = label.replace("%", "")
    return re.sub(r"[^a-z0-9 ]+", " ", label).strip()


def match_stat_key(label: str) -> str | None:
    normalized = normalize_label(label)
    for key, aliases in STAT_LABEL_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                return key
    return None


def parse_stat_row_text(text: str) -> tuple[str, int | None, int | None] | None:
    lines = [clean_text(line) for line in text.splitlines() if clean_text(line)]
    if len(lines) < 3:
        return None

    # Most Flashscore rows are: home value / label / away value.
    candidates = [
        (lines[1], lines[0], lines[2]),
        (lines[0], lines[1], lines[2]),
    ]
    for label, home, away in candidates:
        if match_stat_key(label):
            return label, parse_int(home), parse_int(away)
    return None


def parse_stats_from_rows(page: Page) -> dict[str, int | None]:
    result: dict[str, int | None] = {
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

    selectors = [
        "[class*='stat__row']",
        "[class*='wcl-row']",
        "[class*='matchStatsRow']",
        "[data-testid*='stat']",
    ]

    row_texts: list[str] = []
    for selector in selectors:
        try:
            loc = page.locator(selector)
            count = min(loc.count(), 80)
            for index in range(count):
                text = loc.nth(index).inner_text(timeout=2000)
                if text and text not in row_texts:
                    row_texts.append(text)
        except Exception:
            continue

    for text in row_texts:
        parsed = parse_stat_row_text(text)
        if not parsed:
            continue
        label, home_value, away_value = parsed
        home_key = match_stat_key(label)
        if not home_key:
            continue
        away_key = AWAY_KEYS[home_key]
        result[home_key] = home_value
        result[away_key] = away_value

    return result


def stats_url_for(summary_url: str) -> str:
    base = re.sub(r"/summary/?$", "", summary_url.rstrip("/"))
    return base + "/summary/stats/"


def parse_match_stats(page: Page, summary_url: str) -> dict[str, int | None]:
    url = stats_url_for(summary_url)
    try:
        goto(page, url)
    except PlaywrightTimeoutError:
        print(f"Stats page timeout: {url}", file=sys.stderr)
        return {}

    return parse_stats_from_rows(page)


def build_match_json(header: MatchHeader, stats: dict[str, int | None]) -> dict[str, Any]:
    match: dict[str, Any] = {
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
    return match


def load_existing() -> dict[str, Any]:
    if not OUTPUT_PATH.exists():
        return {
            "competition": COMPETITION,
            "season": SEASON,
            "updatedAt": None,
            "source": SOURCE,
            "matches": [],
        }
    with OUTPUT_PATH.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def merge_matches(existing: dict[str, Any], scraped_matches: list[dict[str, Any]]) -> dict[str, Any]:
    # Keep non-finished future fixtures from the previous JSON, replace finished matches by matchId.
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
        load_all_result_rows(page)
        links = collect_match_links(page)
        print(f"Found {len(links)} candidate match links")

        if not links:
            print("No match links found. Failing without changing JSON.", file=sys.stderr)
            browser.close()
            return 2

        for index, link in enumerate(links, start=1):
            print(f"[{index}/{len(links)}] {link}")
            try:
                header = parse_match_header(page, link)
                if header is None:
                    continue
                stats = parse_match_stats(page, link)
                match_json = build_match_json(header, stats)
                scraped_matches.append(match_json)
                # polite delay; don't hammer pages
                time.sleep(1)
            except Exception as exc:
                print(f"Failed to parse match {link}: {exc}", file=sys.stderr)
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
    print(f"Finished matches parsed this run: {len(scraped_matches)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
