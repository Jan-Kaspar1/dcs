import concurrent.futures
import tempfile
import unittest
from pathlib import Path
from agent_pool.state import State


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'state.db'
        self.state = State(self.path)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def test_atomic_duplicate_claim(self):
        def claim(n):
            state = State(self.path)
            try:
                return state.reserve(1, f'worker-{n}', 'group') is not None
            finally:
                state.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(sum(pool.map(claim, range(8))), 1)

    def test_dependencies_and_groups(self):
        self.assertIsNone(self.state.reserve(2, 'two', 'b', [1]))
        self.assertIsNotNone(self.state.reserve(1, 'one', 'a'))
        self.assertIsNone(self.state.reserve(3, 'three', 'a'))
        self.state.complete(1)
        self.assertIsNotNone(self.state.reserve(2, 'two', 'a', [1]))

    def test_repairs_pause_and_retry_conflict(self):
        self.state.reserve(1,'one','a')
        for _ in range(3):
            self.assertTrue(self.state.repair(1))
        self.assertFalse(self.state.repair(1))
        self.state.reserve(2,'one','a')
        self.assertFalse(self.state.retry(1))
        self.state.pause()
        self.assertIsNone(self.state.reserve(3,'three','c'))
        self.state.resume()
        self.assertIsNotNone(self.state.reserve(3,'three','c'))
        self.state.integrity('conflicting PRs')
        with self.assertRaises(RuntimeError):
            self.state.resume()

    def test_ramp_and_restart(self):
        for number in range(1,16):
            self.assertIsNotNone(self.state.reserve(number,'one','a'))
            self.state.complete(number)
            self.state.complete(number)
            self.assertEqual(self.state.capacity(),20 if number>=15 else 10 if number>=5 else 5)
        other = State(self.path)
        self.assertEqual(other.get('merges'),15)
        self.assertEqual(other.job(15)['status'],'done')
        other.close()
