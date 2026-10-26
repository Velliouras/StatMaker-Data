"""Read verified fast results and select current scorer archives without API calls."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess


def load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def priority_completed(root, leagues):
    """Yield only final fixtures bound to the selected provider league and season."""
    for code, entry in leagues.items():
        doc = load(root / "data/statmaker/priority_refresh" / (code + ".json"))
        try:
            season = int(entry["api_football_season"])
            league_id = int(entry["api_football_league_id"])
        except (KeyError, ValueError, TypeError):
            continue
        if (doc.get("contract") != "statmaker-league-tables-v1" or
                doc.get("leagueCode") != code or doc.get("season") != season or
                doc.get("apiFootballLeagueId") != league_id):
            continue
        for row in doc.get("fixtures") or []:
            if row.get("status") not in {"FT", "AET", "PEN"}:
                continue
            fid, h, a = row.get("fixture_id"), row.get("home_goals"), row.get("away_goals")
            if any(type(x) is not int or x < 0 for x in (fid, h, a)) or not fid:
                continue
            if (not row.get("home_team") or not row.get("away_team") or
                    row["home_team"] == row["away_team"]):
                continue
            try:
                stamp = datetime.fromisoformat(str(row.get("date_utc")).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            if stamp.tzinfo is None:
                continue
            yield code, season, row


def recent_final(root, entry, now, hours=6):
    for _, _, row in priority_completed(root, {entry["league_code"]: entry}):
        stamp = datetime.fromisoformat(row["date_utc"].replace("Z", "+00:00"))
        if now - timedelta(hours=hours) <= stamp <= now:
            return True
    return False


def include_current_archives(root):
    selected = {}
    for row in load(root / "data/statmaker/domestic_enriched/index.json").get("leagues", []):
        code = row.get("league_code")
        if not code:
            continue
        old = selected.get(code)
        rank = lambda r: (r.get("stats_role") == "current_target", str(r.get("app_season", "")))
        if old is None or rank(row) > rank(old):
            selected[code] = row
    paths = sorted({"/" + row["output_path"] for row in selected.values()
                    if str(row.get("output_path", "")).startswith("data/statmaker/domestic_enriched/")
                    and ".." not in Path(row["output_path"]).parts})
    if paths:
        subprocess.run(["git", "sparse-checkout", "add", "--stdin"], cwd=root,
                       input="\n".join(paths) + "\n", text=True, check=True)
    print("SCORER_CURRENT_ARCHIVES", len(paths))


if __name__ == "__main__":
    include_current_archives(Path("."))
