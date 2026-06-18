#!/usr/bin/env python3
"""
StatMaker ElaBet World Cup odds probe.

Purpose:
- Runs outside the Android app.
- Reads StatMaker's World Cup fixture JSON.
- Opens ElaBet sportsbook candidate pages with Playwright.
- Checks whether GitHub Actions can see upcoming World Cup teams/odds.
- Writes diagnostic output and a conservative odds JSON.

This is a probe, not a production scraper. It never invents odds. If it cannot
confidently match a market to a StatMaker fixture, it reports that in the debug
files and leaves the market out.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright

DEFAULT_URLS = [
    "https://elabet.gr/gr/sport",
    "https://elabet.gr/en/sport",
    "https://www.elabet.gr/gr/sport",
    "https://www.elabet.gr/en/sport",
]

FIXTURES_PATH = Path(os.getenv("STATMAKER_WC_FIXTURES", "world-cup/world_cup_2026.json"))
OUTPUT_PATH = Path(os.getenv("STATMAKER_ELABET_WC_ODDS_OUTPUT", "odds/elabet/world_cup_odds.json"))
DEBUG_PATH = Path(os.getenv("STATMAKER_ELABET_WC_ODDS_DEBUG", "odds/elabet/debug_report.json"))
SNAPSHOT_PATH = Path(os.getenv("STATMAKER_ELABET_PROBE_SNAPSHOT", "odds/elabet/elabet_probe_snapshot.txt"))
BOOKMAKER = "ElaBet"
SOURCE = "elabet_probe"
COMPETITION = "World Cup"
SEASON = "2026"
SCRIPT_VERSION = "elabet-wc-odds-probe-v1"
MAX_FIXTURES = int(os.getenv("STATMAKER_WC_ODDS_MAX_FIXTURES", "64"))
MIN_ODD = float(os.getenv("STATMAKER_MIN_VALID_ODD", "1.01"))
MAX_ODD = float(os.getenv("STATMAKER_MAX_VALID_ODD", "1000"))

TEAM_ALIASES = {
    "usa": "united states",
    "u s a": "united states",
    "u.s.a": "united states",
    "us": "united states",
    "czech republic": "czechia",
    "czech rep": "czechia",
    "cote d ivoire": "ivory coast",
    "côte d ivoire": "ivory coast",
    "bosnia": "bosnia and herzegovina",
    "bosnia herzegovina": "bosnia and herzegovina",
    "bosnia & herzegovina": "bosnia and herzegovina",
    "korea republic": "south korea",
    "korea rep": "south korea",
    "curaçao": "curacao",
    "turkiye": "turkey",
    "türkiye": "turkey",
}

SUPPORTED_MARKETS = {"1X2", "MATCH_GOALS", "BTTS", "TEAM_GOALS"}


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
    return bool(text_norm and team_norm and team_norm in text_norm)


def parse_decimal_odd(value: str) -> float | None:
    value = clean_text(value)
    decimal = re.fullmatch(r"(\d{1,3})[\.,](\d{1,3})", value)
    if decimal:
        odd = float(f"{decimal.group(1)}.{decimal.group(2)}")
        return round(odd, 2) if MIN_ODD <= odd <= MAX_ODD else None
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
    for raw in re.findall(r"(?<!\d)(?:\d{1,3}[\.,]\d{1,3}|\d{1,4}\s*/\s*\d{1,4})(?!\d)", text or ""):
        odd = parse_decimal_odd(raw)
        if odd is not None:
            tokens.append(odd)
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
        fixtures.append(Fixture(
            date=match_date,
            time=clean_text(item.get("time")) or None,
            home=home,
            away=away,
            match_id=clean_text(item.get("matchId")) or None,
            status=status,
        ))
    fixtures.sort(key=lambda f: (f.date, f.time or "99:99", f.home, f.away))
    return fixtures[:MAX_FIXTURES]


def candidate_urls() -> list[str]:
    raw = os.getenv("ELABET_PROBE_URLS", "")
    if raw.strip():
        return [clean_text(x) for x in raw.split(",") if clean_text(x)]
    return DEFAULT_URLS


def accept_cookies_if_present(page: Page) -> None:
    labels = [
        "Accept all", "Accept All", "Accept", "Agree", "Allow all",
        "Συμφωνώ", "Αποδοχή", "Αποδέχομαι", "Accept cookies",
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


def goto(page: Page, url: str) -> int | None:
    response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    accept_cookies_if_present(page)
    page.wait_for_timeout(3500)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    return response.status if response else None


def visible_text(page: Page) -> str:
    try:
        return clean_text(page.locator("body").inner_text(timeout=10000))
    except Exception:
        return ""


def safe_page_content(page: Page) -> str:
    try:
        return page.content()
    except Exception:
        return ""


def safe_page_title(page: Page) -> str:
    try:
        return clean_text(page.title())
    except Exception:
        return ""


def extract_jsonish_objects(page: Page) -> list[Any]:
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
        # Common hydration pattern. Kept generic on purpose.
        match = re.search(r"<script[^>]+id=[\"']__NEXT_DATA__[\"'][^>]*>(.*?)</script>", text, re.S)
        if match:
            candidates.append(match.group(1))
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


def collect_probe_links(page: Page, html: str, base_url: str) -> dict[str, list[str]]:
    try:
        script_srcs = [clean_text(x) for x in page.locator("script[src]").evaluate_all("nodes => nodes.map(n => n.src || '')")]
    except Exception:
        script_srcs = []
    try:
        link_hrefs = [clean_text(x) for x in page.locator("link[href]").evaluate_all("nodes => nodes.map(n => n.href || '')")]
    except Exception:
        link_hrefs = []

    absolute_urls = re.findall(r"https?://[^\"'<>\\\s)]+", html or "")
    relative_strings = re.findall(r"[\"']([^\"']*(?:api|sportsbook|sport|event|market|coupon|odds|betradar|kambi|graphql)[^\"']*)[\"']", html or "", flags=re.I)

    keywords = ("api", "sportsbook", "sport", "event", "market", "coupon", "odds", "graphql", "betradar", "kambi", "elabet")
    interesting_absolute = [u for u in absolute_urls if any(k in u.lower() for k in keywords)]
    interesting_relative = [urljoin(base_url, u) if u.startswith("/") else u for u in relative_strings]
    interesting_scripts = [u for u in script_srcs if any(k in u.lower() for k in keywords)] or script_srcs[:80]

    def unique(values: Iterable[str], limit: int = 160) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for value in values:
            value = clean_text(value)
            if not value or value in seen:
                continue
            seen.add(value)
            out.append(value)
            if len(out) >= limit:
                break
        return out

    return {
        "scriptSrcs": unique(interesting_scripts),
        "linkHrefs": unique(link_hrefs, limit=80),
        "interestingAbsoluteUrls": unique(interesting_absolute),
        "interestingRelativeOrJoinedStrings": unique(interesting_relative),
    }


def fixture_presence(fixtures: list[Fixture], page_text: str, html: str, limit: int = 16) -> list[dict[str, Any]]:
    return [
        {
            "matchId": f.match_id,
            "date": f.date,
            "time": f.time,
            "homeTeam": f.home,
            "awayTeam": f.away,
            "homeInVisibleText": normalize_name(f.home) in normalize_name(page_text),
            "awayInVisibleText": normalize_name(f.away) in normalize_name(page_text),
            "homeInHtml": normalize_name(f.home) in normalize_name(html),
            "awayInHtml": normalize_name(f.away) in normalize_name(html),
        }
        for f in fixtures[:limit]
    ]


def text_window(full_text: str, home: str, away: str, radius: int = 800) -> str:
    if not full_text:
        return ""
    lower = full_text.lower()
    home_raw = home.lower()
    away_raw = away.lower()
    positions = [p for p in [lower.find(home_raw), lower.find(away_raw)] if p >= 0]
    if positions:
        start = max(0, min(positions) - radius)
        end = min(len(full_text), max(positions) + radius)
        return full_text[start:end]

    normalized = normalize_name(full_text)
    h = normalized.find(normalize_name(home))
    a = normalized.find(normalize_name(away))
    if h < 0 or a < 0:
        return ""
    start = max(0, min(h, a) - radius)
    end = min(len(normalized), max(h, a) + radius)
    return normalized[start:end]


def build_1x2_from_odds(home: str, away: str, odds: list[float]) -> list[dict[str, Any]]:
    if len(odds) < 3:
        return []
    return [
        {"market": "1X2", "selection": home, "team": home, "line": None, "odd": odds[0]},
        {"market": "1X2", "selection": "Draw", "team": None, "line": None, "odd": odds[1]},
        {"market": "1X2", "selection": away, "team": away, "line": None, "odd": odds[2]},
    ]


def try_extract_from_visible_text(fixture: Fixture, page_text: str) -> tuple[list[dict[str, Any]], list[str]]:
    markets: list[dict[str, Any]] = []
    notes: list[str] = []
    window = text_window(page_text, fixture.home, fixture.away)
    if not window:
        notes.append("fixture teams not found together in visible ElaBet text")
        return markets, notes

    odds = decimal_odd_tokens(window)
    if len(odds) >= 3:
        markets.extend(build_1x2_from_odds(fixture.home, fixture.away, odds[:3]))
        notes.append("1X2 extracted from visible text window")
    else:
        notes.append(f"fixture found in visible text, but only {len(odds)} odds tokens found nearby")

    for line in (1.5, 2.5, 3.5):
        pattern = re.compile(rf"(?:over|o|πάνω)\s*{line}\D{{0,50}}(\d{{1,3}}[\.,]\d{{1,3}}|\d{{1,4}}\s*/\s*\d{{1,4}})", re.I)
        match = pattern.search(window)
        if match:
            odd = parse_decimal_odd(match.group(1))
            if odd:
                markets.append({"market": "MATCH_GOALS", "selection": f"Over {line} Goals", "team": None, "line": line, "odd": odd})
                notes.append(f"Over {line} extracted from visible text window")

    btts = re.search(r"(?:both teams to score|btts|γκολ.*και.*οι.*δύο|goal.*goal)\D{0,100}(?:yes|ναι)\D{0,50}(\d{1,3}[\.,]\d{1,3}|\d{1,4}\s*/\s*\d{1,4})", window, re.I)
    if btts:
        odd = parse_decimal_odd(btts.group(1))
        if odd:
            markets.append({"market": "BTTS", "selection": "Both Teams to Score - Yes", "team": None, "line": None, "odd": odd})
            notes.append("BTTS Yes extracted from visible text window")

    return markets, notes


def try_extract_from_json_roots(fixture: Fixture, roots: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    markets: list[dict[str, Any]] = []
    notes: list[str] = []
    for root in roots:
        for node in walk_json(root):
            if not isinstance(node, dict):
                continue
            node_text = json.dumps(node, ensure_ascii=False)[:6000]
            if not contains_team(node_text, fixture.home) or not contains_team(node_text, fixture.away):
                continue
            odds = decimal_odd_tokens(node_text)
            if len(odds) >= 3:
                markets.extend(build_1x2_from_odds(fixture.home, fixture.away, odds[:3]))
                notes.append("1X2 extracted from JSON-like state node")
                return markets, notes
            notes.append("fixture found in JSON-like state node, but no enough odds tokens")
    return markets, notes


def dedupe_markets(markets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in markets:
        market = clean_text(item.get("market"))
        selection = clean_text(item.get("selection"))
        team = clean_text(item.get("team")) or None
        line = item.get("line")
        try:
            odd = round(float(item.get("odd")), 2)
        except Exception:
            continue
        if market not in SUPPORTED_MARKETS or not selection or odd < MIN_ODD or odd > MAX_ODD:
            continue
        key = (market, selection.lower(), team.lower() if team else None, line, odd)
        if key in seen:
            continue
        seen.add(key)
        clean.append({"market": market, "selection": selection, "team": team, "line": line, "odd": odd})
    return clean


def choose_best_probe(probes: list[dict[str, Any]]) -> dict[str, Any]:
    def score(p: dict[str, Any]) -> tuple[int, int, int]:
        visible = p.get("visibleText", "") or ""
        html = p.get("html", "") or ""
        keyword_score = sum(1 for k in ["football", "world cup", "odds", "market", "event", "coupon", "mexico", "germany", "brazil", "england"] if k in visible.lower() or k in html.lower())
        return (int(p.get("httpStatus") or 0 == 200), keyword_score, len(visible))
    return max(probes, key=score) if probes else {}


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_snapshot(path: Path, *, generated_at: str, fixtures: list[Fixture], probes: list[dict[str, Any]], best: dict[str, Any], links: dict[str, list[str]], roots_count: int, errors: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    page_text = best.get("visibleText", "") or ""
    html = best.get("html", "") or ""
    keyword_values = [
        "football", "world cup", "sport", "sportsbook", "odds", "market", "coupon", "event",
        "mexico", "germany", "brazil", "england", "czech", "south africa", "ivory coast",
        "ποδόσφαιρο", "στοίχημα", "αποδόσεις",
    ]
    lower_text = page_text.lower()
    lower_html = html.lower()
    keyword_report = {key: {"visibleText": key in lower_text, "html": key in lower_html} for key in keyword_values}

    content: list[str] = []
    content.append("StatMaker ElaBet probe snapshot\n")
    content.append(json.dumps({
        "generatedAt": generated_at,
        "scriptVersion": SCRIPT_VERSION,
        "bookmaker": BOOKMAKER,
        "source": SOURCE,
        "candidateUrls": [p.get("requestedUrl") for p in probes],
        "selectedUrl": best.get("requestedUrl"),
        "finalUrl": best.get("finalUrl"),
        "httpStatus": best.get("httpStatus"),
        "pageTitle": best.get("title"),
        "visibleTextLength": len(page_text),
        "htmlLength": len(html),
        "jsonRootsFound": roots_count,
        "fixturesLoaded": len(fixtures),
        "snapshotNote": "Diagnostic only. Do not consume this file from the Android app.",
        "errors": errors,
    }, ensure_ascii=False, indent=2))

    def section(title: str) -> str:
        return f"\n\n===== {title} =====\n"

    content.append(section("Candidate page results"))
    content.append(json.dumps([
        {
            "requestedUrl": p.get("requestedUrl"),
            "finalUrl": p.get("finalUrl"),
            "httpStatus": p.get("httpStatus"),
            "title": p.get("title"),
            "visibleTextLength": len(p.get("visibleText", "") or ""),
            "htmlLength": len(p.get("html", "") or ""),
            "error": p.get("error"),
        }
        for p in probes
    ], ensure_ascii=False, indent=2))
    content.append(section("Keyword presence on selected page"))
    content.append(json.dumps(keyword_report, ensure_ascii=False, indent=2))
    content.append(section("Fixture presence sample on selected page"))
    content.append(json.dumps(fixture_presence(fixtures, page_text, html), ensure_ascii=False, indent=2))
    content.append(section("Script/API/link clues on selected page"))
    content.append(json.dumps(links, ensure_ascii=False, indent=2))
    content.append(section("Visible text sample first 12000 chars"))
    content.append(page_text[:12000])
    content.append(section("HTML sample first 12000 chars"))
    content.append(html[:12000])
    path.write_text("".join(content) + "\n", encoding="utf-8")


def run() -> None:
    generated_at = utc_now()
    fixtures = load_fixtures(FIXTURES_PATH)
    urls = candidate_urls()
    probes: list[dict[str, Any]] = []
    errors: list[str] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            locale="el-GR",
            timezone_id="Europe/Athens",
            viewport={"width": 1440, "height": 1100},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        for url in urls:
            try:
                status = goto(page, url)
                probes.append({
                    "requestedUrl": url,
                    "finalUrl": page.url,
                    "httpStatus": status,
                    "title": safe_page_title(page),
                    "visibleText": visible_text(page),
                    "html": safe_page_content(page),
                })
            except PlaywrightTimeoutError as exc:
                errors.append(f"Timeout on {url}: {exc}")
                probes.append({"requestedUrl": url, "error": f"timeout: {exc}"})
            except Exception as exc:
                errors.append(f"Error on {url}: {exc}")
                probes.append({"requestedUrl": url, "error": str(exc)})
        best = choose_best_probe(probes)
        # Rebuild a temp page state is not necessary; collect links from selected HTML through fallback regex.
        html = best.get("html", "") or ""
        page_text = best.get("visibleText", "") or ""
        # Try to collect DOM links from the actual selected URL if it was the last page; otherwise regex fallback is enough for probe.
        links = {
            "scriptSrcs": [],
            "linkHrefs": [],
            "interestingAbsoluteUrls": [],
            "interestingRelativeOrJoinedStrings": [],
        }
        try:
            if best.get("requestedUrl") == probes[-1].get("requestedUrl"):
                links = collect_probe_links(page, html, best.get("finalUrl") or best.get("requestedUrl") or "https://elabet.gr")
        except Exception as exc:
            errors.append(f"link collection failed: {exc}")
        if not links.get("interestingAbsoluteUrls") and html:
            raw_urls = re.findall(r"https?://[^\"'<>\\\s)]+", html)
            links["interestingAbsoluteUrls"] = [u for u in raw_urls[:120] if any(k in u.lower() for k in ("api", "sport", "market", "event", "odds", "elabet"))]

        roots: list[Any] = []
        try:
            # Only reliable for the last opened page. The diagnostic still uses selected HTML/text for matching.
            if best.get("requestedUrl") == probes[-1].get("requestedUrl"):
                roots = extract_jsonish_objects(page)
        except Exception as exc:
            errors.append(f"JSON extraction failed: {exc}")
        browser.close()

    matches: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    for fixture in fixtures:
        markets, notes = try_extract_from_visible_text(fixture, page_text)
        json_markets: list[dict[str, Any]] = []
        json_notes: list[str] = []
        if not markets and roots:
            json_markets, json_notes = try_extract_from_json_roots(fixture, roots)
            markets.extend(json_markets)
            notes.extend(json_notes)
        markets = dedupe_markets(markets)
        if markets:
            matches.append({
                "matchId": fixture.match_id,
                "date": fixture.date,
                "time": fixture.time,
                "competition": COMPETITION,
                "season": SEASON,
                "homeTeam": fixture.home,
                "awayTeam": fixture.away,
                "bookmaker": BOOKMAKER,
                "source": SOURCE,
                "markets": markets,
                "notes": notes,
            })
        else:
            unmatched.append({
                "matchId": fixture.match_id,
                "date": fixture.date,
                "time": fixture.time,
                "homeTeam": fixture.home,
                "awayTeam": fixture.away,
                "notes": notes or ["fixture not confidently matched on ElaBet page"],
            })

    market_counts: dict[str, int] = {}
    for match in matches:
        for market in match.get("markets", []):
            key = clean_text(market.get("market"))
            market_counts[key] = market_counts.get(key, 0) + 1

    output = {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "competition": COMPETITION,
        "season": SEASON,
        "generatedAt": generated_at,
        "scriptVersion": SCRIPT_VERSION,
        "isProbe": True,
        "selectedUrl": best.get("requestedUrl"),
        "finalUrl": best.get("finalUrl"),
        "matches": matches,
    }
    debug = {
        "source": SOURCE,
        "bookmaker": BOOKMAKER,
        "scriptVersion": SCRIPT_VERSION,
        "generatedAt": generated_at,
        "candidateUrls": urls,
        "selectedUrl": best.get("requestedUrl"),
        "finalUrl": best.get("finalUrl"),
        "httpStatus": best.get("httpStatus"),
        "pageTitle": best.get("title"),
        "fixturesLoaded": len(fixtures),
        "matchesMatched": len(matches),
        "matchesWithMarkets": sum(1 for m in matches if m.get("markets")),
        "marketsFound": sum(len(m.get("markets", [])) for m in matches),
        "marketCounts": market_counts,
        "unmatchedFixtures": unmatched,
        "errors": errors,
        "notes": [
            "Probe only. If matchesMatched is 0 but the snapshot shows useful API URLs, next patch should target that API endpoint.",
            "No odds are invented. Empty matches means the extraction was not confident.",
        ],
    }

    write_json(OUTPUT_PATH, output)
    write_json(DEBUG_PATH, debug)
    write_snapshot(SNAPSHOT_PATH, generated_at=generated_at, fixtures=fixtures, probes=probes, best=best, links=links, roots_count=len(roots), errors=errors)
    print(f"ElaBet probe complete. matchesMatched={len(matches)} marketsFound={debug['marketsFound']}")
    print(f"Wrote {OUTPUT_PATH}, {DEBUG_PATH}, {SNAPSHOT_PATH}")


if __name__ == "__main__":
    run()
