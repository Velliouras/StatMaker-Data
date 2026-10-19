import copy
import unittest
import gzip
import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
import numpy as np
from .features import History, counts
from .model import pmf, probabilities, distribution, conservative, favourite_guard
from .feed import bind_quote, raw_offer_index, settle


class ContractTest(unittest.TestCase):
    def test_total_over_does_not_require_picking_the_dominant_team(self):
        # Either team can contribute to the sum; swapping team order cannot change it.
        replicas = [dict(shots=[14.7, 13.], sot=[5., 4.3], alpha=.06)]
        swapped = [dict(shots=[13., 14.7], sot=[4.3, 5.], alpha=.06)]
        first = conservative(replicas, "shots", "TOTAL", "", "OVER", 24.5)
        second = conservative(swapped, "shots", "TOTAL", "", "OVER", 24.5)
        self.assertAlmostEqual(first[0], second[0], places=12)
        self.assertTrue(favourite_guard([[1, 67], [0, -67]], replicas, "AWAY", "shots", "TOTAL", "", "OVER"))
        self.assertTrue(favourite_guard([[1, 67], [0, -67]], replicas, None, "shots", "TOTAL", "", "OVER"))

    def test_team_prop_keeps_favourite_direction_and_elo_safeguards(self):
        pair = [[1, 67], [0, -67]]
        replicas = [dict(shots=[20., 8.], sot=[7., 2.5], alpha=.06)]
        self.assertTrue(favourite_guard(pair, replicas, "HOME", "shots", "TEAM", "HOME", "OVER"))
        self.assertFalse(favourite_guard(pair, replicas, "HOME", "shots", "TEAM", "AWAY", "OVER"))
        self.assertFalse(favourite_guard(pair, replicas, "AWAY", "shots", "TEAM", "AWAY", "OVER"))
        self.assertFalse(favourite_guard(pair, replicas, None, "shots", "TEAM", "HOME", "OVER"))

    def test_history_refresh_does_not_duplicate_renamed_api_fixture(self):
        from . import feed
        old = dict(fixture_id=1, _date=100000., _home="old", _away="a")
        new = dict(old, _home="canonical")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.gz"
            path.write_bytes(gzip.compress(json.dumps([old]).encode()))
            with patch.object(feed, "HISTORY", path), patch("scripts.betting_v2.opponent_matchup.load_competitive_history", return_value=([new], {})):
                result = feed.load_history()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["_home"], "canonical")

    def test_public_commit_precedes_kickoff_and_cannot_reprice_issue(self):
        from . import feed
        row = dict(selectionId="s1", publishedAt=None, kickoff="2026-10-19T15:00:00Z", odd=1.91)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); out = root / "feed"; out.mkdir()
            (out / "ledger.json").write_text(json.dumps(dict(rows=[row])))
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "add", "feed/ledger.json"], cwd=root, check=True)
            env = dict(os.environ, GIT_AUTHOR_DATE="2026-10-19T10:00:00Z", GIT_COMMITTER_DATE="2026-10-19T10:00:00Z")
            subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "issue"], cwd=root, env=env, check=True)
            with patch.object(feed, "ROOT", root), patch.object(feed, "OUT", out):
                verified = feed.verify_publication([row])[0]
                self.assertTrue(verified["publicationCommit"])
                self.assertLess(feed.timestamp(verified["publishedAt"]), feed.timestamp(row["kickoff"]))
                with self.assertRaises(ValueError): feed.verify_publication([dict(row, odd=2.2)])

    def test_future_result_never_enters_features(self):
        rows = [dict(_date=100000., _home="h", _away="a", home_goals=2, away_goals=0,
                     normalized_stats=dict(HS=20, AS=5, HST=8, AST=1))]
        history = History(rows)
        history.advance(100001.)
        self.assertEqual(history.pair("h", "a", "L", 100002)[0][4], 0)
        history.advance(186400.)
        self.assertEqual(history.pair("h", "a", "L", 200000)[0][4], 1)
        with self.assertRaises(ValueError): history.advance(100000.)

    def test_inconsistent_sot_is_not_capped_into_training(self):
        self.assertIsNone(counts(dict(normalized_stats=dict(HS=2, AS=4, HST=3, AST=1))))

    def test_numerical_distribution_and_push_contract(self):
        p = pmf(13.5, .06)
        self.assertAlmostEqual(p.sum(), 1., places=9)
        self.assertAlmostEqual(np.dot(np.arange(len(p)), p), 13.5, places=7)
        over, push = probabilities(p, "OVER", 13)
        under, same_push = probabilities(p, "UNDER", 13)
        self.assertAlmostEqual(over + under + push, 1., places=9)
        self.assertEqual(push, same_push)
        self.assertEqual(probabilities(p, "OVER", 13.5)[1], 0.)
        with self.assertRaises(ValueError): probabilities(p, "OVER", 13.25)

    def test_sot_is_binomial_thinning_of_shots(self):
        sh = pmf(14, .06); sot = pmf(14 * .35, .06)
        # For binomial thinning E[SOT]=p*E[shots], Var[SOT]=p(1-p)E[N]+p^2 Var[N].
        x = np.arange(len(sh)); y = np.arange(len(sot))
        var_sh = np.dot((x - 14) ** 2, sh)
        var_sot = np.dot((y - 4.9) ** 2, sot)
        self.assertAlmostEqual(var_sot, .35 * .65 * 14 + .35 ** 2 * var_sh, places=7)

    def test_lower_envelope_does_not_raise_probability(self):
        replicas = [dict(shots=[14, 10], sot=[5, 3], alpha=.06), dict(shots=[12, 11], sot=[4, 3], alpha=.08)]
        win, push = conservative(replicas, "shots", "TOTAL", "", "OVER", 22.5)
        for r in replicas:
            self.assertLessEqual(win, probabilities(distribution(r, "shots", "TOTAL", ""), "OVER", 22.5)[0])
        self.assertEqual(push, 0.)

    def test_exact_quote_rejects_conflicting_book_price(self):
        m = dict(id="7", kickoff="2026-10-21T15:00:00Z", providerHomeTeam="H", providerAwayTeam="A")
        q = dict(selection="Shots Over 24.5", line=24.5, odds=1.91, bookmaker="B",
                 providerMarketName="Total Shots", exactBookmakerOdds=True)
        market = dict(name="Total Shots", updatedAt="2026-10-20T15:00:00Z", odds=[dict(hdp=24.5, over="1.91")],
                      _kickoff=m["kickoff"], _home="H", _away="A")
        raw = {("7", "B", "Total Shots"): [market]}
        self.assertIsNotNone(bind_quote(m, q, raw))
        raw["7", "B", "Total Shots"].append(dict(market, odds=[dict(hdp=24.5, over="1.99")]))
        self.assertIsNone(bind_quote(m, q, raw))

    def test_settlement_missing_target_stays_pending_and_price_is_immutable(self):
        row = dict(apiFixtureId=1, kickoff="2026-10-19T15:00:00Z", publishedAt="2026-10-19T10:00:00Z",
                   outcome="PENDING", metric="sot", scope="TOTAL", side="OVER", teamSide="", line=7.5, odd=1.91)
        from .feed import timestamp
        ko = timestamp(row["kickoff"])
        result = dict(fixture_id=1, _date=ko, normalized_stats=dict(HS=15, AS=10))
        self.assertEqual(settle(row, [result], ko + 15000)["outcome"], "PENDING")
        result["normalized_stats"].update(HST=5, AST=4)
        scored = settle(row, [result], ko + 15000)
        self.assertEqual(scored["outcome"], "WON")
        self.assertEqual(scored["settlementReturn"], 1.91)
        self.assertEqual(scored["odd"], row["odd"])
        self.assertEqual(row["outcome"], "PENDING")
        row["line"] = 9.
        self.assertEqual(settle(row, [result], ko + 15000)["settlementReturn"], 1.)


if __name__ == "__main__": unittest.main()
