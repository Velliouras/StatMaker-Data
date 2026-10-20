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
    def test_old_client_read_model_excludes_p55_without_losing_or_rewriting_issues(self):
        from .feed import compatible_p60_payload
        high=dict(selectionId="old",winProbability=.65,odd=1.91,publishedAt="original")
        developing=dict(selectionId="new",winProbability=.57,odd=1.91,publishedAt="later")
        payload=dict(candidates=[high,developing],issued=[high,developing],candidateCount=2)
        legacy=compatible_p60_payload(payload)
        self.assertEqual(legacy["candidateCount"],1)
        self.assertEqual(legacy["candidates"],[high]);self.assertEqual(legacy["issued"],[high])
        self.assertEqual(payload["issued"],[high,developing]);self.assertEqual(payload["candidateCount"],2)

    def test_p55_admission_keeps_positive_ev_and_exact_odds_bounds(self):
        from .model import eligible_quote
        self.assertTrue(eligible_quote(.55, 0., 1.91))
        self.assertFalse(eligible_quote(.549, 0., 1.91))
        self.assertFalse(eligible_quote(.55, 0., 1.81))
        self.assertFalse(eligible_quote(.75, 0., 1.80))
        self.assertFalse(eligible_quote(.75, 0., 3.00))
        self.assertFalse(eligible_quote(.95, .1, 2.))

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



class SourceContractTest(unittest.TestCase):
    def test_context_deduplicates_provider_entries_for_the_same_player(self):
        from .intelligence import context_facts
        from .feed import timestamp
        player=dict(id=667,name='M. Depay',reason='Injury')
        context=dict(generatedAt='2026-10-20T07:00:00Z',home=dict(injuriesCovered=True,unavailablePlayers=[player,dict(player)]))
        facts=context_facts(dict(match={}),context,timestamp('2026-10-20T08:00:00Z'))
        self.assertEqual(facts['teams']['home']['unavailablePlayers'],[player])
        self.assertTrue(facts['fresh']);self.assertFalse(facts['usedByFrozenProbability'])

    def test_game_intelligence_no_vig_uses_same_book_and_handles_pushes(self):
        from .intelligence import market_facts
        from .feed import timestamp
        now=timestamp('2026-10-20T08:00:00Z')
        m=dict(kickoff='2026-10-21T15:00:00Z',providerHomeTeam='H',providerAwayTeam='A')
        row=dict(match=m,fixtureId='7',metric='shots',scope='TOTAL',teamSide='',side='OVER',line=24.,bookmaker='B',
                 odd=1.9,winProbability=.6,pushProbability=.1,indicativeEV=.24)
        quote=dict(_observedAt=now-60,_kickoff=m['kickoff'],_home='H',_away='A',odds=[dict(hdp=24,over='1.9',under='1.9')])
        raw={('7','B','Total Shots'):[quote]};facts=market_facts(row,raw,now)
        self.assertAlmostEqual(facts['currentSameBook']['marketConditionalProbability'],.5)
        self.assertAlmostEqual(facts['edgeVsMarketPercentagePoints'],100/6)
        self.assertAlmostEqual(facts['modelFairOdd'],1.5)
        self.assertAlmostEqual(facts['breakEvenWinProbability'],.9/1.9)
        raw={('7','B','Total Shots'):[dict(quote,odds=[dict(hdp=24,over='1.9')])],
             ('7','C','Total Shots'):[dict(quote,odds=[dict(hdp=24,under='1.9')])]}
        self.assertNotIn('marketConditionalProbability',market_facts(row,raw,now)['currentSameBook'])
        raw[('7','B','Total Shots')].append(dict(quote,odds=[dict(hdp=24,over='1.99')]))
        self.assertNotIn('currentSameBook',market_facts(row,raw,now))

    def test_future_context_is_not_presented_as_current(self):
        from .intelligence import context_facts
        from .feed import timestamp
        context=dict(generatedAt='2026-10-21T08:00:00Z',home=dict(injuriesCovered=True,unavailablePlayers=['Future news']))
        facts=context_facts(dict(match={}),context,timestamp('2026-10-20T08:00:00Z'))
        self.assertIsNone(facts['asOf']);self.assertFalse(facts['fresh'])
        self.assertFalse(facts['teams']['home']['injuriesCovered'])
        self.assertEqual(facts['teams']['home']['unavailablePlayers'],[])

    def test_game_intelligence_keeps_original_prediction_and_cutoff(self):
        from .intelligence import annotate
        from .feed import timestamp
        cutoff=timestamp('2026-10-19T12:00:00Z')
        actual=[dict(_home='england:h',_away='england:a',_date=cutoff-(10-i)*86400,
                     home_goals=2,away_goals=1,normalized_stats=dict(HS=12,AS=8,HST=4,AST=2)) for i in range(8)]
        actual.append(dict(actual[-1],_date=cutoff-3600,normalized_stats=dict(HS=999,AS=999,HST=999,AST=999)))
        m=dict(kickoff='2026-10-21T15:00:00Z',leagueCode='E0',providerHomeTeam='H',providerAwayTeam='A')
        row=dict(selectionId='s',featureCutoff='2026-10-19T12:00:00Z',match=m,kickoff=m['kickoff'],fixtureId='7',apiFixtureId=99,
                 metric='shots',scope='TOTAL',teamSide='',side='OVER',line=24.5,bookmaker='B',homeTeam='H',awayTeam='A',
                 odd=1.9,winProbability=.6,pushProbability=0.,indicativeEV=.14)
        before=copy.deepcopy(row)
        with patch('scripts.shots_uat.intelligence.Identities') as identity:
            identity.return_value.bind.return_value=((m,'england:h','england:a',99),'exact')
            result=annotate([row],actual,{},[],cutoff+86400)[0]
        self.assertEqual(row,before)
        self.assertEqual({k:v for k,v in result.items() if k!='intelligence'},before)
        facts=result['intelligence'];self.assertFalse(facts['changesPrediction'])
        self.assertEqual(facts['historicalFacts']['home']['sample'],8)
        self.assertEqual(facts['historicalFacts']['home']['averages']['own_8_shots_for'],12.)

    def test_reobservation_does_not_reprice_or_mutate_first_issue(self):
        from .feed import observe_issued_quote,timestamp
        m=dict(id='7',kickoff='2026-10-22T15:00:00Z',providerHomeTeam='H',providerAwayTeam='A')
        row=dict(match=m,market='MATCH_SHOTS',scope='TOTAL',teamSide='',selection='Shots Over 24.5',line=24.5,
                 odd=1.91,bookmaker='B',quoteUpdatedAt='2026-10-19T15:00:00Z',winProbability=.65)
        before=copy.deepcopy(row);now=timestamp('2026-10-20T20:00:00Z')
        market=dict(_observedAt=now-3600,_kickoff=m['kickoff'],_home='H',_away='A',odds=[dict(hdp=24.5,over='1.91')])
        key=('7','B','Total Shots')
        observed=observe_issued_quote(row,{key:[market]},now)
        self.assertEqual(observed['latestQuoteObservedAt'],'2026-10-20T19:00:00Z')
        self.assertEqual(row,before)
        for field in before:self.assertEqual(observed[field],before[field])
        self.assertIsNone(observe_issued_quote(row,{key:[dict(market,odds=[dict(hdp=24.5,over='1.99')])]},now)['latestQuoteObservedAt'])
        self.assertIsNone(observe_issued_quote(row,{},now)['latestQuoteObservedAt'])

    def test_recent_observation_accepts_unchanged_price_without_rewriting_provider_clock(self):
        from .sources import latest_markets
        from .feed import timestamp
        m = dict(id='7', kickoff='2026-10-21T15:00:00Z', providerHomeTeam='H', providerAwayTeam='A')
        q = dict(selection='Shots Over 24.5', line=24.5, odds=1.91, bookmaker='B',
                 providerMarketName='Total Shots', exactBookmakerOdds=True)
        market = dict(updatedAt='2026-10-15T15:00:00Z', odds=[dict(hdp=24.5, over='1.91')],
                      _kickoff=m['kickoff'], _home='H', _away='A')
        key = ('7','B','Total Shots'); observed = timestamp('2026-10-20T15:00:00Z')
        fresh = dict(market, _observedAt=observed)
        merged = latest_markets({key:[market]}, {key:[fresh]})
        self.assertEqual(bind_quote(m,q,merged), ('OVER',observed))
        self.assertEqual(merged[key][0]['updatedAt'],market['updatedAt'])
        stale_clock=bind_quote(m,q,{key:[market]})[1]
        self.assertGreater(observed-stale_clock,86400)

    def test_new_market_observation_does_not_retain_withdrawn_old_lines(self):
        from .sources import latest_markets
        key=('7','B','Total Shots')
        old=dict(_observedAt=100.,odds=[dict(hdp=24.5,over='1.91')])
        new=dict(_observedAt=200.,odds=[dict(hdp=25.5,over='1.91')])
        self.assertEqual(latest_markets({key:[old]},{key:[new]})[key],[new])

    def test_domestic_poll_cannot_refresh_rotated_or_preserved_payloads(self):
        from .sources import poll_observations
        league=dict(leagueCode='L', matches=[dict(id='7',providerMarkets=[{}])])
        clock='2026-10-19T15:00:00Z'
        archive=dict(generatedAt=clock,leagues=[league])
        debug=dict(generatedAt=clock,leaguesRequested=['L'],leagueReports=[dict(leagueCode='L',providerMatchesArchived=1,providerMarketPayloadsArchived=1)])
        odds=dict(generatedAt=clock,debug=debug)
        self.assertIn('L',poll_observations(odds,archive))
        debug['leaguesRequested']=[]
        self.assertEqual(poll_observations(odds,archive),{})
        debug['leaguesRequested']=['L'];archive['preservedAfterEmptyRefresh']=['L']
        self.assertEqual(poll_observations(odds,archive),{})

    def test_observation_ledger_requires_exact_payload_hash_and_prior_recent_clock(self):
        from .sources import domestic_observations,payload_sha
        from .feed import timestamp
        league=dict(leagueCode='L',matches=[dict(id='7',providerMarkets=[])])
        archive=dict(leagues=[league]);now=timestamp('2026-10-19T16:00:00Z')
        proof=dict(payloadSha256=payload_sha(league),observedAt='2026-10-19T15:00:00Z')
        self.assertIn('L',domestic_observations({},archive,{'L':proof},now))
        self.assertEqual(domestic_observations({},archive,{'L':dict(proof,payloadSha256='wrong')},now),{})
        self.assertEqual(domestic_observations({},archive,{'L':dict(proof,observedAt='2026-10-20T15:00:00Z')},now),{})
        self.assertEqual(domestic_observations({},archive,{'L':dict(proof,observedAt='2026-10-17T15:00:00Z')},now),{})

    def test_uefa_only_observes_successful_current_requests_without_preservation(self):
        from .sources import uefa_packet
        m=dict(id='7',kickoff='2026-10-21T15:00:00Z',providerHomeTeam='H',providerAwayTeam='A',
               markets=[dict(exactBookmakerOdds=True,providerMarketName='Total Shots',bookmaker='B',
                             market='MATCH_SHOTS',selection='Over 24.5',line=24.5,odds=1.91)])
        debug=dict(oddsRefreshSkippedByRateLimit=False,preservedPreviousExactMarketRows=0,
                   apiCalls=[dict(status=200,path='/odds/multi',params=dict(eventIds='7'))])
        doc=dict(leagueCode='CL',country='Europe',competition='CL',season='2026',matches=[m],debug=debug,generatedAt='2026-10-19T15:00:00Z')
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'uefa.json';path.write_text(json.dumps(doc))
            self.assertTrue(uefa_packet(path,'champions_league')[1])
            for key,value in [('preservedPreviousExactMarketRows',1),('oddsRefreshSkippedByRateLimit',True)]:
                broken=copy.deepcopy(doc);broken['debug'][key]=value;path.write_text(json.dumps(broken))
                self.assertFalse(uefa_packet(path,'champions_league')[1])
            doc['debug']['apiCalls'][0]['params']['eventIds']='8';path.write_text(json.dumps(doc))
            self.assertFalse(uefa_packet(path,'champions_league')[1])

    def test_exact_schedule_resolves_global_name_ambiguity_and_rejects_wrong_fixture(self):
        from .sources import Identities
        history=[dict(_home='england:arsenal',_away='france:psg'),dict(_home='argentina:arsenal',_away='england:arsenal')]
        ko='2026-10-21T15:00:00Z'
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);tables=root/'data/statmaker/league_tables';tables.mkdir(parents=True)
            domestic=dict(leagueCode='E0',country='England',fixtures=[dict(fixture_id=1,date_utc=ko,home_team='Arsenal',away_team='Arsenal',home_team_id=42,away_team_id=42)])
            european=dict(leagueCode='CL',fixtures=[dict(fixture_id=99,date_utc=ko,home_team='Arsenal',away_team='PSG',home_team_id=42,away_team_id=85)])
            for name,doc in [('domestic',domestic),('european',european)]: (tables/(name+'.json')).write_text(json.dumps(doc))
            identities=Identities(root,history);league=dict(leagueCode='CL',country='Europe')
            match=dict(kickoff=ko,homeTeam='Arsenal',awayTeam='PSG')
            bound,reason=identities.bind(league,match)
            self.assertEqual(bound[1:4],('england:arsenal','france:psg',99))
            self.assertEqual(reason,'cachedExactSchedule')
            self.assertIsNone(identities.bind(league,dict(match,squadContext=dict(apiFootballFixtureId=100)))[0])
            self.assertIsNone(identities.bind(league,dict(match,kickoff='2026-10-21T16:00:00Z'))[0])
            european['fixtures'].append(dict(european['fixtures'][0],fixture_id=100));(tables/'european.json').write_text(json.dumps(european))
            self.assertIsNone(Identities(root,history).bind(league,match)[0])


if __name__ == "__main__": unittest.main()
