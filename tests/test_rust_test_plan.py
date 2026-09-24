"""The Rust test split must keep all workspace checks and rerun exact slow proofs."""
import unittest

from scripts.run_rust_tests import NESTED_PROOFS, command_plan


class RustTestPlanTests(unittest.TestCase):
    def test_plan_names_the_nested_cargo_proofs(self):
        self.assertEqual(
            NESTED_PROOFS,
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
                (
                    "reference-template",
                    "reference_plant",
                    "the_template_passes_its_own_clean_ci_outside_the_workspace",
                ),
                (
                    "reference-upgrade",
                    "reference_plant",
                    "the_upgrade_stage_proves_the_repin_and_the_named_crossings",
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
            [part for _, _, name in NESTED_PROOFS
             for part in ("--skip", name)],
        )
        self.assertNotIn("--no-run", workspace)

    def test_each_skipped_consumer_proof_runs_once_as_a_targeted_test(self):
        plan = command_plan()

        for label, target, test_name in NESTED_PROOFS:
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

        self.assertEqual(len(plan), len(NESTED_PROOFS) + 1)


if __name__ == "__main__":
    unittest.main()
