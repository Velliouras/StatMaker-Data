"""Common-scale club Elo and observed xG residuals for UEFA simulations.

Club/federation identities are explicit. No draw-pot ratings or guessed aliases.
Missing xG stays neutral; the source Elo remains available for every participant.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
import unicodedata
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
ALGORITHM = "uefa-clubelo-xg-sporting-bracket-v2"
SNAPSHOT = "data/statmaker/uefa_clubelo_strengths.json"
SOURCE_URL = "https://clubelo.com/"
XG_PRIOR_MATCHES = 12.0
XG_MAX_MATCHES = 24
COUNTRIES = dict(zip(
    "England Germany Spain Italy France Portugal Netherlands Belgium Greece Austria Denmark Sweden Norway Finland Switzerland Scotland CzechRepublic Slovakia Croatia Serbia Slovenia Bulgaria Romania Poland Hungary Cyprus Israel Ukraine Azerbaijan Armenia Kazakhstan Bosnia Georgia Albania Latvia Lithuania Andorra Gibraltar".split(),
    "ENG GER ESP ITA FRA POR NED BEL GRE AUT DEN SWE NOR FIN SUI SCO CZE SVK CRO SRB SVN BUL ROU POL HUN CYP ISR UKR AZE ARM KAZ BIH GEO ALB LVA LTU AND GIB".split()))
COUNTRIES["Czech Republic"] = "CZE"
COUNTRIES["Bosnia and Herzegovina"] = "BIH"


def norm(value):
    return "".join(c for c in unicodedata.normalize("NFKD", str(value)).lower() if c.isalnum())


def parse_catalog(raw: str):
    """Read only published club/federation/Elo table cells, never execute scripts."""
    rows = []
    for table in re.findall(r'<table class="ast">(.*?)</table>', raw, re.S):
        for row in re.findall(r'<tr[^>]*>(.*?)(?=<tr|$)', table, re.S):
            name = re.search(r'<span class="Ast">(.*?)</span>', row, re.S)
            rating = re.search(r'<td class="r">([0-9]+)(p?)</td>', row)
            country = re.search(r'alt="([A-Z]{3})"', row)
            if name and rating and country:
                rows.append({"name": html.unescape(re.sub('<[^>]*>', '', name[1])),
                             "country": country[1], "elo": int(rating[1]),
                             "provisional": bool(rating[2])})
    stamp = re.search(r'Page created on (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})', raw)
    if len(rows) < 500 or not stamp:
        raise ValueError("Unrecognised or incomplete public ClubElo snapshot")
    as_of = datetime.strptime(stamp[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return rows, as_of


def load_catalog(root=ROOT):
    snapshot = json.loads((root / SNAPSHOT).read_text())
    if snapshot.get("schemaVersion") != 1 or snapshot.get("sourceUrl") != SOURCE_URL:
        raise ValueError("Unknown club-strength source")
    aliases = json.loads((root / "config/uefa_clubelo_aliases.json").read_text())["teams"]
    indexed = defaultdict(list)
    for row in snapshot["clubs"]:
        if not row.get("provisional") and math.isfinite(float(row["elo"])):
            indexed[(row["country"], norm(row["name"]))].append(row)
    mapped = {}
    for canonical, source in aliases.items():
        rows = indexed[(source["country"], norm(source["name"]))]
        unique = {(r["country"], r["name"], float(r["elo"])) for r in rows}
        if len(unique) != 1:
            raise ValueError(f"Missing/ambiguous club strength: {canonical}")
        country, name, elo = next(iter(unique))
        if not 500 <= elo <= 2600:
            raise ValueError(f"Invalid participant strength: {canonical}/{elo}")
        mapped[canonical] = {"country": country, "name": name, "elo": elo}
    return snapshot, mapped, indexed


def refresh_snapshot(root=ROOT, now=None):
    now = now or datetime.now(timezone.utc)
    path = root / SNAPSHOT
    old = json.loads(path.read_text()) if path.is_file() else None
    # The public page is a daily rating snapshot. Avoid repeatedly fetching it on a matchday.
    if old and str(old.get("fetchedAt", ""))[:10] == now.date().isoformat():
        load_catalog(root)
        return False
    try:
        request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "StatMaker UEFA simulation"})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(3 * 1024 * 1024)
        rows, as_of = parse_catalog(raw.decode("utf-8"))
        if as_of > now + timedelta(hours=1) or now - as_of > timedelta(days=7):
            raise ValueError("Public club-strength snapshot is stale or in the future")
        value = {"schemaVersion": 1, "source": "ClubElo", "sourceUrl": SOURCE_URL,
                 "sourceAsOf": as_of.isoformat(),
                 "throughDate": (as_of.date() - timedelta(days=1)).isoformat(),
                 "fetchedAt": now.isoformat(), "sourceSha256": hashlib.sha256(raw).hexdigest(),
                 "clubs": rows}
        path.parent.mkdir(parents=True, exist_ok=True)
        prior = path.read_bytes() if path.is_file() else None
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        try:
            load_catalog(root)
        except Exception:
            if prior is None:
                path.unlink()
            else:
                path.write_bytes(prior)
            raise
        return True
    except Exception as error:
        # Retain a recent verified snapshot instead of inventing ratings or damaging published data.
        if old and now - datetime.fromisoformat(old["sourceAsOf"]) <= timedelta(days=7):
            load_catalog(root)
            print("CLUB_STRENGTH_LAST_VALID_RETAINED", type(error).__name__)
            return False
        raise


def xg_profiles(config, root, prior_means, now=None):
    now = now or datetime.now(timezone.utc)
    snapshot, mapped, indexed = load_catalog(root)
    # Source names are scoped to the federation. Known provider spellings use reviewed aliases.
    identities = defaultdict(set)
    for canonical, row in mapped.items():
        identities[norm(canonical)].add((row["country"], norm(row["name"])))
    for comp in config["competitions"]:
        for alias, canonical in comp.get("aliases", {}).items():
            row = mapped[canonical]
            identities[norm(alias)].add((row["country"], norm(row["name"])))
    targets = {(r["country"], norm(r["name"])): t for t, r in mapped.items()}

    def resolve(name, country=None):
        candidates = set(identities.get(norm(name), ()))
        if country:
            candidates = {c for c in candidates if c[0] == country}
            if (country, norm(name)) in indexed:
                candidates.add((country, norm(name)))
        if len(candidates) != 1:
            return None
        key = next(iter(candidates))
        values = {float(r["elo"]) for r in indexed.get(key, ())}
        return (key, next(iter(values))) if len(values) == 1 else None

    groups = []
    index = json.loads((root / "data/statmaker/domestic_enriched/index.json").read_text())
    for entry in index["leagues"]:
        country = COUNTRIES.get(entry.get("country"))
        if not country or not entry.get("output_path"):
            continue
        path = root / entry["output_path"]
        if path.is_file():
            groups.append((country, json.loads(path.read_text()).get("matches", []), False))
    current = json.loads((root / "data/statmaker/uefa_current_stats.json").read_text())
    for comp in current.get("competitions", []):
        groups.append((None, comp.get("fixtures", []), True))
    observed = defaultdict(list)
    seen = set()
    for country, rows, is_uefa in groups:
        valid = []
        for row in rows:
            date = str(row.get("date_utc") or row.get("date") or "")[:10]
            if not date or not (now.date()-timedelta(days=365)).isoformat() <= date <= now.date().isoformat():
                continue
            if row.get("status") not in {"FT", "AET", "PEN"}:
                continue
            stats = row.get("normalized_stats") or row.get("normalizedStats") or {}
            hx, ax = stats.get("HxG"), stats.get("AxG")
            if any(type(x) not in (int, float) or not math.isfinite(x) or not 0 <= x <= 12 for x in (hx, ax)):
                continue
            home = row.get("homeTeam") if is_uefa else row.get("home_team")
            away = row.get("awayTeam") if is_uefa else row.get("away_team")
            identity = (date, norm(home), norm(away))
            if identity in seen:
                continue
            seen.add(identity)
            valid.append((date, home, away, float(hx), float(ax)))
        if not valid:
            continue
        base_h = sum(r[3] for r in valid)/len(valid)
        base_a = sum(r[4] for r in valid)/len(valid)
        for date, home, away, hx, ax in valid:
            h, a = resolve(home, country), resolve(away, country)
            if h is None or a is None:
                continue
            expected_h, expected_a = prior_means(home, away, {home: h[1], away: a[1]}, base_h, base_a)
            for key, attack, defense, expected_attack, expected_defense in (
                (h[0], hx, ax, expected_h, expected_a),
                (a[0], ax, hx, expected_a, expected_h),
            ):
                if key in targets:
                    observed[targets[key]].append((date, attack, defense, expected_attack, expected_defense))
    profiles = {}
    for team in mapped:
        rows = sorted(observed[team], reverse=True)[:XG_MAX_MATCHES]
        if not rows:
            profiles[team] = {"attack": 1.0, "defense": 1.0, "samples": 0}
            continue
        expected_a = sum(r[3] for r in rows)
        expected_d = sum(r[4] for r in rows)
        # Gamma-Poisson residual shrinkage: Elo is the prior; xG adjusts only unexplained form.
        prior_a = XG_PRIOR_MATCHES * expected_a / len(rows)
        prior_d = XG_PRIOR_MATCHES * expected_d / len(rows)
        profiles[team] = {
            "attack": max(.65, min(1.5, (sum(r[1] for r in rows)+prior_a)/(expected_a+prior_a))),
            "defense": max(.65, min(1.5, (sum(r[2] for r in rows)+prior_d)/(expected_d+prior_d))),
            "samples": len(rows), "throughDate": rows[0][0]}
    return profiles
