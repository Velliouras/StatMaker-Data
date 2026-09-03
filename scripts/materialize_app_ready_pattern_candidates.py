#!/usr/bin/env python3
import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import struct
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime
from pathlib import Path

RULES_FINGERPRINT = "pattern-policy-v2-final-read-model-v4-modifiers-v1"
COMPETITIONS = ("domestic", "champions_league", "europa_league", "conference_league")
TEAM_MATCHING_ALIASES = {
    "aek": "AEK Athens FC",
    "olympiakos": "Olympiakos Piraeus",
    "asteras_tripolis": "Asteras Tripolis",
    "volos_nfc": "Volos NFC",
    "panathinaikos": "Panathinaikos",
    "paok": "PAOK",
}
SHORT_SEASON_RANGE = re.compile(r"(20\d{2})\s*[-_/]\s*(\d{2})(?!\d)")
SEASON_YEAR = re.compile(r"20\d{2}")

MARKET_FAMILY_LABEL = {
    "MATCH_GOALS": "Match Goals",
    "TEAM_GOALS": "Team Goals",
    "FIRST_HALF_GOALS": "Half-time Goals",
    "TEAM_FIRST_HALF_GOALS": "Team Goals 1H",
    "SECOND_HALF_GOALS": "Goals 2H",
    "TEAM_SECOND_HALF_GOALS": "Team Goals 2H",
    "ASIAN_GOALS": "Asian Goals",
    "ASIAN_GOALS_1H": "Asian Goals 1H",
    "MATCH_CORNERS": "Corners",
    "TEAM_CORNERS": "Corners",
    "ASIAN_CORNERS": "Asian Corners",
    "ASIAN_CORNER_HANDICAP": "Asian Corner Handicap",
    "MATCH_CARDS": "Cards",
    "TEAM_CARDS": "Cards",
    "MATCH_YELLOW_CARDS": "Yellow Cards",
    "TEAM_YELLOW_CARDS": "Team Yellow Cards",
    "MATCH_SHOTS": "Shots",
    "TEAM_SHOTS": "Shots",
    "MATCH_SHOTS_ON_TARGET": "Shots on Target",
    "TEAM_SHOTS_ON_TARGET": "Shots on Target",
    "MATCH_FOULS": "Fouls",
    "TEAM_FOULS": "Fouls",
}
FILTER_LABEL = {
    "MATCH_GOALS": "Match Goals",
    "TEAM_GOALS": "Team Goals",
    "FIRST_HALF_GOALS": "Half-time Goals",
    "TEAM_FIRST_HALF_GOALS": "Team Goals 1H",
    "SECOND_HALF_GOALS": "Goals 2H",
    "TEAM_SECOND_HALF_GOALS": "Team Goals 2H",
    "ASIAN_GOALS": "Asian Goals",
    "ASIAN_GOALS_1H": "Asian Goals 1H",
    "MATCH_CORNERS": "Match Corners",
    "ASIAN_CORNERS": "Asian Corners",
    "ASIAN_CORNER_HANDICAP": "Asian Corner Handicap",
    "TEAM_CORNERS": "Team Corners",
    "MATCH_CARDS": "Match Cards",
    "TEAM_CARDS": "Team Cards",
    "MATCH_YELLOW_CARDS": "Yellow Cards",
    "TEAM_YELLOW_CARDS": "Team Yellow Cards",
    "MATCH_SHOTS": "Match Shots",
    "TEAM_SHOTS": "Team Shots",
    "MATCH_SHOTS_ON_TARGET": "Match Shots on Target",
    "TEAM_SHOTS_ON_TARGET": "Team Shots on Target",
    "MATCH_FOULS": "Match Fouls",
    "TEAM_FOULS": "Team Fouls",
}
CANONICAL_LABEL = {
    "goals": "Match Goals",
    "match goals": "Match Goals",
    "team goals": "Team Goals",
    "1h goals": "Half-time Goals",
    "first half goals": "Half-time Goals",
    "half-time goals": "Half-time Goals",
    "half time goals": "Half-time Goals",
    "sot": "Shots on Target",
    "shots on target": "Shots on Target",
    "double chance": "Double Chance",
    "dc": "Double Chance",
    "doublechance": "Double Chance",
    "asian handicap": "Asian Handicap",
    "asian spread": "Asian Handicap",
    "spread": "Asian Handicap",
    "asian handicap 1h": "Asian Handicap 1H",
    "1h asian handicap": "Asian Handicap 1H",
    "first half asian handicap": "Asian Handicap 1H",
    "asian goals": "Asian Goals",
    "asian totals": "Asian Goals",
    "asian total goals": "Asian Goals",
    "asian goals 1h": "Asian Goals 1H",
    "1h asian goals": "Asian Goals 1H",
    "first half asian goals": "Asian Goals 1H",
    "asian corners": "Asian Corners",
    "asian corner totals": "Asian Corners",
    "asian corner handicap": "Asian Corner Handicap",
    "asian corners handicap": "Asian Corner Handicap",
}
FAMILY_ORDER = {
    "1X2": 0,
    "Double Chance": 1,
    "Asian Handicap": 2,
    "Asian Handicap 1H": 3,
    "BTTS": 4,
    "Match Goals": 5,
    "Asian Goals": 6,
    "Asian Goals 1H": 7,
    "Team Goals": 8,
    "Shots": 9,
    "Shots on Target": 10,
    "Corners": 11,
    "Asian Corners": 12,
    "Asian Corner Handicap": 13,
    "Cards": 14,
    "Fouls": 15,
    "Half-time Goals": 16,
}

COUNTRY_CONTINENT = {}
for value in (
    "ENGLAND", "ENG", "UNITED KINGDOM", "UK", "GB", "SCOTLAND", "SCO",
    "GERMANY", "GER", "DEU", "DE", "ITALY", "ITA", "IT", "SPAIN", "ESP", "ES",
    "FRANCE", "FRA", "FR", "NETHERLANDS", "NLD", "NL", "BELGIUM", "BEL",
    "PORTUGAL", "PRT", "PT", "TURKEY", "TUR", "TÜRKIYE", "TR", "GREECE", "GRC", "GR",
    "AUSTRIA", "AUT", "DENMARK", "DNK", "FINLAND", "FIN", "IRELAND", "IRL",
    "NORWAY", "NOR", "POLAND", "POL", "ROMANIA", "ROU", "ROM", "RUSSIA", "RUS",
    "SWEDEN", "SWE", "SWITZERLAND", "SWZ", "CHE", "BULGARIA", "BGR",
    "CROATIA", "CRO", "CYPRUS", "CYP", "CZECH REPUBLIC", "CZECHIA", "CZE",
    "ESTONIA", "EST", "HUNGARY", "HUN", "ICELAND", "ISL", "ISRAEL", "ISR",
    "LATVIA", "LVA", "LITHUANIA", "LTU", "SERBIA", "SRB", "SLOVAKIA", "SVK",
    "SLOVENIA", "SVN", "UKRAINE", "UKR",
):
    COUNTRY_CONTINENT[value] = "Europe"
for value in ("USA", "US", "UNITED STATES", "UNITED STATES OF AMERICA", "MEXICO", "MEX", "CANADA", "CAN"):
    COUNTRY_CONTINENT[value] = "North America"
for value in ("ARGENTINA", "ARG", "BRAZIL", "BRA", "CHILE", "CHL", "COLOMBIA", "COL", "ECUADOR", "ECU", "PERU", "PER", "URUGUAY", "URU"):
    COUNTRY_CONTINENT[value] = "South America"
for value in ("CHINA", "CHN", "JAPAN", "JPN", "SAUDI ARABIA", "SAU", "UNITED ARAB EMIRATES", "UAE", "SOUTH KOREA", "KOREA REPUBLIC", "KOR"):
    COUNTRY_CONTINENT[value] = "Asia"
for value in ("MOROCCO", "MAR", "EGYPT", "EGY", "SOUTH AFRICA", "RSA"):
    COUNTRY_CONTINENT[value] = "Africa"
for value in ("AUSTRALIA", "AUS", "NEW ZEALAND", "NZL"):
    COUNTRY_CONTINENT[value] = "Oceania"


def sha256_text(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_key(value):
    normalized = unicodedata.normalize("NFD", (value or "").lower())
    normalized = "".join(ch for ch in normalized if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", normalized).strip("_")


def normalized_team(value):
    base = normalize_key(value)
    key = normalize_key(TEAM_MATCHING_ALIASES.get(base, value))
    if key in {"bod_glimt", "bodoe_glimt"}:
        return "bodo_glimt"
    if key in {"olympiacos", "olympiakos"}:
        return "olympiakos_piraeus"
    if key == "aek_athens":
        return "aek_athens_fc"
    return key


def parse_date(value):
    text = str(value or "").strip()[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d_%m_%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def season_years(raw, fallback_date):
    raw = str(raw or "")
    match = SHORT_SEASON_RANGE.search(raw)
    if match:
        return {int(match.group(1)), 2000 + int(match.group(2))}
    years = {int(value) for value in SEASON_YEAR.findall(raw)}
    if years:
        return years
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) == 4 and not digits.startswith("20"):
        return {2000 + int(digits[:2]), 2000 + int(digits[-2:])}
    if fallback_date.month >= 7:
        return {fallback_date.year, fallback_date.year + 1}
    return {fallback_date.year - 1, fallback_date.year}


def season_matches_fixture(source_season, fixture_season, fixture_date):
    source_years = season_years(source_season, fixture_date)
    fixture_years = season_years(fixture_season, fixture_date)
    if len(source_years) == 1:
        return next(iter(source_years)) in fixture_years
    if len(fixture_years) == 1:
        return next(iter(fixture_years)) in source_years
    return source_years == fixture_years


class JavaDataInput:
    def __init__(self, path):
        self.handle = Path(path).open("rb")

    def close(self):
        self.handle.close()

    def read(self, size):
        data = self.handle.read(size)
        if len(data) != size:
            raise EOFError("Unexpected end of UEFA snapshot")
        return data

    def int32(self):
        return struct.unpack(">i", self.read(4))[0]

    def boolean(self):
        return self.read(1) != b"\x00"

    def utf(self):
        size = struct.unpack(">H", self.read(2))[0]
        return self.read(size).decode("utf-8", "surrogatepass")

    def nullable_string(self):
        return self.utf() if self.boolean() else None

    def nullable_int(self):
        return self.int32() if self.boolean() else None


def read_uefa_rows(path):
    stream = JavaDataInput(path)
    try:
        if stream.int32() != 0x534D5553 or stream.int32() != 1:
            raise SystemExit(f"Invalid UEFA snapshot {path}")
        stream.utf()
        stream.utf()
        stream.utf()
        rows = []
        for _ in range(max(stream.int32(), 0)):
            competition = stream.utf()
            season = stream.utf()
            stage = stream.utf()
            match_date = stream.utf()
            stream.nullable_string()
            home = stream.utf()
            away = stream.utf()
            stream.int32()
            stream.int32()
            stream.nullable_string()
            for _ in range(12):
                stream.nullable_int()
            source_label = stream.utf()
            rows.append((competition, season, stage, match_date, home, away, source_label))
        for _ in range(max(stream.int32(), 0)):
            stream.utf()
            stream.utf()
            stream.utf()
        return rows
    finally:
        stream.close()


class MaturityIndex:
    def __init__(self, history_db, snapshot_dir):
        started = time.monotonic()
        self.domestic = {}
        self.european = {}
        connection = sqlite3.connect(f"file:{Path(history_db)}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT season, division, date_text, home_team, away_team FROM matches"
            )
            count = 0
            for season, division, date_text, home_team, away_team in rows:
                match_date = parse_date(date_text)
                if match_date is None:
                    continue
                home = normalized_team(home_team)
                away = normalized_team(away_team)
                if not home or not away:
                    continue
                key = f"{match_date}|{home}|{away}|{division}"
                record = (str(season), match_date, key)
                self.domestic.setdefault(home, []).append(record)
                if away != home:
                    self.domestic.setdefault(away, []).append(record)
                count += 1
        finally:
            connection.close()

        for competition_id in ("champions_league", "europa_league", "conference_league"):
            for competition, season, stage, match_date_raw, home_team, away_team, source_label in read_uefa_rows(
                Path(snapshot_dir) / f"{competition_id}.bin"
            ):
                descriptor = f"{competition} {stage} {source_label}".lower()
                if "friendly" in descriptor or "club friendly" in descriptor:
                    continue
                if not any(token in descriptor for token in ("champions league", "europa league", "conference league", "uefa")):
                    continue
                match_date = parse_date(match_date_raw)
                if match_date is None:
                    continue
                home = normalized_team(home_team)
                away = normalized_team(away_team)
                if not home or not away:
                    continue
                competition_key = re.sub(r"[^\w]+", " ", competition.lower(), flags=re.UNICODE).strip()
                key = f"{match_date}|{home}|{away}|{competition_key}"
                record = (str(season), match_date, key)
                self.european.setdefault(home, []).append(record)
                if away != home:
                    self.european.setdefault(away, []).append(record)

        print(
            "APP_READY_HOST_MATURITY_INDEX_OK",
            f"domestic_teams={len(self.domestic)}",
            f"uefa_teams={len(self.european)}",
            f"elapsed_ms={int((time.monotonic() - started) * 1000)}",
        )

    def resolve(self, match):
        fixture_date = parse_date(match.get("date"))
        if fixture_date is None:
            return None
        return (
            self._team_evidence(match, True, fixture_date),
            self._team_evidence(match, False, fixture_date),
        )

    def _team_evidence(self, match, home, fixture_date):
        canonical = (
            match.get("canonicalHomeTeam") if home else match.get("canonicalAwayTeam")
        ) or (match.get("homeTeam") if home else match.get("awayTeam"))
        aliases = {
            canonical,
            match.get("homeTeam") if home else match.get("awayTeam"),
            match.get("providerHomeTeam") if home else match.get("providerAwayTeam"),
        }
        aliases = {normalized_team(value) for value in aliases if value}

        def count(index):
            keys = set()
            for alias in aliases:
                for season, match_date, key in index.get(alias, ()):
                    if match_date < fixture_date and season_matches_fixture(
                        season, match.get("season", ""), fixture_date
                    ):
                        keys.add(key)
            return len(keys)

        return count(self.domestic), count(self.european)


def canonical_label(value):
    text = str(value or "").strip()
    if text.lower() in {"", "any", "all"}:
        return "Any"
    return CANONICAL_LABEL.get(text.lower(), text)


def market_family(identity_family):
    return MARKET_FAMILY_LABEL.get(identity_family, identity_family)


def market_filter_label(identity_family):
    return canonical_label(FILTER_LABEL.get(identity_family, market_family(identity_family)))


def continent_for_country(country):
    key = str(country or "").strip().upper().replace(".", "").replace("_", " ")
    key = re.sub(r"\s+", " ", key)
    return COUNTRY_CONTINENT.get(key, "")


def sane_exact_odd(identity_family, selection_side, line, odd):
    if odd <= 1.0 or odd > 100.0:
        return False
    if identity_family in {"MATCH_GOALS", "ASIAN_GOALS"}:
        if selection_side == "OVER" and line is not None and line <= 1.5 and odd >= 5.0:
            return False
        if selection_side == "OVER" and line is not None and line <= 2.5 and odd >= 8.0:
            return False
        if selection_side == "UNDER" and line is not None and line >= 4.5 and odd >= 8.0:
            return False
    elif identity_family == "TEAM_GOALS":
        if selection_side == "OVER" and line is not None and line <= 0.5 and odd >= 5.0:
            return False
        if selection_side == "OVER" and line is not None and line <= 1.5 and odd >= 10.0:
            return False
    elif identity_family == "BTTS":
        return odd < 8.0
    elif identity_family in {"FIRST_HALF_GOALS", "TEAM_FIRST_HALF_GOALS", "ASIAN_GOALS_1H"}:
        if selection_side == "OVER" and line is not None and line <= 0.5 and odd >= 6.0:
            return False
    elif identity_family in {"MATCH_CORNERS", "ASIAN_CORNERS"}:
        if line is not None and line <= 8.5 and selection_side == "OVER" and odd >= 8.0:
            return False
    elif identity_family == "TEAM_CORNERS":
        if line is not None and line <= 3.5 and selection_side == "OVER" and odd >= 8.0:
            return False
    elif identity_family in {"Correct Score", "Half-time / Full-time", "Winning Margin"}:
        return odd <= 40.0
    return True


def selection_score(identity_family, sub_market_key, selection_side, odd, sample, hits, posterior, reliability, positive_edge):
    family = canonical_label(market_family(identity_family))
    rank = FAMILY_ORDER.get(family, 99)
    family_rank = 1.0 if rank <= 0 else 1.0 / float(rank)
    if odd <= 1.80:
        price_score = 0.86
    elif odd <= 2.60:
        price_score = 1.00
    elif odd <= 3.50:
        price_score = 0.72
    else:
        price_score = 0.45
    bookmaker_score = posterior * 0.78 + reliability * 0.12 + positive_edge * 0.10
    result_bonus = 0.0
    if sub_market_key == "RESULT_1X2":
        if selection_side in {"HOME", "AWAY"}:
            result_bonus = 0.075
        elif selection_side == "DRAW":
            result_bonus = 0.045
        else:
            result_bonus = 0.05
    elif sub_market_key == "RESULT_DOUBLE_CHANCE":
        result_bonus = -0.06
    return (
        bookmaker_score * 0.72
        + price_score * 0.08
        + family_rank * 0.04
        + min(sample, 20) / 20.0 * 0.08
        + min(hits, 15) / 15.0 * 0.08
        + result_bonus
        + (-0.08 if sub_market_key == "RESULT_DOUBLE_CHANCE" else 0.0)
    )


def value_tier(market_probability, posterior, sample_reliability, odd):
    if market_probability is None or posterior is None:
        return None
    if not (0.01 <= market_probability <= 0.99 and 0.01 <= posterior <= 0.99):
        return None
    edge = posterior - market_probability
    expected_value = posterior * odd - 1.0
    if edge < 0.04 or expected_value < 0.05:
        return None
    reliability = max(0.0, min(1.0, sample_reliability)) * 0.55 + 0.21
    low_odds_penalty = 0.0
    if odd < 1.50:
        low_odds_penalty = max(0.0, min(1.0, (1.50 - odd) / (1.50 - 1.20))) * 0.12
    normalized_ev = max(0.0, min(1.0, (expected_value - 0.05) / 0.25))
    normalized_edge = max(0.0, min(1.0, (edge - 0.04) / 0.12))
    ranking_score = max(
        0.0,
        min(
            1.0,
            normalized_ev * 0.42
            + normalized_edge * 0.30
            + reliability * 0.28
            - low_odds_penalty,
        ),
    )
    if ranking_score >= 0.66 and reliability >= 0.60:
        return "STRONG_VALUE"
    if ranking_score >= 0.42:
        return "VALUE"
    return "LEAN_VALUE"


def policy_decision(match, posterior, maturity, eligible):
    if not eligible:
        return False, None
    is_world_cup = "world cup" in str(match.get("competition") or "").lower() or str(match.get("leagueCode") or "").lower() == "wc"
    if is_world_cup:
        if posterior is not None and posterior >= 0.65:
            return True, None
        return False, "REJECTED_POLICY_V2_PROBABILITY_LT_65"
    if maturity is None:
        return False, "REJECTED_POLICY_V2_SEASON_EVIDENCE_UNAVAILABLE"
    home_total = sum(maturity[0])
    away_total = sum(maturity[1])
    minimum_sample = min(home_total, away_total)
    if minimum_sample <= 3:
        return False, "REJECTED_POLICY_V2_SEASON_SAMPLE_0_3"
    if minimum_sample <= 6 and (posterior is None or posterior < 0.70):
        return False, "REJECTED_POLICY_V2_SEASON_SAMPLE_4_6_PROB_LT_70"
    if posterior is None or posterior < 0.65:
        return False, "REJECTED_POLICY_V2_PROBABILITY_LT_65"
    return True, None


def extract_manifest_prefs(checkpoint_root, source_root):
    path = Path(checkpoint_root) / "shared_prefs" / "statmaker_data_manifests.xml"
    tree = ET.parse(path)
    values = {
        node.attrib["name"]: node.text or ""
        for node in tree.getroot()
        if node.tag == "string" and node.attrib.get("name")
    }
    main_raw = values.get("main_manifest_raw", "")
    uefa_raw = values.get("uefa_manifest_raw", "")
    if not main_raw or not uefa_raw:
        raise SystemExit("Checkpoint is missing bundled source manifests")
    source_root = Path(source_root)
    source_root.mkdir(parents=True, exist_ok=True)
    (source_root / "main_manifest.json").write_text(main_raw, encoding="utf-8")
    (source_root / "uefa_manifest.json").write_text(uefa_raw, encoding="utf-8")
    odds_root = Path(checkpoint_root) / "files" / "app_ready_odds"
    for competition_id in COMPETITIONS:
        shutil.copy2(odds_root / f"{competition_id}.json", source_root / f"{competition_id}.json")
    print(
        "APP_READY_HOST_SOURCE_MANIFESTS_OK",
        "main=" + json.loads(main_raw).get("contentVersion", "")[:12],
        "uefa=" + json.loads(uefa_raw).get("contentVersion", "")[:12],
    )


def materialize(checkpoint_root, raw_root):
    checkpoint_root = Path(checkpoint_root)
    raw_root = Path(raw_root)
    shutil.rmtree(raw_root, ignore_errors=True)
    shutil.copytree(checkpoint_root, raw_root)

    prepared_db = raw_root / "databases" / "statmaker_prepared_betting.db"
    history_db = raw_root / "databases" / "statmaker.db"
    snapshot_dir = raw_root / "files" / "statmaker_stats_snapshots"
    connection = sqlite3.connect(prepared_db)
    connection.execute("PRAGMA foreign_keys=OFF")
    quick = connection.execute("PRAGMA quick_check").fetchone()
    if not quick or quick[0] != "ok":
        raise SystemExit(f"Prepared checkpoint quick_check failed: {quick}")
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) < 10:
        raise SystemExit("Prepared checkpoint schema is below v10")

    versions = {
        str(competition): str(version)
        for competition, version in connection.execute(
            "SELECT competition_id, snapshot_version FROM prepared_snapshot_meta WHERE state='ready'"
        )
    }
    if set(versions) != set(COMPETITIONS):
        raise SystemExit(f"Checkpoint does not contain exact 4/4 READY snapshots: {sorted(versions)}")

    source_seed = "\n".join(f"{competition}|{versions[competition]}" for competition in sorted(COMPETITIONS))
    source_fingerprint = sha256_text(source_seed)
    generation_id = sha256_text(f"{source_fingerprint}|{RULES_FINGERPRINT}")

    existing = connection.execute(
        """
        SELECT candidate_count
        FROM prepared_pattern_generation
        WHERE generation_id=? AND state='ready' AND rules_fingerprint=?
        """,
        (generation_id, RULES_FINGERPRINT),
    ).fetchone()
    if existing and int(existing[0]) > 0:
        print(
            "APP_READY_HOST_PATTERN_REUSED",
            f"generation={generation_id}",
            f"candidates={int(existing[0])}",
        )
        connection.close()
        return generation_id, int(existing[0])

    started = time.monotonic()
    maturity_index = MaturityIndex(history_db, snapshot_dir)
    maturity_by_match = {}
    candidates = []
    source_order = 0
    rejection_counts = Counter()

    query = """
        SELECT rowid, selection_key, match_key, local_date,
               selection_market, selection_name, selection_team, selection_line, selection_odd,
               category,
               bm_hits, bm_sample, bm_hit_rate,
               bm_market_probability, bm_market_probability_source,
               bm_empirical_probability, bm_posterior_probability, bm_market_edge,
               bm_sample_reliability, bm_normalized_positive_edge, bm_raw_implied_probability,
               bm_market_overround, bm_bookmaker_margin,
               identity_broad_group, identity_family, identity_sub_market_key,
               identity_team_side, identity_line, identity_selection_side,
               identity_source_market, identity_team, identity_selection_token,
               score_value, score_tier, score_bookmaker_base,
               score_model_adjustment, score_trend_adjustment,
               qualifies_builder
        FROM prepared_selections
        WHERE competition_id=? AND snapshot_version=? AND qualifies_pattern=1
        ORDER BY rowid
    """

    for competition_id in COMPETITIONS:
        snapshot_version = versions[competition_id]
        matches = {
            str(match_key): json.loads(payload)
            for match_key, payload in connection.execute(
                """
                SELECT match_key, payload
                FROM prepared_matches
                WHERE competition_id=? AND snapshot_version=?
                """,
                (competition_id, snapshot_version),
            )
        }
        count = 0
        for row in connection.execute(query, (competition_id, snapshot_version)):
            (
                _rowid, selection_key, prepared_match_key, local_date,
                _selection_market, _selection_name, _selection_team, _selection_line, odd,
                _category, hits, sample, hit_rate,
                market_probability, _market_probability_source,
                _empirical_probability, posterior_probability, _market_edge,
                sample_reliability, normalized_positive_edge, _raw_implied_probability,
                _market_overround, _bookmaker_margin,
                broad_group, identity_family, sub_market_key,
                _team_side, identity_line, selection_side,
                _source_market, identity_team, selection_token,
                evidence_score, _score_tier, _score_bookmaker_base,
                _score_model_adjustment, _score_trend_adjustment,
                _qualifies_builder,
            ) = row
            order = source_order
            source_order += 1
            count += 1

            required = (
                hits, sample, hit_rate, market_probability, posterior_probability,
                sample_reliability, normalized_positive_edge, broad_group,
                identity_family, sub_market_key, selection_side, evidence_score,
            )
            if any(value is None for value in required):
                raise SystemExit(
                    f"Prepared PATTERN row is missing persisted scalar evidence: {competition_id}:{selection_key}"
                )

            match = matches.get(str(prepared_match_key))
            if match is None:
                raise SystemExit(
                    f"Prepared PATTERN row has no prepared match: {competition_id}:{prepared_match_key}"
                )
            odd = float(odd)
            hits = int(hits)
            sample = int(sample)
            hit_rate = float(hit_rate)
            posterior_probability = float(posterior_probability)
            sample_reliability = float(sample_reliability)
            normalized_positive_edge = float(normalized_positive_edge)
            evidence_score = float(evidence_score)

            eligible = odd >= 1.20 and sane_exact_odd(
                str(identity_family),
                str(selection_side),
                None if identity_line is None else float(identity_line),
                odd,
            )
            runtime_match_key = f"{match.get('date', '')}|{match.get('homeTeam', '')}|{match.get('awayTeam', '')}"
            maturity = None
            if eligible:
                if runtime_match_key not in maturity_by_match:
                    maturity_by_match[runtime_match_key] = maturity_index.resolve(match)
                maturity = maturity_by_match[runtime_match_key]
            premium, rejection_reason = policy_decision(
                match, posterior_probability, maturity, eligible
            )
            rejection_counts[rejection_reason or "ELIGIBLE"] += 1

            line_text = "" if identity_line is None else str(float(identity_line))
            exact_key = (
                f"{broad_group}|{sub_market_key}|{selection_side}|"
                f"{line_text}|{identity_team or ''}|{selection_token or ''}"
            )
            league = str(match.get("leagueCode") or match.get("competition") or "")
            candidates.append(
                (
                    generation_id,
                    competition_id,
                    snapshot_version,
                    str(selection_key),
                    runtime_match_key,
                    str(local_date),
                    continent_for_country(match.get("country", "")),
                    str(match.get("country") or ""),
                    league,
                    market_filter_label(str(identity_family)),
                    odd,
                    exact_key,
                    selection_score(
                        str(identity_family),
                        str(sub_market_key),
                        str(selection_side),
                        odd,
                        sample,
                        hits,
                        posterior_probability,
                        sample_reliability,
                        normalized_positive_edge,
                    ),
                    evidence_score,
                    order,
                    hit_rate,
                    sample,
                    value_tier(
                        float(market_probability),
                        posterior_probability,
                        sample_reliability,
                        odd,
                    ),
                    1 if eligible else 0,
                    1 if premium else 0,
                    rejection_reason,
                )
            )
        print(
            "APP_READY_HOST_PATTERN_SOURCE_OK",
            f"competition={competition_id}",
            f"rows={count}",
        )

    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute(
            "DELETE FROM prepared_pattern_candidates WHERE generation_id=?",
            (generation_id,),
        )
        connection.execute(
            "DELETE FROM prepared_pattern_generation WHERE generation_id=?",
            (generation_id,),
        )
        connection.execute(
            """
            INSERT INTO prepared_pattern_generation(
                generation_id, source_fingerprint, rules_fingerprint, state,
                built_at_ms, candidate_count, proposal_count
            ) VALUES(?,?,?,?,?,?,0)
            """,
            (
                generation_id,
                source_fingerprint,
                RULES_FINGERPRINT,
                "building",
                int(time.time() * 1000),
                0,
            ),
        )
        connection.executemany(
            """
            INSERT INTO prepared_pattern_candidates(
                generation_id, competition_id, snapshot_version, selection_key,
                match_key, local_date, continent, country, league_code, market_family,
                selection_odd, exact_recommendation_key, selection_score, evidence_score,
                source_order, strict_hit_rate, strict_sample, value_tier,
                recommendation_eligible, policy_premium_eligible, policy_rejection_reason
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            candidates,
        )
        connection.execute(
            """
            UPDATE prepared_pattern_generation
            SET state='ready', candidate_count=?, proposal_count=0
            WHERE generation_id=? AND state='building'
            """,
            (len(candidates), generation_id),
        )
        connection.execute(
            "DELETE FROM prepared_pattern_candidates WHERE generation_id<>?",
            (generation_id,),
        )
        connection.execute(
            "DELETE FROM prepared_pattern_generation WHERE generation_id<>?",
            (generation_id,),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    actual = int(
        connection.execute(
            "SELECT COUNT(*) FROM prepared_pattern_candidates WHERE generation_id=?",
            (generation_id,),
        ).fetchone()[0]
    )
    quick = connection.execute("PRAGMA quick_check").fetchone()
    connection.close()
    if actual != len(candidates) or not quick or quick[0] != "ok":
        raise SystemExit(
            f"Host candidate materialization validation failed: expected={len(candidates)} actual={actual} quick={quick}"
        )

    print(
        "APP_READY_HOST_PATTERN_OK",
        f"generation={generation_id}",
        f"candidates={len(candidates)}",
        f"premium={rejection_counts['ELIGIBLE']}",
        "rejections=" + ",".join(
            f"{key}:{value}"
            for key, value in sorted(rejection_counts.items())
            if key != "ELIGIBLE"
        ),
        f"elapsed_ms={int((time.monotonic() - started) * 1000)}",
    )
    return generation_id, len(candidates)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("checkpoint_root")
    parser.add_argument("raw_root")
    parser.add_argument("source_root")
    args = parser.parse_args()
    extract_manifest_prefs(args.checkpoint_root, args.source_root)
    materialize(args.checkpoint_root, args.raw_root)


if __name__ == "__main__":
    main()
