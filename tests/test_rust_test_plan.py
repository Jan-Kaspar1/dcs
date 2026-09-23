"""The Rust test split must keep all workspace checks and rerun only exact slow proofs."""
import unittest

from scripts.run_rust_tests import CONSUMER_PROOFS, command_plan


class RustTestPlanTests(unittest.TestCase):
    def test_plan_names_the_two_nested_cargo_proofs(self):
        self.assertEqual(
            CONSUMER_PROOFS,
            (
                (
                    "consumer-release",
                    "consumer_release",
                    "consumer_resolves_composes_emits_and_passes_released_tooling",
                ),
                (
                    "consumer-upgrade",
                    "consumer_upgrade",
                    "a_repin_within_the_minor_series_is_a_drop_in_upgrade",
                ),
            ),
        )

    def test_workspace_command_keeps_default_test_and_doc_coverage(self):
        plan = command_plan()
        workspace = plan["workspace"]

        self.assertEqual(
            workspace[:4], ["cargo", "test", "--workspace", "--locked"]
        )
        self.assertEqual(workspace[4], "--")
        self.assertEqual(
            workspace[5:],
            [part for _, _, name in CONSUMER_PROOFS
             for part in ("--skip", name)],
        )
        self.assertNotIn("--no-run", workspace)

    def test_each_skipped_consumer_proof_runs_once_as_a_targeted_test(self):
        plan = command_plan()

        for label, target, test_name in CONSUMER_PROOFS:
            self.assertEqual(
                plan[label],
                [
                    "cargo",
                    "test",
                    "-p",
                    "dcs-build",
                    "--test",
                    target,
                    "--locked",
                    test_name,
                ],
            )

        self.assertEqual(len(plan), len(CONSUMER_PROOFS) + 1)


if __name__ == "__main__":
    unittest.main()
