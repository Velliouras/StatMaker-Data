#!/usr/bin/env python3
import json
import math
import re
import struct
import sys
import unicodedata
from pathlib import Path

MAGIC = 0x534D4453  # SMDS
FORMAT_VERSION = 2
HALF_CORNERS_MAGIC = 0x534D4831  # SMH1, optional trailer after the legacy v2 rows
STAT_KEYS = [
    "HxG", "AxG", "HSaves", "ASaves", "HPossession", "APossession",
    "HPasses", "APasses", "HPassesAccurate", "APassesAccurate",
    "HF", "AF", "HShotsOffGoal", "AShotsOffGoal", "HBlockedShots", "ABlockedShots",
    "HShotsInsideBox", "AShotsInsideBox", "HShotsOutsideBox", "AShotsOutsideBox",
    "HOffsides", "AOffsides", "HPassAccuracy", "APassAccuracy",
    "HGoalsPrevented", "AGoalsPrevented", "HFreeKicks", "AFreeKicks",
]
TEAM_MATCHING_ALIASES = {
    "aek": "AEK Athens FC",
    "olympiakos": "Olympiakos Piraeus",
    "asteras_tripolis": "Asteras Tripolis",
    "volos_nfc": "Volos NFC",
    "panathinaikos": "Panathinaikos",
    "paok": "PAOK",
}


def normalize_key(value):
    text = unicodedata.normalize("NFD", str(value).lower())
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def normalize_team_key(value):
    base = normalize_key(value)
    return normalize_key(TEAM_MATCHING_ALIASES.get(base, value))


def normalize_date_key(value):
    text = str(value).strip().replace("-", "_").replace("/", "_")
    parts = [part for part in text.split("_") if part]
    if len(parts) == 3:
        try:
            first, second, third = (int(part) for part in parts)
        except ValueError:
            pass
        else:
            if len(parts[0]) == 4:
                return f"{first:04d}_{second:02d}_{third:02d}"
            if len(parts[2]) == 4:
                return f"{third:04d}_{second:02d}_{first:02d}"
    return normalize_key(value)


def fixture_keys(fixture):
    keys = []
    league_id = str(fixture.get("league_id", "") or "")
    fixture_id = str(fixture.get("fixture_id", "") or "")
    if league_id and fixture_id:
        keys.append(f"api_{league_id}_{fixture_id}".lower())
    league_code = str(fixture.get("league_code", "") or "")
    date = str(fixture.get("date", "") or "")[:10]
    home = str(fixture.get("home_team", "") or "")
    away = str(fixture.get("away_team", "") or "")
    if league_code and date and home and away:
        keys.append("|".join((
            normalize_key(league_code), normalize_date_key(date),
            normalize_team_key(home), normalize_team_key(away),
        )))
    if date and home and away:
        keys.append("|".join((
            normalize_date_key(date), normalize_team_key(home), normalize_team_key(away),
        )))
    return keys


def opt_double(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def write_utf(out, value):
    # All StatMaker normalized keys are ASCII after normalize_key, so Java DataOutput.writeUTF
    # is exactly a two-byte big-endian byte length followed by UTF-8 bytes.
    raw = value.encode("utf-8")
    if len(raw) > 65535:
        raise ValueError("Snapshot key exceeds Java writeUTF limit")
    out.write(struct.pack(">H", len(raw)))
    out.write(raw)


def half_corner_rows(fixtures):
    rows = {}
    for fixture in fixtures:
        values = []
        for field in ('HC1H', 'AC1H'):
            number = opt_double((fixture.get('stats') or {}).get(field))
            values.append(number if number is not None and 0 <= number <= 2147483647
                          and number == int(number) else None)
        if any(number is not None for number in values):
            for key in fixture_keys(fixture):
                rows[key] = tuple(values)
    return rows


def write_half_corner_tail(handle, rows):
    if not rows:
        return
    handle.write(struct.pack('>ii', HALF_CORNERS_MAGIC, len(rows)))
    for key, numbers in rows.items():
        write_utf(handle, key)
        for number in numbers:
            handle.write(b'\1' if number is not None else b'\0')
            if number is not None:
                handle.write(struct.pack('>d', number))


def read_half_corner_tail(handle):
    header = handle.read(8)
    if not header:
        return {}
    if len(header) != 8:
        raise ValueError('Incomplete first-half corner trailer')
    magic, count = struct.unpack('>ii', header)
    if magic != HALF_CORNERS_MAGIC or count < 0:
        raise ValueError('Invalid first-half corner trailer')
    rows = {}
    for _ in range(count):
        length = struct.unpack('>H', handle.read(2))[0]
        key = handle.read(length).decode('utf-8')
        values = []
        for _ in range(2):
            flag = handle.read(1)
            if flag not in (b'\0', b'\1'):
                raise ValueError('Invalid first-half corner flag')
            value = struct.unpack('>d', handle.read(8))[0] if flag == b'\1' else None
            if value is not None and (not math.isfinite(value) or value < 0 or value != int(value)):
                raise ValueError('Invalid first-half corner value')
            values.append(value)
        if key in rows:
            raise ValueError('Duplicate first-half corner key')
        rows[key] = tuple(values)
    if handle.read(1):
        raise ValueError('Trailing first-half corner bytes')
    return rows


def main():
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "data/api_football/domestic_normalized_fixture_stats.json")
    target = Path(sys.argv[2] if len(sys.argv) > 2 else "domestic_normalized_stats_v2.bin")
    payload = json.loads(source.read_text(encoding="utf-8"))
    fixtures = payload.get("fixtures", [])
    if not isinstance(fixtures, list):
        raise SystemExit("Domestic normalized fixture stats has no fixtures array")

    rows = {}
    for fixture in fixtures:
        if not isinstance(fixture, dict):
            continue
        stats = fixture.get("stats")
        if not isinstance(stats, dict):
            continue
        values = [opt_double(stats.get(key)) for key in STAT_KEYS]
        if not any(value is not None for value in values):
            continue
        for key in fixture_keys(fixture):
            rows[key] = values

    if not rows:
        raise SystemExit("Domestic normalized fixture stats produced no snapshot rows")

    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    with temp.open("wb") as out:
        out.write(struct.pack(">iii", MAGIC, FORMAT_VERSION, len(rows)))
        for key, values in rows.items():
            write_utf(out, key)
            for value in values:
                out.write(b"\x01" if value is not None else b"\x00")
                if value is not None:
                    out.write(struct.pack(">d", value))
        write_half_corner_tail(out, half_corner_rows(fixtures))
    temp.replace(target)
    print(f"APP_READY_DOMESTIC_NORMALIZED_OK rows={len(rows)} bytes={target.stat().st_size}")


if __name__ == "__main__":
    main()
