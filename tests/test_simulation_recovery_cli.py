import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import hotpatch_app_ready_simulation as hotpatch


class SimulationRecoveryCliTest(unittest.TestCase):
    def command(self, environment=None, **options):
        with patch.dict(os.environ, environment or {}, clear=True), patch.object(hotpatch.subprocess, "run") as run:
            hotpatch.run_uefa_materializer(Path("prepared.db"), **options)
            run.assert_called_once()
            self.assertTrue(run.call_args.kwargs["check"])
            command = run.call_args.args[0]
            self.assertEqual("50000", command[command.index("--runs") + 1])
            self.assertEqual("50000", command[command.index("--match-runs") + 1])
            return command

    def test_full_publisher_recovery_uses_published_tables_without_an_api_key(self):
        command = self.command({"CACHED_RECOVERY": "true"})
        self.assertIn("--cached-league-tables", command)
        self.assertNotIn("--matches-only", command)

    def test_existing_normal_refresh_keeps_its_provider_route(self):
        self.assertNotIn("--cached-league-tables", self.command())

    def test_explicit_uefa_only_update_is_always_cached(self):
        self.assertIn("--cached-league-tables", self.command(cached=True))

    def test_match_only_update_does_not_receive_conflicting_modes(self):
        command = self.command({"CACHED_RECOVERY": "true"}, matches_only=True)
        self.assertIn("--matches-only", command)
        self.assertNotIn("--cached-league-tables", command)


if __name__ == "__main__":
    unittest.main()
