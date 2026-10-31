from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
import yaml

from scripts.maintain_live_results_cadence import active, probe, next_slot

NOW = datetime(2026,10,31,20,12,tzinfo=timezone.utc)


def document(checked=None, status='1H'):
    return {'contract':'statmaker-league-tables-v1','leagueCode':'I1','season':2026,
            'resultsCheckedAtUTC':(checked or NOW-timedelta(minutes=12)).isoformat(),
            'fixtures':[{'date_utc':(NOW-timedelta(minutes=27)).isoformat(),'status':status}]}


class Client:
    def __init__(self, doc, busy=False):
        self.doc, self.running, self.dispatched = doc, busy, 0
    def document(self, code): return self.doc
    def busy(self): return self.running
    def dispatch(self): self.dispatched += 1


class LiveResultsCadenceTest(unittest.TestCase):
    specs = {'I1': {'tier':'primary','season':2026}}

    def test_stale_milan_live_file_dispatches_the_existing_guarded_writer(self):
        client = Client(document())
        self.assertEqual('dispatched:I1',probe({'I1':document()},self.specs,client,NOW))
        self.assertEqual(1,client.dispatched)

    def test_current_five_minute_bucket_does_not_spend_another_provider_refresh(self):
        client = Client(document(NOW-timedelta(seconds=30)))
        self.assertEqual('fresh',probe({'I1':document()},self.specs,client,NOW))
        self.assertEqual(0,client.dispatched)

    def test_existing_queued_writer_is_not_duplicated(self):
        client = Client(document(),busy=True)
        self.assertEqual('already-running',probe({'I1':document()},self.specs,client,NOW))
        self.assertEqual(0,client.dispatched)

    def test_finished_fixture_stops_followup_checks(self):
        client = Client(document(status='FT'))
        self.assertEqual('inactive',probe({'I1':document()},self.specs,client,NOW))
        self.assertEqual('inactive',probe({'I1':client.doc},self.specs,client,NOW))
        self.assertEqual(0,client.dispatched)

    def test_wrong_season_is_rejected(self):
        client = Client({**document(), 'season':2025})
        with self.assertRaises(ValueError): probe({'I1':document()},self.specs,client,NOW)

    def test_unknown_leagues_or_stuck_historical_live_matches_do_not_add_calls(self):
        client = Client(document())
        self.assertEqual('inactive',probe({'I1':document()},{},client,NOW))
        old = document(); old['fixtures'][0]['date_utc']=(NOW-timedelta(days=1)).isoformat()
        self.assertFalse(active(old,NOW))

    def test_slots_are_aligned_to_the_existing_five_minute_buckets(self):
        self.assertEqual(datetime(2026,10,31,20,15,20,tzinfo=timezone.utc).timestamp(),next_slot(NOW))

    def test_postmatch_watchdog_completion_cannot_create_a_timer_loop(self):
        path=Path(__file__).resolve().parents[1]/'.github/workflows/live-results-cadence.yml'
        doc=yaml.safe_load(path.read_text())
        self.assertIn("github.event.workflow_run.event != 'workflow_run'",doc['jobs']['maintain']['if'])
        self.assertEqual('statmaker-live-results-cadence',doc['concurrency']['group'])
        self.assertNotIn('API_FOOTBALL_KEY',path.read_text())


if __name__ == '__main__': unittest.main()
