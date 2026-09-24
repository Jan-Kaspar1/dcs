"""Deterministic sharding tests for the supervisor test runner."""
import unittest

from scripts.run_tests import shard_test_cases


class ShardingTests(unittest.TestCase):
    def make_cases(self, sizes):
        cases = []
        groups = []
        for index, size in enumerate(sizes):
            group = type(
                f"SyntheticGroup{index}",
                (unittest.TestCase,),
                {f"test_{case}": (lambda self: None) for case in range(size)},
            )
            groups.append(group)
            cases.extend(
                unittest.defaultTestLoader.loadTestsFromTestCase(group)
            )
        return cases, groups

    def test_shards_are_repeatable_complete_and_keep_classes_together(self):
        cases, groups = self.make_cases((9, 7, 5, 4, 3, 2, 1))
        first = shard_test_cases(cases, 4)
        second = shard_test_cases(cases, 4)

        self.assertEqual(
            [[case.id() for case in shard] for shard in first],
            [[case.id() for case in shard] for shard in second],
        )
        self.assertCountEqual(
            [case.id() for shard in first for case in shard],
            [case.id() for case in cases],
        )
        self.assertLessEqual(max(map(len, first)) - min(map(len, first)), 5)
        for group in groups:
            owners = {
                index
                for index, shard in enumerate(first)
                if any(type(case) is group for case in shard)
            }
            self.assertEqual(len(owners), 1)

    def test_worker_count_must_be_positive(self):
        with self.assertRaises(ValueError):
            shard_test_cases([], 0)


if __name__ == "__main__":
    unittest.main()
