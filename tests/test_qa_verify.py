"""Lenovo-side fix-verification lane: queue consumption, ancestry gate,
dispatch ordering, and the verification run's blocked/verdict paths —
all covered with fakes, no Docker."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_lane import report as qa_report
from qa_lane import runner, state as qa_state, verify

SHA_A = 'a' * 40
SHA_B = 'b' * 40
FIX = 'f' * 40
DAY = '2026-09-15'


class Result:
    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


def cfg_for(root):
    cfg = dict(runner.DEFAULT_CONFIG)
    cfg['state_dir'] = str(Path(root) / 'state')
    cfg['src_dir'] = str(Path(root) / 'state' / 'src')
    cfg['git_dir'] = str(Path(root) / 'repo.git')
    return cfg


def item(key='standby-tracking', fix=FIX, case=None, **kw):
    entry = {'finding_key': key, 'case': case or key, 'fix_sha': fix,
             'issue': 7,
             'reproduction': 'replay the original case',
             'expected': 'the defect no longer reproduces'}
    entry.update(kw)
    return entry


def write_queue(cfg, items):
    Path(cfg['state_dir']).mkdir(parents=True, exist_ok=True)
    (Path(cfg['state_dir']) / 'verifications.json').write_text(
        json.dumps({'schema': 'qa-verifications/1', 'items': items}))


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        Path(self.cfg['state_dir']).mkdir(parents=True)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def stage_src(self, sha):
        src = Path(self.cfg['src_dir']) / sha
        src.mkdir(parents=True, exist_ok=True)

    def spec(self, run_id, it, contained=True, tested=SHA_A):
        self.st.set(verify.SPEC_PREFIX + run_id, {
            'item': it,
            'ancestry': {'checked': True, 'contained': contained,
                         'method': 'git merge-base --is-ancestor'},
            'tested_sha': tested})


class QueueTests(Fixture):
    def test_missing_queue_is_no_work(self):
        self.assertEqual(verify.load_queue(self.cfg), [])

    def test_malformed_items_are_skipped(self):
        write_queue(self.cfg, [
            item(),
            {'finding_key': 'Bad Key', 'case': 'x', 'fix_sha': FIX},
            {'finding_key': 'ok-key', 'case': 'ok-key'},
            'not-a-dict',
            item('no-fix', fix='zzz'),
        ])
        loaded = verify.load_queue(self.cfg)
        self.assertEqual([i['finding_key'] for i in loaded],
                         ['standby-tracking'])

    def test_wrong_schema_is_ignored(self):
        Path(self.cfg['state_dir'], 'verifications.json').write_text(
            json.dumps({'schema': 'other/1', 'items': [item()]}))
        self.assertEqual(verify.load_queue(self.cfg), [])


class AncestryTests(Fixture):
    def test_missing_mirror_is_uncheckable(self):
        result = verify.fix_ancestry(self.cfg['git_dir'], FIX, SHA_A)
        self.assertIsNone(result['contained'])
        self.assertFalse(result['checked'])

    def test_missing_objects_are_uncheckable(self):
        Path(self.cfg['git_dir']).mkdir(parents=True)
        calls = []

        def git(cmd, **kw):
            calls.append(cmd)
            return Result(returncode=1)

        result = verify.fix_ancestry(self.cfg['git_dir'], FIX, SHA_A,
                                     runner_=git)
        self.assertIsNone(result['contained'])
        self.assertIn('missing', result['detail'])

    def test_contained_and_not_contained(self):
        Path(self.cfg['git_dir']).mkdir(parents=True)

        def git(cmd, **kw):
            if 'cat-file' in cmd:
                return Result(returncode=0)
            return Result(returncode=0)

        self.assertTrue(verify.fix_ancestry(
            self.cfg['git_dir'], FIX, SHA_A, runner_=git)['contained'])

        def git_not(cmd, **kw):
            if 'cat-file' in cmd:
                return Result(returncode=0)
            return Result(returncode=1)

        self.assertFalse(verify.fix_ancestry(
            self.cfg['git_dir'], FIX, SHA_A, runner_=git_not)['contained'])


class DispatchTests(Fixture):
    def test_no_queue_no_run(self):
        self.assertIsNone(verify.next_run(self.st, self.cfg, 1.0))

    def test_no_target_no_run(self):
        write_queue(self.cfg, [item()])
        self.assertIsNone(verify.next_run(self.st, self.cfg, 1.0))

    def test_unstaged_source_waits(self):
        write_queue(self.cfg, [item()])
        self.st.enqueue('qa-1', SHA_A, 1.0, DAY)
        logs = []
        self.assertIsNone(verify.next_run(self.st, self.cfg, 1.0,
                                          log=logs.append))
        self.assertTrue(any('not staged' in m for m in logs))

    def test_uncheckable_ancestry_spends_no_run(self):
        write_queue(self.cfg, [item()])
        self.st.enqueue('qa-1', SHA_A, 1.0, DAY)
        self.stage_src(SHA_A)
        logs = []
        self.assertIsNone(verify.next_run(self.st, self.cfg, 1.0,
                                          log=logs.append))
        self.assertEqual(self.st.runs(('queued',))[0]['run_id'], 'qa-1')
        self.assertTrue(any('unverifiable' in m for m in logs))

    def test_replay_marker_validated(self):
        write_queue(self.cfg, [item('exploratory-case', replay='agent'),
                               item('bad-replay', replay='bogus')])
        items = verify.load_queue(self.cfg)
        self.assertEqual([i['finding_key'] for i in items],
                         ['exploratory-case'])

    def test_agent_replay_items_skip_dispatch(self):
        """An exploratory case identity has no deterministic scenario —
        the qav-* lane must not spend a run on it; the exploration lane
        replays it instead."""
        write_queue(self.cfg, [item('exploratory-case', replay='agent')])
        self.st.enqueue('qa-1', SHA_A, 1.0, DAY)
        self.stage_src(SHA_A)
        Path(self.cfg['git_dir']).mkdir(parents=True)
        self.assertIsNone(verify.next_run(self.st, self.cfg, 1.0))
        self.assertEqual([r['run_id'] for r in self.st.runs(('queued',))],
                         ['qa-1'])

    def test_checkable_item_dispatches_dedicated_run(self):
        write_queue(self.cfg, [item()])
        self.st.enqueue('qa-1', SHA_A, 1.0, DAY)
        self.stage_src(SHA_A)
        Path(self.cfg['git_dir']).mkdir(parents=True)
        ancestry = {'checked': True, 'contained': True,
                    'method': 'fake'}
        with patch.object(verify, 'fix_ancestry', return_value=ancestry):
            record = verify.next_run(self.st, self.cfg, 1.0)
        self.assertTrue(record['run_id'].startswith('qav-'))
        self.assertEqual(record['status'], 'queued')
        self.assertEqual(record['attempted_sha'], SHA_A)
        spec = self.st.get(verify.SPEC_PREFIX + record['run_id'])
        self.assertEqual(spec['item']['finding_key'], 'standby-tracking')
        self.assertTrue(spec['ancestry']['contained'])
        # The assessment run was not superseded by the verification.
        self.assertEqual(self.st.run('qa-1')['status'], 'queued')

    def test_reported_verdict_dedups(self):
        write_queue(self.cfg, [item()])
        self.st.enqueue('qa-1', SHA_A, 1.0, DAY)
        self.stage_src(SHA_A)
        verify._mark_reported(self.st, 'standby-tracking', FIX, SHA_A,
                              'qav-1', 'passed')
        self.assertIsNone(verify.next_run(self.st, self.cfg, 1.0))

    def test_new_target_or_new_fix_reruns(self):
        write_queue(self.cfg, [item()])
        self.st.enqueue('qa-1', SHA_B, 1.0, DAY)
        self.stage_src(SHA_B)
        verify._mark_reported(self.st, 'standby-tracking', FIX, SHA_A,
                              'qav-1', 'passed')
        ancestry = {'checked': True, 'contained': True, 'method': 'fake'}
        with patch.object(verify, 'fix_ancestry', return_value=ancestry):
            record = verify.next_run(self.st, self.cfg, 1.0)
        self.assertIsNotNone(record)

    def test_inconclusive_retries_bounded(self):
        write_queue(self.cfg, [item()])
        self.st.enqueue('qa-1', SHA_A, 1.0, DAY)
        self.stage_src(SHA_A)
        self.cfg['max_attempts_per_sha'] = 2
        verify._mark_reported(self.st, 'standby-tracking', FIX, SHA_A,
                              'qav-1', 'inconclusive')
        ancestry = {'checked': True, 'contained': True, 'method': 'fake'}
        with patch.object(verify, 'fix_ancestry', return_value=ancestry):
            self.assertIsNotNone(verify.next_run(self.st, self.cfg, 1.0))
        verify._mark_reported(self.st, 'standby-tracking', FIX, SHA_A,
                              'qav-2', 'inconclusive')
        self.assertIsNone(verify.next_run(self.st, self.cfg, 1.0))


class RunTests(Fixture):
    def test_not_contained_blocks_before_reproduction(self):
        """A tested revision lacking the fix records the failed check —
        the reproduction never runs."""
        self.st.queue_verification('qav-1', SHA_A, 1.0, DAY)
        self.spec('qav-1', item(), contained=False)
        record = self.st.run('qav-1')
        verify.run(self.st, record, self.cfg, log=lambda m: None)
        rec = self.st.run('qav-1')
        self.assertEqual((rec['status'], rec['outcome']),
                         ('finished', 'blocked'))
        doc = qa_report.validate_report(Path(rec['report']).read_text())
        self.assertEqual(doc['schema_version'], qa_report.SCHEMA_VERSION)
        entry = doc['verifications'][0]
        self.assertEqual(entry['outcome'], 'inconclusive')
        self.assertFalse(entry['fix_ancestry']['contained'])
        self.assertIn('does not contain', entry['detail'])
        mark = verify.reported(self.st)['standby-tracking']
        self.assertEqual(mark['outcome'], 'not-contained')

    def test_contained_replays_case_and_records_verdict(self):
        self.st.queue_verification('qav-1', SHA_A, 1.0, DAY)
        self.spec('qav-1', item())
        self.stage_src(SHA_A)
        record = self.st.run('qav-1')
        case = {'key': 'standby-tracking', 'title': 'Standby converges',
                'expected': 'tracking convergence', 'outcome': 'passed',
                'observations': ['ctrl-b tracking'],
                'evidence': [{'kind': 'file',
                              'ref': 'evidence/role.json',
                              'detail': 'RoleReport'}]}
        with patch.object(runner, '_build_images', return_value=None), \
                patch.object(runner, '_start_rig'), \
                patch.object(runner, '_wait_monitor', return_value=True), \
                patch.object(verify, 'case_function',
                             return_value=lambda ctx: case), \
                patch.object(verify, '_teardown'):
            verify.run(self.st, record, self.cfg, log=lambda m: None)
        rec = self.st.run('qav-1')
        self.assertEqual((rec['status'], rec['outcome']),
                         ('finished', 'passed'))
        doc = qa_report.validate_report(Path(rec['report']).read_text())
        entry = doc['verifications'][0]
        self.assertEqual(entry['outcome'], 'passed')
        self.assertEqual(entry['tested_sha'], SHA_A)
        self.assertTrue(entry['fix_ancestry']['contained'])
        self.assertTrue(entry['evidence'])
        mark = verify.reported(self.st)['standby-tracking']
        self.assertEqual(mark['outcome'], 'passed')

    def test_failed_case_records_failed_verdict(self):
        self.st.queue_verification('qav-1', SHA_A, 1.0, DAY)
        self.spec('qav-1', item())
        self.stage_src(SHA_A)
        record = self.st.run('qav-1')
        case = {'key': 'standby-tracking', 'title': 'Standby converges',
                'expected': 'tracking convergence', 'outcome': 'failed',
                'observations': [], 'detail': 'still broken',
                'evidence': [{'kind': 'file', 'ref': 'evidence/r.json'}]}
        with patch.object(runner, '_build_images', return_value=None), \
                patch.object(runner, '_start_rig'), \
                patch.object(runner, '_wait_monitor', return_value=True), \
                patch.object(verify, 'case_function',
                             return_value=lambda ctx: case), \
                patch.object(verify, '_teardown'):
            verify.run(self.st, record, self.cfg, log=lambda m: None)
        rec = self.st.run('qav-1')
        self.assertEqual(rec['outcome'], 'failed')
        doc = qa_report.validate_report(Path(rec['report']).read_text())
        self.assertEqual(doc['verifications'][0]['outcome'], 'failed')

    def test_case_function_mapping(self):
        self.assertIs(verify.case_function('standby-tracking'),
                      __import__('qa_lane.scenarios', fromlist=['x'])
                      .scenario_standby_tracking)
        self.assertIsNone(verify.case_function('no-such-case'))


class PreserveTests(Fixture):
    """Retention pins track the live queue: pending verifications keep
    their run evidence and tested revisions; settled items release."""

    def test_empty_queue_pins_nothing(self):
        verify.sync_preserves(self.st, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.preserved(), {'runs': [], 'shas': []})

    def test_pending_item_pins_target_before_dispatch(self):
        write_queue(self.cfg, [item()])
        self.st.enqueue('qa-1', SHA_A, 1.0, DAY)
        verify.sync_preserves(self.st, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.preserved()['shas'], [SHA_A])

    def test_dispatched_run_and_tested_sha_pinned(self):
        write_queue(self.cfg, [item()])
        self.st.queue_verification('qav-1', SHA_A, 1.0, DAY)
        self.spec('qav-1', item())
        verify.sync_preserves(self.st, self.cfg, log=lambda m: None)
        preserved = self.st.preserved()
        self.assertEqual(preserved['runs'], ['qav-1'])
        self.assertEqual(preserved['shas'], [SHA_A])
        # A reported mark keeps the tested revision pinned as well.
        verify._mark_reported(self.st, 'standby-tracking', FIX, SHA_B,
                              'qav-1', 'passed')
        verify.sync_preserves(self.st, self.cfg, log=lambda m: None)
        self.assertIn(SHA_B, self.st.preserved()['shas'])

    def test_settled_item_releases_only_lane_pins(self):
        write_queue(self.cfg, [item()])
        self.st.queue_verification('qav-1', SHA_A, 1.0, DAY)
        self.spec('qav-1', item())
        verify.sync_preserves(self.st, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.preserved()['runs'], ['qav-1'])
        # A manual pin alongside the lane's is never released.
        self.st.set_preserve('sha', FIX, True)
        write_queue(self.cfg, [])
        verify.sync_preserves(self.st, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.preserved(), {'runs': [], 'shas': [FIX]})

    def test_unrelated_qav_run_not_pinned(self):
        write_queue(self.cfg, [item()])
        self.st.queue_verification('qav-9', SHA_B, 1.0, DAY)
        self.spec('qav-9', item('other-finding'))
        verify.sync_preserves(self.st, self.cfg, log=lambda m: None)
        self.assertEqual(self.st.preserved()['runs'], [])


if __name__ == '__main__':
    unittest.main()
