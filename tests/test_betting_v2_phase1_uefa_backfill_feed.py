"""Source-safe extraction of canonical future UEFA match identities."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from zipfile import ZipFile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts/betting_v2"))
from phase1_uefa_backfill_feed import create_feeds


class CanonicalUefaFeedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.out=self.root/"feeder"
        d=self.root/"data/statmaker/app_ready"
        d.mkdir(parents=True,exist_ok=True)
        z=d/"fixture.zip"
        with ZipFile(z,"w") as f:
            f.writestr("files/app_ready_odds/source.json",json.dumps({
                "leagues":[
                    {"leagueCode":"CL","matches":[
                        {"canonicalHomeTeam":"Arsenal","canonicalAwayTeam":"Lille",
                         "kickoff":"2026-10-14T20:00:00Z","markets":[{"odd":1.9}]},
                        {"canonicalHomeTeam":"Old","canonicalAwayTeam":"Game",
                         "kickoff":"2026-09-10T20:00:00Z"}]},
                    {"leagueCode":"EL","matches":[{"homeTeam":"Braga","awayTeam":"Ajax",
                      "kickoff":"2026-10-15T20:00:00Z"}]},
                    {"leagueCode":"CONF","matches":[{"homeTeam":"Celtic","awayTeam":"Basel",
                      "kickoff":"2026-10-16T20:00:00Z"}]}
                 ]}))
        manifest={"artifacts":[{"id":"app_ready_betting_bundle",
            "path":"data/statmaker/app_ready/fixture.zip",
            "sha256":hashlib.sha256(z.read_bytes()).hexdigest()}]}
        (d/"update_manifest.json").write_text(json.dumps(manifest),encoding="utf-8")
    def test_all_three_leagues_are_preserved_without_odds(self):
        result=create_feeds(self.root,self.out,
            datetime(2026,10,13,tzinfo=timezone.utc))
        self.assertEqual([result["byLeague"][x]["finalFixtures"] for x in
                         ("CL","EL","UECL")],[1,1,1])
        path=self.out/"champions_league_odds.json"
        row=json.loads(path.read_text(encoding="utf-8"))["matches"][0]
        self.assertEqual(row["canonicalHomeTeam"],"Arsenal")
        self.assertNotIn("markets",row)
        self.assertNotIn("odds",row)
    def test_source_hash_mismatch_fails_closed(self):
        p=self.root/"data/statmaker/app_ready/update_manifest.json"
        m=json.loads(p.read_text(encoding="utf-8"))
        m["artifacts"][0]["sha256"]="bad"
        p.write_text(json.dumps(m),encoding="utf-8")
        with self.assertRaisesRegex(ValueError,"checksum"):
            create_feeds(self.root,self.out,
                datetime(2026,10,13,tzinfo=timezone.utc))
    def test_no_remaining_fixture_does_not_trigger_paid_lookup(self):
        with self.assertRaisesRegex(ValueError,"both empty"):
            create_feeds(self.root,self.out,
                datetime(2027,10,13,tzinfo=timezone.utc))


if __name__=="__main__":
    unittest.main()
