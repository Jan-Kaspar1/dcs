"""The common scenario seam's unit coverage — the
shared helper contracts every leg module binds
through qa_lane.scenarios.common.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'PointAccessorTests.test_point_sample_serves_the_present_point',
    'PointAccessorTests.test_point_sample_missing_point_answers_none',
    'PointAccessorTests.test_point_quality_serves_the_present_point',
    'PointAccessorTests.test_point_quality_missing_point_answers_none',
})


class PointAccessorTests(unittest.TestCase):
    """The snapshot point accessors' single contract: _point_sample
    serves the point's sample and answers None when the point is
    absent — the missing-point answer _point_quality shares on the
    diagnostics path instead of raising."""

    SNAPSHOT = {'tick': 7, 'points': [
        {'point': 10, 'sample': {'value': {'bool': True},
                                 'quality': 'good'}},
        {'point': 20, 'sample': {'value': {'float': 1.5},
                                 'quality': {'uncertain':
                                             'substituted'}}}]}

    def test_point_sample_serves_the_present_point(self):
        self.assertEqual(
            scenarios._point_sample(self.SNAPSHOT, 10),
            {'value': {'bool': True}, 'quality': 'good'})
        self.assertEqual(
            scenarios._point_sample(self.SNAPSHOT, 20),
            {'value': {'float': 1.5},
             'quality': {'uncertain': 'substituted'}})

    def test_point_sample_missing_point_answers_none(self):
        self.assertIsNone(scenarios._point_sample(self.SNAPSHOT, 99))
        self.assertIsNone(scenarios._point_sample({}, 10))

    def test_point_quality_serves_the_present_point(self):
        self.assertEqual(
            scenarios._point_quality(self.SNAPSHOT, 10), 'good')
        self.assertEqual(
            scenarios._point_quality(self.SNAPSHOT, 20),
            {'uncertain': 'substituted'})

    def test_point_quality_missing_point_answers_none(self):
        self.assertIsNone(scenarios._point_quality(self.SNAPSHOT, 99))
        self.assertIsNone(scenarios._point_quality({}, 10))


if __name__ == '__main__':
    unittest.main()
