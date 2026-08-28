import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domestic_live_july_pipeline as pipeline
import refresh_domestic_odds_schedule_priority as schedule_priority


ENGLAND_CASES = {
    "E0": {
        "Tottenham Hotspur": "Tottenham",
        "Newcastle United": "Newcastle",
        "Coventry City": "Coventry",
        "Hull City": "Hull City",
    },
    "E1": {
        "Derby County": "Derby",
        "Swansea City": "Swansea",
        "Wolverhampton Wanderers": "Wolves",
        "West Bromwich Albion": "West Brom",
        "Sheffield United": "Sheffield Utd",
        "Queens Park Rangers": "QPR",
        "Burnley FC": "Burnley",
        "Wrexham AFC": "Wrexham",
    },
    "E2": {
        "Leicester City": "Leicester",
        "Peterborough United": "Peterborough",
        "Bradford City FC": "Bradford",
        "Leyton Orient London": "Leyton Orient",
    },
    "E3": {
        "Tranmere Rovers FC": "Tranmere",
        "York City FC": "York",
        "Rochdale AFC": "Rochdale",
        "Accrington Stanley": "Accrington ST",
    },
}

CASES = {'CA Aldosivi': 'Aldosivi', 'Argentinos Juniors': 'Argentinos JRS', 'CA Banfield': 'Banfield', 'CA Barracas Central': 'Barracas Central', 'CA Belgrano de Cordoba': 'Belgrano Cordoba', 'CA Central Cordoba SE': 'Central Cordoba de Santiago', 'Deportivo Riestra AFBC': 'Deportivo Riestra', 'Estudiantes de La Plata': 'Estudiantes L.P.', 'Estudiantes Rio Cuarto': 'Estudiantes de Rio Cuarto', 'Gimnasia y Esgrima La Plata': 'Gimnasia L.P.', 'Gimnasia y Esgrima Mendoza': 'Gimnasia M.', 'CA Huracan': 'Huracan', 'Independiente Rivadavia': 'Independ. Rivadavia', 'CA Independiente Avellaneda': 'Independiente', 'CA Lanus': 'Lanus', "Newell's Old Boys": 'Newells Old Boys', 'CA Platense': 'Platense', 'Racing Club Avellaneda': 'Racing Club', 'CA River Plate (ARG)': 'River Plate', 'CA Rosario Central': 'Rosario Central', 'CA San Lorenzo de Almagro': 'San Lorenzo', 'CA Sarmiento Junin': 'Sarmiento Junin', 'CA Talleres de Cordoba': 'Talleres Cordoba', 'CA Tigre': 'Tigre', 'Union de Santa Fe': 'Union Santa Fe'}

class EnglandCurrentMembershipProviderAliasTest(unittest.TestCase):
    def test_current_provider_names_map_to_current_canonical_roster(self):
        registry = pipeline.load_json(pipeline.REGISTRY_PATH, {}).get("leagues", [])
        schedule_priority.target.domestic_odds_expansion.install(
            schedule_priority.target.odds_fetch,
            schedule_priority.target.pipeline,
        )
        aliases = schedule_priority.target.pipeline.generated_aliases(registry)
        schedule_priority._install_conservative_team_mapping()

        for league_code, cases in ENGLAND_CASES.items():
            for provider_team, expected in cases.items():
                with self.subTest(league=league_code, provider_team=provider_team):
                    debug = {}
                    mapped, canonical = schedule_priority.target.odds_fetch.canonical_team_info(
                        provider_team, league_code, aliases, debug
                    )
                    self.assertEqual(expected, mapped)
                    self.assertEqual(expected, canonical)
                    self.assertFalse(debug.get("unmatchedTeams"))


class ArgentinaProviderAliasTest(unittest.TestCase):
    def test_all_observed_imminent_provider_names_map_exactly(self):
        registry = pipeline.load_json(pipeline.REGISTRY_PATH, {}).get("leagues", [])
        aliases = pipeline.generated_aliases(registry)
        schedule_priority._install_conservative_team_mapping()
        for provider_team, expected in CASES.items():
            with self.subTest(provider_team=provider_team):
                debug = {}
                mapped, canonical = schedule_priority.target.odds_fetch.canonical_team_info(
                    provider_team, "ARG", aliases, debug
                )
                self.assertEqual(expected, mapped)
                self.assertEqual(expected, canonical)
                self.assertFalse(debug.get("unmatchedTeams"))

if __name__ == "__main__":
    unittest.main()
