import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from scripts.build_statmaker_update_manifest import (
    ArtifactSpec, build_manifest, fixture_calendar_groups, attach_fixture_calendar,
)

class FixtureCalendarManifestTest(unittest.TestCase):
    def test_unpriced_fixture_past_the_prepared_window_survives(self):
        payload = {'matches': [{'id':'1','country':'Greece','leagueCode':'G2',
            'kickoff':'2027-02-14T15:00:00Z','markets':[]}]}
        groups = fixture_calendar_groups(payload, 'domestic_odds', 'hash')
        self.assertEqual(['2027-02-14'], groups[0]['dates'])
        self.assertEqual('hash', groups[0]['sourceSha256'])

    def test_athens_date_and_dst_are_used(self):
        payload = {'matches': [dict(country='Greece', leagueCode='G1', kickoff=v)
            for v in ['2026-10-24T22:30:00Z','2026-10-25T22:30:00Z','2026-10-26T00:30:00+02:00']]}
        self.assertEqual(['2026-10-25','2026-10-26'], fixture_calendar_groups(payload,'domestic_odds','hash')[0]['dates'])

    def test_nested_fixtures_are_deduplicated_and_invalid_dates_are_ignored(self):
        game = {'country':'Greece','leagueCode':'G1','kickoff':'2026-11-02T12:00:00Z'}
        payload = {'matches':[game,dict(game,kickoff='invalid')], 'leagues':[{'matches':[game]}]}
        self.assertEqual(['2026-11-02'], fixture_calendar_groups(payload,'domestic_odds','hash')[0]['dates'])

    def test_regular_manifest_build_includes_calendar_without_changing_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); path = root/'odds.json'
            path.write_text(json.dumps({'matches':[{'leagueCode':'G2','date':'2027-02-14'}]}))
            manifest = build_manifest(root,'main',specs=[ArtifactSpec('domestic_odds','odds.json','odds',True)])
            self.assertEqual(1, manifest['artifactCount'])
            self.assertEqual(manifest['artifacts'][0]['sha256'],manifest['fixtureCalendar']['groups'][0]['sourceSha256'])
            self.assertEqual(manifest,build_manifest(root,'main',specs=[ArtifactSpec('domestic_odds','odds.json','odds',True)]))

    def test_metadata_patch_rejects_a_changed_source_without_mutating_manifest(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder); path=root/'odds/odds_api_io/domestic_odds.json'
            path.parent.mkdir(parents=True);path.write_text('{}')
            manifest={'profile':'main','artifacts':[{'id':'domestic_odds','sha256':'stale'}]}
            with self.assertRaises(ValueError): attach_fixture_calendar(root,manifest)
            self.assertNotIn('fixtureCalendar',manifest)

    def test_metadata_patch_keeps_existing_version_and_artifact_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'odds/odds_api_io/domestic_odds.json'
            path.parent.mkdir(parents=True);path.write_text(json.dumps({'matches':[{'leagueCode':'G2','date':'2027-02-14'}]}))
            item={'id':'domestic_odds','sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            manifest={'profile':'main','artifacts':[item.copy()],'contentVersion':'existing'}
            attach_fixture_calendar(root,manifest)
            self.assertEqual([item],manifest['artifacts']);self.assertEqual('existing',manifest['contentVersion'])
            self.assertEqual(['2027-02-14'],manifest['fixtureCalendar']['groups'][0]['dates'])

if __name__ == '__main__': unittest.main()
