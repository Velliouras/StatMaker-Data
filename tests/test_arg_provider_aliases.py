import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import domestic_live_july_pipeline as pipeline
import refresh_domestic_odds_schedule_priority as schedule_priority


class ArgentinaProviderAliasTest(unittest.TestCase):
    def test_current_arg_provider_names_map_deterministically(self):
        registry_payload = pipeline.load_json(pipeline.REGISTRY_PATH, {})
        registry = registry_payload.get("leagues", [])
        aliases = pipeline.generated_aliases(registry)
        schedule_priority._install_conservative_team_mapping()
        cases = {
            "Argentinos Juniors": "Argentinos JRS",
            "CA Central Cordoba SE": "Central Cordoba de Santiago",
            "Estudiantes de La Plata": "Estudiantes L.P.",
            "Gimnasia y Esgrima La Plata": "Gimnasia L.P.",
            "Gimnasia y Esgrima Mendoza": "Gimnasia M.",
            "Independiente Rivadavia": "Independ. Rivadavia",
            "Racing Club Avellaneda": "Racing Club",
        }
        for provider_team, expected in cases.items():
            with self.subTest(provider_team=provider_team):
                mapped, canonical = schedule_priority.target.odds_fetch.canonical_team_info(
                    provider_team, "ARG", aliases, {}
                )
                self.assertEqual(expected, mapped)
                self.assertEqual(expected, canonical)


if __name__ == "__main__":
    unittest.main()
