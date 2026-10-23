import copy
import json
import random
import unittest
from unittest.mock import Mock

from scripts import materialize_prepared_uefa_simulations as sim
from scripts import uefa_club_strengths as strengths


class UefaSimulationModelV2Test(unittest.TestCase):
    def test_top_two_seeds_have_opposite_semifinal_paths_and_can_meet_only_in_final(self):
        groups = [[1, 2], [3, 4], [5, 6], [7, 8]]
        for swap in range(16):
            drawn = [list(reversed(g)) if swap & (1 << i) else g for i, g in enumerate(groups)]
            quarter_winners = [min(a, b) for a, b in sim.quarterfinal_pairs(drawn)]
            semifinals = sim.semifinal_pairs(quarter_winners)
            self.assertFalse(any(1 in tie and 2 in tie for tie in semifinals))
            self.assertEqual({1, 2}, {min(tie) for tie in semifinals})

    def test_fixed_quarterfinal_paths_are_not_redrawn(self):
        self.assertEqual([('a0', 'd0'), ('a1', 'd1'), ('b0', 'c0'), ('b1', 'c1')],
                         sim.quarterfinal_pairs([['a0', 'a1'], ['b0', 'b1'],
                                                 ['c0', 'c1'], ['d0', 'd1']]))

    def test_return_leg_and_extra_time_are_at_seeded_path_home(self):
        model = Mock()
        model.draw.side_effect = [(0, 0), (0, 0), (1, 0)]
        self.assertEqual('seed', sim.two_leg_winner('seed', 'visitor', model, random.Random(2)))
        self.assertEqual(('visitor', 'seed'), model.draw.call_args_list[0].args[:2])
        self.assertEqual(('seed', 'visitor'), model.draw.call_args_list[1].args[:2])
        self.assertTrue(model.draw.call_args_list[2].kwargs['extra_time'])

    def test_final_and_final_extra_time_are_neutral(self):
        model = Mock()
        model.draw.side_effect = [(1, 1), (0, 1)]
        self.assertEqual('b', sim.final_winner('a', 'b', model, random.Random(1)))
        self.assertTrue(all(c.kwargs.get('neutral') for c in model.draw.call_args_list))
        self.assertTrue(model.draw.call_args_list[1].kwargs['extra_time'])

    def test_shootout_does_not_reuse_elo_without_keeper_evidence(self):
        for value, winner in ((.1, 'a'), (.9, 'b')):
            model = Mock(); model.draw.return_value = (0, 0)
            rng = Mock(); rng.random.return_value = value
            self.assertEqual(winner, sim.two_leg_winner('a', 'b', model, rng))

    def test_away_goals_wins_and_away_wins_break_ties_in_uefa_order(self):
        for field in ('away_gf', 'wins', 'away_wins'):
            a = sim.TableRow(played=6, points=10, gf=8, ga=4)
            z = copy.deepcopy(a)
            setattr(z, field, 2)
            table = {'AlphabeticallyFirst': a, 'Z': z}
            self.assertEqual('Z', sim.ranking(table)[0])
        a = sim.TableRow(points=10, gf=8, ga=4, away_gf=3, wins=1)
        z = sim.TableRow(points=10, gf=8, ga=4, away_gf=2, wins=5)
        self.assertEqual('A', sim.ranking({'A': a, 'Z': z})[0])

    def test_opponents_points_then_goal_difference_then_goals_are_used(self):
        for field in ('points', 'gf'):
            table = {'A': sim.TableRow(points=10, gf=8, ga=4),
                     'Z': sim.TableRow(points=10, gf=8, ga=4),
                     'weak': sim.TableRow(), 'strong': sim.TableRow()}
            setattr(table['strong'], field, 2)
            self.assertLess(sim.ranking(table, {'A': ['weak'], 'Z': ['strong']}).index('Z'),
                            sim.ranking(table, {'A': ['weak'], 'Z': ['strong']}).index('A'))

    def test_unobserved_final_tie_is_not_alphabetical(self):
        table = {'A': sim.TableRow(), 'Z': sim.TableRow()}
        self.assertEqual('Z', sim.ranking(table, tie_order={'A': .9, 'Z': .1})[0])
        self.assertEqual('A', sim.ranking(table, tie_order={'A': .1, 'Z': .9})[0])

    def test_result_updates_all_sporting_tiebreak_fields(self):
        table = {'a': sim.TableRow(), 'b': sim.TableRow()}
        sim.apply_result(table, 'a', 'b', 1, 3)
        self.assertEqual((3, 3, 1, 1), (table['b'].points, table['b'].away_gf,
                                      table['b'].wins, table['b'].away_wins))

    def test_real_club_and_federation_ratings_are_distinct_within_same_pot(self):
        _, mapped, _ = strengths.load_catalog()
        self.assertEqual('GRE', mapped['Panathinaikos']['country'])
        self.assertEqual('ENG', mapped['Brighton']['country'])
        self.assertNotEqual(mapped['Panathinaikos']['elo'], mapped['Brighton']['elo'])
        self.assertEqual('GIB', mapped['Lincoln Red Imps']['country'])
        self.assertEqual(108, len(mapped))

    def test_full_profiles_use_only_real_xg_or_neutral_missing_data(self):
        config = json.loads(sim.CONFIG_PATH.read_text())
        profiles = strengths.xg_profiles(config, sim.ROOT, sim.goal_means)
        self.assertEqual(108, len(profiles))
        self.assertGreater(profiles['Panathinaikos']['samples'], 0)
        for p in profiles.values():
            self.assertLessEqual(p['samples'], strengths.XG_MAX_MATCHES)
            self.assertTrue(.65 <= p['attack'] <= 1.5)
            self.assertTrue(.65 <= p['defense'] <= 1.5)
            if not p['samples']:
                self.assertEqual((1.0, 1.0), (p['attack'], p['defense']))

    def test_source_elo_results_already_in_snapshot_are_not_counted_twice(self):
        config = json.loads(sim.CONFIG_PATH.read_text())
        comp = next(c for c in config['competitions'] if c['competitionId']=='conference_league')
        fixtures, logos, _, league_id = sim.cached_competition(comp, int(config['season']))
        snapshot, mapped, _ = strengths.load_catalog()
        snapshot = dict(snapshot, throughDate=max(f.date for f in fixtures if f.status in sim.FINISHED))
        profiles = {t: {'attack': 1., 'defense': 1., 'samples': 0} for t in mapped}
        _, rows = sim.simulate_competition(comp, fixtures, logos, 10, league_id,
                                          (snapshot, mapped, profiles))
        row = next(r for r in rows if r['team_name']=='Panathinaikos')
        self.assertEqual(mapped['Panathinaikos']['elo'], row['strength_rating'])
        changed = dict(comp, potBaseRatings=[100.] * 6)
        _, other = sim.simulate_competition(changed, fixtures, logos, 10, league_id,
                                           (snapshot, mapped, profiles))
        self.assertEqual(rows, other)

    def test_neutral_final_means_have_no_home_advantage(self):
        model = sim.ScoreModel({'a': 1600, 'b': 1600}, 1.8, 1.1)
        h, a = model.means('a', 'b', neutral=True)
        self.assertEqual(h, a)

    def test_xg_adjustment_and_draws_share_the_same_expected_goals(self):
        model = sim.ScoreModel({'a': 1600, 'b': 1600}, 1.5, 1.2,
                              {'a': {'attack': 1.4, 'defense': .8}})
        base = sim.ScoreModel({'a': 1600, 'b': 1600}, 1.5, 1.2)
        self.assertGreater(model.means('a', 'b')[0], base.means('a', 'b')[0])
        self.assertLess(model.means('a', 'b')[1], base.means('a', 'b')[1])


if __name__ == '__main__':
    unittest.main()
