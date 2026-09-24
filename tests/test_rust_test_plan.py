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

    def test_workspace_scope_is_exactly_the_fast_leg(self):
        plan = command_plan("workspace")

        self.assertEqual(list(plan), ["workspace"])
        self.assertEqual(plan, {"workspace": command_plan()["workspace"]})

    def test_proofs_scope_is_exactly_the_targeted_reruns(self):
        plan = command_plan("proofs")

        self.assertEqual(len(plan), len(NESTED_PROOFS))
        self.assertNotIn("workspace", plan)
        for label, _, _ in NESTED_PROOFS:
            self.assertEqual(plan[label], command_plan()[label])

    def test_scopes_partition_the_full_plan_without_overlap(self):
        full = command_plan()
        workspace = command_plan("workspace")
        proofs = command_plan("proofs")

        self.assertTrue(set(workspace).isdisjoint(proofs))
        self.assertEqual(full, {**workspace, **proofs})
        # Every skipped proof has exactly one targeted rerun and vice
        # versa: no proof is dropped and none runs twice in one scope.
        skipped = [
            name
            for index, part in enumerate(workspace["workspace"])
            if part == "--skip"
            for name in (workspace["workspace"][index + 1],)
        ]
        self.assertEqual(skipped, [name for _, _, name in NESTED_PROOFS])
        self.assertEqual(
            sorted(command[-1] for command in proofs.values()),
            sorted(name for _, _, name in NESTED_PROOFS),
        )


if __name__ == "__main__":
    unittest.main()
