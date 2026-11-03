"""External outages must not mutate verified published data or pretend freshness."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, Mock
from urllib.error import HTTPError
import zipfile
from scripts import uefa_club_strengths as source
from scripts import refresh_uefa_simulations as refresh

class StrengthRefreshResilienceTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.now=datetime(2026,11,1,8,tzinfo=timezone.utc)
        self.path=self.root/source.SNAPSHOT;self.path.parent.mkdir(parents=True)
        self.offline=HTTPError(source.SOURCE_URL,403,'Forbidden',None,None)

    def anchor(self, age):
        stamp=self.now-age
        self.path.write_text(json.dumps(dict(schemaVersion=1,sourceUrl=source.SOURCE_URL,
            sourceAsOf=stamp.isoformat(),fetchedAt=stamp.isoformat(),clubs=[])))
        return self.path.read_bytes()

    def test_recent_verified_cache_survives_403_without_faking_fetched_time(self):
        previous=self.anchor(timedelta(days=2))
        with patch.object(source,'load_catalog'),patch.object(source.urllib.request,'urlopen',side_effect=self.offline) as fetch:
            self.assertFalse(source.refresh_snapshot(self.root,self.now))
            self.assertFalse(source.refresh_snapshot(self.root,self.now+timedelta(minutes=5)))
        fetch.assert_called_once()
        self.assertEqual(previous,self.path.read_bytes())

    def test_expired_anchor_defers_and_rate_limits_failed_fetches_without_relaxing_seven_days(self):
        previous=self.anchor(timedelta(days=7,hours=1))
        with patch.object(source,'load_catalog'),patch.object(source.urllib.request,'urlopen',side_effect=self.offline) as fetch:
            for offset in (0,5):
                with self.assertRaises(source.ClubStrengthSourceUnavailable) as error:
                    source.refresh_snapshot(self.root,self.now+timedelta(minutes=offset))
                self.assertIn('HTTP 403',str(error.exception))
        fetch.assert_called_once()
        self.assertEqual(previous,self.path.read_bytes())
        status=json.loads((self.root/source.REFRESH_STATUS).read_text())
        self.assertEqual('unavailable',status['status'])
        self.assertEqual((self.now+timedelta(hours=6)).isoformat(),status['nextAttemptAt'])

    def test_invalid_cached_catalog_is_an_error_and_cannot_be_silenced_as_outage(self):
        self.anchor(timedelta(days=8))
        with patch.object(source,'load_catalog',side_effect=ValueError('Missing/ambiguous club')),\
             patch.object(source.urllib.request,'urlopen') as fetch:
            with self.assertRaisesRegex(ValueError,'ambiguous'):
                source.refresh_snapshot(self.root,self.now)
        fetch.assert_not_called()

    def test_success_after_retry_publishes_only_validated_new_ratings_and_clears_failure(self):
        self.anchor(timedelta(days=8))
        with patch.object(source,'load_catalog'),patch.object(source.urllib.request,'urlopen',side_effect=self.offline):
            with self.assertRaises(source.ClubStrengthSourceUnavailable):source.refresh_snapshot(self.root,self.now)
        recovered=self.now+timedelta(hours=6,seconds=1)
        response=Mock();response.read.return_value=b'verified-public-page'
        opener=Mock();opener.__enter__=Mock(return_value=response);opener.__exit__=Mock(return_value=False)
        with patch.object(source,'load_catalog'),patch.object(source.urllib.request,'urlopen',return_value=opener),\
             patch.object(source,'parse_catalog',return_value=([dict(name='Club',country='GRE',elo=1500)],recovered)):
            self.assertTrue(source.refresh_snapshot(self.root,recovered))
        updated=json.loads(self.path.read_text())
        self.assertEqual(recovered.isoformat(),updated['sourceAsOf'])
        self.assertEqual(hashlib.sha256(b'verified-public-page').hexdigest(),updated['sourceSha256'])
        self.assertFalse((self.root/source.REFRESH_STATUS).exists())

    def test_invalid_new_catalog_rolls_back_instead_of_publishing_or_claiming_an_outage(self):
        previous=self.anchor(timedelta(days=2))
        response=Mock();response.read.return_value=b'page'
        opener=Mock();opener.__enter__=Mock(return_value=response);opener.__exit__=Mock(return_value=False)
        with patch.object(source,'load_catalog',side_effect=[None,ValueError('new identities invalid')]),\
             patch.object(source.urllib.request,'urlopen',return_value=opener),\
             patch.object(source,'parse_catalog',return_value=([],self.now)):
            with self.assertRaisesRegex(ValueError,'identities'):source.refresh_snapshot(self.root,self.now)
        self.assertEqual(previous,self.path.read_bytes())

    def published(self, bad_hash=False):
        config=self.root/'config.json';config.write_text('{"competitions":[]}')
        db=self.root/'retained.db'
        with sqlite3.connect(db) as connection:connection.execute('CREATE TABLE verified (value INTEGER)')
        bundle=self.root/'prepared.zip'
        with zipfile.ZipFile(bundle,'w') as archive:archive.write(db,'databases/statmaker_prepared_betting.db')
        manifest=self.root/'manifest.json'
        manifest.write_text(json.dumps(dict(artifacts=[dict(id='app_ready_betting_bundle',path='prepared.zip',
            sha256='bad' if bad_hash else hashlib.sha256(bundle.read_bytes()).hexdigest())])))
        return config,manifest,bundle

    def test_deferred_job_keeps_manifest_zip_and_model_unchanged_and_is_explicit_in_report(self):
        config,manifest,bundle=self.published();report=self.root/'report.json'
        before=manifest.read_bytes(),bundle.read_bytes()
        with patch.object(refresh,'ROOT',self.root),patch.object(refresh,'MANIFEST',manifest),\
             patch.object(refresh,'REPORT',report),patch.object(refresh.sim,'CONFIG_PATH',config),\
             patch.object(refresh.strengths,'refresh_snapshot',side_effect=source.ClubStrengthSourceUnavailable('HTTP 403','2026-10-25','2026-11-01T14:00:00Z')),\
             patch.object(refresh.subprocess,'run') as generate:
            self.assertEqual(0,refresh.main())
        generate.assert_not_called()
        self.assertEqual(before,(manifest.read_bytes(),bundle.read_bytes()))
        status=json.loads(report.read_text())
        self.assertEqual('deferred',status['status']);self.assertTrue(status['publishedDataPreserved'])
        self.assertFalse(status['publicRatingRefreshed']);self.assertEqual(0,status['apiRequestsUsed'])

    def test_corrupt_retained_bundle_still_fails_instead_of_reporting_success(self):
        config,manifest,_=self.published(bad_hash=True)
        with patch.object(refresh,'ROOT',self.root),patch.object(refresh,'MANIFEST',manifest),\
             patch.object(refresh,'REPORT',self.root/'report.json'),patch.object(refresh.sim,'CONFIG_PATH',config),\
             patch.object(refresh.strengths,'refresh_snapshot',side_effect=source.ClubStrengthSourceUnavailable('HTTP 403','2026-10-25','2026-11-01T14:00:00Z')):
            with self.assertRaisesRegex(ValueError,'SHA256'):refresh.main()

    def test_lagging_standings_defer_before_ratings_or_generation(self):
        config, manifest, bundle = self.published()
        config.write_text('{"season":2026,"competitions":[{}]}')
        report = self.root / 'report.json'
        before = manifest.read_bytes(), bundle.read_bytes()
        with patch.object(refresh, 'ROOT', self.root), patch.object(refresh, 'MANIFEST', manifest), \
             patch.object(refresh, 'REPORT', report), patch.object(refresh.sim, 'CONFIG_PATH', config), \
             patch.object(refresh.sim, 'cached_competition',
                          side_effect=refresh.sim.UefaStandingsPending('waiting for official standings')), \
             patch.object(refresh.strengths, 'refresh_snapshot') as ratings, \
             patch.object(refresh.subprocess, 'run') as generate:
            self.assertEqual(0, refresh.main())
        ratings.assert_not_called()
        generate.assert_not_called()
        self.assertEqual(before, (manifest.read_bytes(), bundle.read_bytes()))
        status = json.loads(report.read_text())
        self.assertEqual('deferred', status['status'])
        self.assertEqual('official-standings', status['deferredInput'])
        self.assertTrue(status['publishedDataPreserved'])

if __name__=='__main__':unittest.main()
