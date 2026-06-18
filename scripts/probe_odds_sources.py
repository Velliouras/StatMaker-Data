#!/usr/bin/env python3
"""
StatMaker international odds source probe.

Purpose:
- Runs outside the Android app, from GitHub Actions.
- Probes multiple international odds/bookmaker/odds-listing pages.
- Detects whether GitHub Actions can access the page, whether useful football/WC text is visible,
  and whether script/API candidates exist.
- Does not produce consumable app odds. This is diagnostic only.

The output helps decide which source is worth turning into a real scraper.
"""

from __future__ import annotations

import json
import os
import re
import textwrap
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

FIXTURES_PATH = Path(os.getenv("STATMAKER_WC_FIXTURES", "world-cup/world_cup_2026.json"))
REPORT_PATH = Path(os.getenv("STATMAKER_ODDS_PROBE_REPORT", "odds/probe/odds_sources_report.json"))
SNAPSHOT_DIR = Path(os.getenv("STATMAKER_ODDS_PROBE_SNAPSHOTS", "odds/probe/source_snapshots"))
TIMEOUT_MS = int(os.getenv("STATMAKER_ODDS_PROBE_TIMEOUT_MS", "18000"))

KEYWORDS = [
    "football",
    "soccer",
    "world cup",
    "fifa",
    "odds",
    "market",
    "event",
    "coupon",
    "sportsbook",
    "match",
]

# Keep this list practical. The goal is to find one source that is not blocked by GitHub Actions,
# not to scrape every bookmaker in the world.
SOURCES = [
    {
        "id": "pinnacle",
        "name": "Pinnacle",
        "kind": "bookmaker",
        "urls": [
            "https://www.pinnacle.com/en/soccer/matchups",
            "https://www.pinnacle.com/en/soccer/fifa-world-cup/matchups",
        ],
    },
    {
        "id": "betfair",
        "name": "Betfair",
        "kind": "exchange/bookmaker",
        "urls": [
            "https://www.betfair.com/sport/football",
            "https://sports.betfair.com/sport/football",
        ],
    },
    {
        "id": "william_hill",
        "name": "William Hill",
        "kind": "bookmaker",
        "urls": [
            "https://sports.williamhill.com/betting/en-gb/football",
            "https://www.williamhill.com/betting/en-gb/football",
        ],
    },
    {
        "id": "bwin",
        "name": "Bwin",
        "kind": "bookmaker",
        "urls": [
            "https://sports.bwin.com/en/sports/football-4/betting",
            "https://sports.bwin.com/en/sports/football-4",
        ],
    },
    {
        "id": "unibet",
        "name": "Unibet",
        "kind": "bookmaker",
        "urls": [
            "https://www.unibet.com/betting/sports/filter/football",
            "https://www.unibet.com/betting/sports/home",
        ],
    },
    {
        "id": "betvictor",
        "name": "BetVictor",
        "kind": "bookmaker",
        "urls": [
            "https://www.betvictor.com/en-gb/sports/football",
        ],
    },
    {
        "id": "paddypower",
        "name": "Paddy Power",
        "kind": "bookmaker",
        "urls": [
            "https://www.paddypower.com/football",
        ],
    },
    {
        "id": "oddschecker",
        "name": "Oddschecker",
        "kind": "odds-comparison",
        "urls": [
            "https://www.oddschecker.com/football",
        ],
    },
]


@dataclass
class Fixture:
    match_id: str
    date: str
    time: str
    home_team: str
    away_team: str


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("_").lower() or "source"


def load_fixtures() -> list[Fixture]:
    if not FIXTURES_PATH.exists():
        return []
    data = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    fixtures: list[Fixture] = []
    for item in data.get("matches", []):
        status = str(item.get("status", "")).lower().strip()
        # We care about upcoming fixtures for pre-match odds.
        if status in {"finished", "complete", "completed", "ft"}:
            continue
        home = str(item.get("homeTeam") or item.get("team1") or "").strip()
        away = str(item.get("awayTeam") or item.get("team2") or "").strip()
        if not home or not away:
            continue
        fixtures.append(
            Fixture(
                match_id=str(item.get("matchId") or item.get("id") or f"{home}_{away}"),
                date=str(item.get("date") or ""),
                time=str(item.get("time") or ""),
                home_team=home,
                away_team=away,
            )
        )
    return fixtures


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def contains_team(text: str, team: str) -> bool:
    if not team:
        return False
    norm_text = normalize_text(text)
    norm_team = normalize_text(team)
    if norm_team in norm_text:
        return True
    # Light aliases for common naming differences.
    aliases = {
        "united states": ["usa", "u.s.a", "u.s."],
        "south korea": ["korea republic", "korea rep", "republic of korea"],
        "czech republic": ["czechia"],
        "dr congo": ["congo dr", "democratic republic of congo", "d.r. congo"],
        "ivory coast": ["cote d'ivoire", "côte d’ivoire", "cote divoire"],
        "bosnia & herzegovina": ["bosnia and herzegovina", "bosnia-herzegovina", "bosnia"],
        "cape verde": ["cabo verde"],
        "curacao": ["curaçao"],
    }
    for alias in aliases.get(norm_team, []):
        if alias in norm_text:
            return True
    return False


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


def count_odds_like_numbers(text: str) -> int:
    # Decimal odds usually look like 1.40, 2.05, 11.00 etc.
    matches = re.findall(r"(?<!\d)(?:1\.0[1-9]|1\.[1-9]\d|[2-9]\.[0-9]{2}|[1-9][0-9]\.[0-9]{2})(?!\d)", text or "")
    return len(matches)


def extract_candidate_urls(html: str, base_url: str) -> list[str]:
    candidates: set[str] = set()
    for raw in re.findall(r"(?:src|href)=[\"']([^\"']+)[\"']", html or "", re.IGNORECASE):
        url = urljoin(base_url, raw)
        lower = url.lower()
        if any(token in lower for token in ["api", "event", "market", "coupon", "odds", "sports", "prematch", "graphql", "json", "chunk", "static", "js"]):
            candidates.add(url)
    # Also scan inline text for full URLs that smell like endpoints.
    for raw in re.findall(r"https?://[^\"'<>\\\s]+", html or ""):
        lower = raw.lower()
        if any(token in lower for token in ["api", "event", "market", "coupon", "odds", "sports", "prematch", "graphql", "json"]):
            candidates.add(raw)
    return sorted(candidates)[:80]


def fixture_presence_sample(visible_text: str, html: str, fixtures: list[Fixture], limit: int = 12) -> list[dict[str, Any]]:
    sample: list[dict[str, Any]] = []
    for fx in fixtures[:limit]:
        home_v = contains_team(visible_text, fx.home_team)
        away_v = contains_team(visible_text, fx.away_team)
        home_h = contains_team(html, fx.home_team)
        away_h = contains_team(html, fx.away_team)
        sample.append(
            {
                "matchId": fx.match_id,
                "date": fx.date,
                "time": fx.time,
                "homeTeam": fx.home_team,
                "awayTeam": fx.away_team,
                "homeInVisibleText": home_v,
                "awayInVisibleText": away_v,
                "homeInHtml": home_h,
                "awayInHtml": away_h,
                "bothInVisibleText": home_v and away_v,
                "bothInHtml": home_h and away_h,
            }
        )
    return sample


def classify_status(http_status: int | None, final_url: str, title: str, visible_text: str, html: str) -> str:
    combined = normalize_text(" ".join([final_url or "", title or "", visible_text or "", html[:3000] or ""]))
    if http_status in {401, 403, 451}:
        return "blocked"
    if "country" in combined and "block" in combined:
        return "blocked"
    if "not available" in combined or "access denied" in combined or "forbidden" in combined:
        return "blocked"
    if http_status and http_status >= 500:
        return "server_error"
    if http_status and http_status >= 400:
        return "http_error"
    if len(normalize_text(visible_text)) < 300:
        return "empty_or_js_only"
    return "visible"


def score_result(result: dict[str, Any]) -> int:
    score = 0
    if result.get("classification") == "visible":
        score += 40
    if result.get("classification") == "empty_or_js_only":
        score += 10
    score += min(int(result.get("oddsLikeNumbersInVisibleText", 0)), 50)
    score += min(int(result.get("scriptApiCandidatesCount", 0)), 30)
    if result.get("fixturesBothTeamsInVisibleText", 0):
        score += 60
    if result.get("fixturesBothTeamsInHtml", 0):
        score += 30
    if result.get("classification") == "blocked":
        score -= 80
    return score


def write_snapshot(path: Path, payload: dict[str, Any], visible_text: str, html: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text_sample = (visible_text or "")[:12000]
    html_sample = (html or "")[:12000]
    lines = [
        f"StatMaker odds source probe snapshot: {payload.get('sourceName')} ({payload.get('sourceId')})",
        "",
        json.dumps({k: v for k, v in payload.items() if k not in {"fixturePresenceSample", "keywordPresence", "scriptApiCandidates"}}, ensure_ascii=False, indent=2),
        "",
        "===== Keyword presence =====",
        json.dumps(payload.get("keywordPresence", {}), ensure_ascii=False, indent=2),
        "",
        "===== Fixture presence sample =====",
        json.dumps(payload.get("fixturePresenceSample", []), ensure_ascii=False, indent=2),
        "",
        "===== Script/API candidates =====",
        "\n".join(payload.get("scriptApiCandidates", [])) or "None found.",
        "",
        "===== Visible text sample =====",
        text_sample or "<empty>",
        "",
        "===== HTML sample =====",
        html_sample or "<empty>",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def probe_single_source(page, source: dict[str, Any], fixtures: list[Fixture]) -> dict[str, Any]:
    attempts: list[dict[str, Any]] = []
    best: dict[str, Any] | None = None
    best_visible = ""
    best_html = ""

    for url in source["urls"]:
        response_status: int | None = None
        final_url = url
        title = ""
        visible_text = ""
        html = ""
        error: str | None = None
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            response_status = response.status if response else None
            page.wait_for_timeout(2500)
            title = page.title() or ""
            final_url = page.url or url
            visible_text = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
            html = page.content()
        except PlaywrightTimeoutError as exc:
            error = f"timeout: {exc}"
        except Exception as exc:  # noqa: BLE001 - diagnostics should capture any site-specific failure.
            error = f"{type(exc).__name__}: {exc}"

        fixture_sample = fixture_presence_sample(visible_text, html, fixtures)
        result = {
            "requestedUrl": url,
            "finalUrl": final_url,
            "httpStatus": response_status,
            "title": title,
            "classification": classify_status(response_status, final_url, title, visible_text, html),
            "visibleTextLength": len(visible_text or ""),
            "htmlLength": len(html or ""),
            "oddsLikeNumbersInVisibleText": count_odds_like_numbers(visible_text),
            "oddsLikeNumbersInHtml": count_odds_like_numbers(html),
            "scriptApiCandidatesCount": len(extract_candidate_urls(html, final_url)),
            "fixturesBothTeamsInVisibleText": sum(1 for item in fixture_sample if item["bothInVisibleText"]),
            "fixturesBothTeamsInHtml": sum(1 for item in fixture_sample if item["bothInHtml"]),
            "error": error,
        }
        result["score"] = score_result(result)
        attempts.append(result)

        if best is None or result["score"] > best.get("score", -9999):
            best = result
            best_visible = visible_text
            best_html = html

    assert best is not None
    script_candidates = extract_candidate_urls(best_html, best.get("finalUrl") or "")
    fixture_sample = fixture_presence_sample(best_visible, best_html, fixtures)
    enriched = {
        "sourceId": source["id"],
        "sourceName": source["name"],
        "kind": source["kind"],
        "bestUrl": best.get("requestedUrl"),
        "bestFinalUrl": best.get("finalUrl"),
        "httpStatus": best.get("httpStatus"),
        "title": best.get("title"),
        "classification": best.get("classification"),
        "score": best.get("score"),
        "visibleTextLength": best.get("visibleTextLength"),
        "htmlLength": best.get("htmlLength"),
        "oddsLikeNumbersInVisibleText": best.get("oddsLikeNumbersInVisibleText"),
        "oddsLikeNumbersInHtml": best.get("oddsLikeNumbersInHtml"),
        "fixturesBothTeamsInVisibleText": best.get("fixturesBothTeamsInVisibleText"),
        "fixturesBothTeamsInHtml": best.get("fixturesBothTeamsInHtml"),
        "scriptApiCandidatesCount": len(script_candidates),
        "attempts": attempts,
        "keywordPresence": keyword_presence(best_visible, best_html),
        "fixturePresenceSample": fixture_sample,
        "scriptApiCandidates": script_candidates,
        "notes": [],
    }
    if enriched["classification"] == "blocked":
        enriched["notes"].append("Source appears blocked/unavailable from GitHub Actions.")
    elif enriched["fixturesBothTeamsInVisibleText"] == 0 and enriched["fixturesBothTeamsInHtml"] == 0:
        enriched["notes"].append("No StatMaker WC fixture teams found in visible text or HTML for sampled fixtures.")
    elif enriched["fixturesBothTeamsInVisibleText"] == 0 and enriched["fixturesBothTeamsInHtml"] > 0:
        enriched["notes"].append("Teams appear in HTML but not visible text; likely JS/data extraction needed.")
    elif enriched["fixturesBothTeamsInVisibleText"] > 0:
        enriched["notes"].append("At least one sampled fixture has both teams visible; source may be usable.")
    if enriched["scriptApiCandidatesCount"] > 0:
        enriched["notes"].append("Script/API-like URLs found; inspect source snapshot for candidate endpoints.")

    write_snapshot(SNAPSHOT_DIR / f"{safe_slug(source['id'])}.txt", enriched, best_visible, best_html)
    return enriched


def main() -> None:
    generated_at = now_iso()
    fixtures = load_fixtures()
    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    errors: list[str] = []

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
        for source in SOURCES:
            try:
                results.append(probe_single_source(page, source, fixtures))
            except Exception as exc:  # noqa: BLE001 - continue probing other sources.
                errors.append(f"{source.get('id')}: {type(exc).__name__}: {exc}")
        context.close()
        browser.close()

    ranked = sorted(results, key=lambda item: int(item.get("score") or 0), reverse=True)
    usable = [
        item for item in ranked
        if item.get("classification") in {"visible", "empty_or_js_only"}
        and (
            int(item.get("fixturesBothTeamsInVisibleText") or 0) > 0
            or int(item.get("fixturesBothTeamsInHtml") or 0) > 0
            or int(item.get("scriptApiCandidatesCount") or 0) > 0
        )
        and int(item.get("score") or 0) > 0
    ]

    report = {
        "source": "international_odds_probe",
        "generatedAt": generated_at,
        "scriptVersion": "odds-source-probe-v1",
        "fixturesPath": str(FIXTURES_PATH),
        "fixturesLoaded": len(fixtures),
        "sourcesTested": len(SOURCES),
        "usableCandidates": [
            {
                "sourceId": item["sourceId"],
                "sourceName": item["sourceName"],
                "classification": item["classification"],
                "score": item["score"],
                "bestUrl": item["bestUrl"],
                "bestFinalUrl": item["bestFinalUrl"],
                "fixturesBothTeamsInVisibleText": item["fixturesBothTeamsInVisibleText"],
                "fixturesBothTeamsInHtml": item["fixturesBothTeamsInHtml"],
                "scriptApiCandidatesCount": item["scriptApiCandidatesCount"],
                "notes": item["notes"],
            }
            for item in usable[:5]
        ],
        "results": ranked,
        "errors": errors,
        "notes": [
            "Diagnostic only. Do not consume this file from the Android app.",
            "A source is not selected for production until its odds can be reliably mapped to StatMaker fixtures and markets.",
        ],
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # Small index for quick browsing in GitHub UI.
    index_lines = [
        "StatMaker odds probe snapshots",
        "",
        "Generated by scripts/probe_odds_sources.py.",
        "Open odds/probe/odds_sources_report.json first, then inspect per-source .txt files here.",
        "",
    ]
    for item in ranked:
        index_lines.append(
            f"- {item['sourceName']} ({item['sourceId']}): {item['classification']}, "
            f"score={item['score']}, snapshot={safe_slug(item['sourceId'])}.txt"
        )
    (SNAPSHOT_DIR / "README.txt").write_text("\n".join(index_lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
