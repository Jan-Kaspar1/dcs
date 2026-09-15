import tempfile
import unittest
from pathlib import Path

from qa_lane import state as qa_state


SHA_A = 'a' * 40
SHA_B = 'b' * 40
SHA_C = 'c' * 40


class StateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = qa_state.State(Path(self.tmp.name) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def enqueue(self, sha, run_id, now=1000.0, day='2026-09-15', **kw):
        return self.st.enqueue(run_id, sha, now, day, **kw)

    def test_enqueue_and_next(self):
        rec = self.enqueue(SHA_A, 'qa-1')
        self.assertEqual(rec['status'], 'queued')
        self.assertEqual(self.st.next_queued()['run_id'], 'qa-1')

    def test_enqueue_same_sha_is_idempotent(self):
        self.enqueue(SHA_A, 'qa-1')
        rec = self.enqueue(SHA_A, 'qa-2')
        self.assertEqual(rec['run_id'], 'qa-1')
        self.assertEqual(len(self.st.runs()), 1)

    def test_newer_enqueue_supersedes_queued(self):
        self.enqueue(SHA_A, 'qa-1')
        self.enqueue(SHA_B, 'qa-2')
        self.assertEqual(self.st.run('qa-1')['status'], 'superseded')
        self.assertEqual(self.st.next_queued()['run_id'], 'qa-2')
        # the intervening range is preserved on the surviving record
        self.assertEqual(self.st.run('qa-2')['range_first'], SHA_A)

    def test_range_first_falls_back_to_last_finished(self):
        self.enqueue(SHA_A, 'qa-1')
        self.st.begin('qa-1', 1234, 1100.0)
        self.st.finish('qa-1', 'passed', SHA_A, '/r/qa-1.json', 1200.0)
        self.enqueue(SHA_C, 'qa-3')
        self.assertEqual(self.st.run('qa-3')['range_first'], SHA_A)

    def test_same_sha_finished_again_no_range(self):
        self.enqueue(SHA_A, 'qa-1')
        self.st.begin('qa-1', 1, 1.0)
        self.st.finish('qa-1', 'inconclusive', None, '/r.json', 2.0)
        rec = self.enqueue(SHA_A, 'qa-2', now=3.0, attempt=2)
        self.assertIsNone(rec['range_first'])
        self.assertEqual(rec['attempt'], 2)

    def test_begin_only_moves_queued(self):
        self.enqueue(SHA_A, 'qa-1')
        self.st.begin('qa-1', 4321, 1.0)
        rec = self.st.run('qa-1')
        self.assertEqual(rec['status'], 'running')
        self.assertEqual(rec['pid'], 4321)

    def test_attempts_for_counts_launched(self):
        self.assertEqual(self.st.attempts_for(SHA_A), 0)
        self.enqueue(SHA_A, 'qa-1')
        self.st.begin('qa-1', 1, 1.0)
        self.st.interrupt('qa-1', 'killed', 2.0)
        self.assertEqual(self.st.attempts_for(SHA_A), 1)

    def test_started_today_budget(self):
        self.assertEqual(self.st.started_today('2026-09-15'), 0)
        self.enqueue(SHA_A, 'qa-1', day='2026-09-15')
        self.st.begin('qa-1', 1, 1.0)
        self.assertEqual(self.st.started_today('2026-09-15'), 1)
        # queued runs do not consume the budget
        self.enqueue(SHA_B, 'qa-2', day='2026-09-15')
        self.assertEqual(self.st.started_today('2026-09-15'), 1)

    def test_last_attempted_sha(self):
        self.assertIsNone(self.st.last_attempted_sha())
        self.enqueue(SHA_A, 'qa-1')
        self.enqueue(SHA_B, 'qa-2')
        self.assertEqual(self.st.last_attempted_sha(), SHA_B)


if __name__ == '__main__':
    unittest.main()
