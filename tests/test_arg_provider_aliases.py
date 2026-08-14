import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domestic_live_july_pipeline as pipeline
import refresh_domestic_odds_schedule_priority as schedule_priority

CASES = {'CA Aldosivi': 'Aldosivi', 'Argentinos Juniors': 'Argentinos JRS', 'CA Banfield': 'Banfield', 'CA Barracas Central': 'Barracas Central', 'CA Belgrano de Cordoba': 'Belgrano Cordoba', 'CA Central Cordoba SE': 'Central Cordoba de Santiago', 'Deportivo Riestra AFBC': 'Deportivo Riestra', 'Estudiantes de La Plata': 'Estudiantes L.P.', 'Estudiantes Rio Cuarto': 'Estudiantes de Rio Cuarto', 'Gimnasia y Esgrima La Plata': 'Gimnasia L.P.', 'Gimnasia y Esgrima Mendoza': 'Gimnasia M.', 'CA Huracan': 'Huracan', 'Independiente Rivadavia': 'Independ. Rivadavia', 'CA Independiente Avellaneda': 'Independiente', 'CA Lanus': 'Lanus', "Newell's Old Boys": 'Newells Old Boys', 'CA Platense': 'Platense', 'Racing Club Avellaneda': 'Racing Club', 'CA River Plate (ARG)': 'River Plate', 'CA Rosario Central': 'Rosario Central', 'CA San Lorenzo de Almagro': 'San Lorenzo', 'CA Sarmiento Junin': 'Sarmiento Junin', 'CA Talleres de Cordoba': 'Talleres Cordoba', 'CA Tigre': 'Tigre', 'Union de Santa Fe': 'Union Santa Fe'}

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
