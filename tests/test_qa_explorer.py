"""Charter-based exploratory lane: dispatch gating, prompt rendering,
agent-result parsing, and the run's report/ledger paths — all covered
with fakes, no Docker and no real Devin session."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_lane import explorer, report as qa_report
from qa_lane import runner, state as qa_state

# Exploration runs resolve cfg['exploration_devin'] ('/bin/true') and the
# agent spawn signals process groups; both are POSIX-only.
posix_only = unittest.skipUnless(os.name == 'posix',
                                 'exploration run fixtures are POSIX-only')

SHA_A = 'a' * 40
SHA_B = 'b' * 40
FIX = 'f' * 40
DAY = '2026-09-16'


def cfg_for(root):
    cfg = dict(runner.DEFAULT_CONFIG)
    cfg['state_dir'] = str(Path(root) / 'state')
    cfg['src_dir'] = str(Path(root) / 'state' / 'src')
    cfg['git_dir'] = str(Path(root) / 'repo.git')
    cfg['egress_required'] = False
    cfg['exploration_enabled'] = True
    cfg['exploration_devin'] = '/bin/true'
    return cfg


def agent_doc(**kw):
    doc = {
        'charter': 'command-boundary-behavior',
        'novelty_rationale': 'receipt persistence was never probed',
        'results': [
            {'key': 'receipts-lost-on-standby-window',
             'title': 'Receipts vanish during demote/promote overlap',
             'expected': 'receipts outlive the peer transition',
             'outcome': 'failed',
             'observations': ['receipts empty after transition'],
             'detail': 'reproduced twice',
             'evidence': [{'kind': 'file',
                           'ref': 'evidence/receipts.json'}],
             'module': 'dcs-monitor',
             'mode': 'simulation',
             'reproduction': 'demote active, promote standby, GET /receipts',
             'severity': 'high',
             'confidence': 'medium',
             'test_requirements': 'regression test driving the transition',
             'product_cause': True},
            {'key': 'telemetry-gap-probe',
             'title': 'telemetry coalescing under load',
             'expected': 'no product defect expected',
             'outcome': 'passed',
             'observations': ['gap bounded at 2 polls']}],
        'capability_limitations': [
            {'key': 'no-browser', 'detail': 'no browser in session',
             'blocking': False}],
        'infrastructure_failures': [
            {'key': 'slow-poll', 'detail': 'monitor slow once'}],
        'verifications': [
            {'finding_key': 'evidence-capture', 'case': 'evidence-capture',
             'fix_sha': FIX, 'outcome': 'passed',
             'evidence': [{'detail': 'receipts present post-fix'}]}],
        'ledger': {'charter': 'command-boundary-behavior',
                   'explored': ['command path', 'receipts'],
                   'next': ['stale-data-presentation'],
                   'note': 'one defect, one clean probe'},
        'timeline': [{'t': '2026-09-16T10:00:00Z', 'event': 'probe-1',
                      'detail': 'demote/promote'}],
    }
    doc.update(kw)
    return doc


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = cfg_for(self.tmp.name)
        Path(self.cfg['state_dir']).mkdir(parents=True)
        self.st = qa_state.State(Path(self.cfg['state_dir']) / 'state.db')

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def verdicted(self, sha=SHA_A):
        self.st.enqueue('qa-20990101-001', sha, 1.0, DAY)
        self.st.begin('qa-20990101-001', 1, 2.0)
        self.st.finish('qa-20990101-001', 'passed', sha, '/r', 3.0)


class DispatchTests(Fixture):
    def test_disabled_means_no_run(self):
        self.verdicted()
        self.cfg['exploration_enabled'] = False
        self.assertIsNone(explorer.next_run(self.st, self.cfg, 10.0))

    def test_no_verdict_no_run(self):
        self.assertIsNone(explorer.next_run(self.st, self.cfg, 10.0))

    def test_missing_devin_means_no_run(self):
        self.verdicted()
        self.cfg['exploration_devin'] = '/nonexistent/devin'
        with patch.object(explorer, '_commit_known', return_value=True):
            self.assertIsNone(explorer.next_run(self.st, self.cfg, 10.0))

    def test_target_not_in_mirror_waits(self):
        # cfg['git_dir'] points at nothing — the verdicted revision can
        # not be proven present, so dispatch waits.
        self.verdicted()
        self.assertIsNone(explorer.next_run(self.st, self.cfg, 10.0))

    def test_daily_cap(self):
        self.verdicted()
        self.cfg['max_explorations_per_day'] = 0
        self.assertIsNone(explorer.next_run(self.st, self.cfg, 10.0))

    def test_interval_spacing(self):
        self.verdicted()
        self.cfg['exploration_interval_seconds'] = 7200
        self.st.queue_dedicated('qax-20990101-001', SHA_A, 5.0, DAY)
        self.st.begin('qax-20990101-001', 1, 5.0)
        self.st.finish('qax-20990101-001', 'passed', SHA_A, '/r', 6.0)
        with patch.object(explorer, '_commit_known', return_value=True):
            self.assertIsNone(explorer.next_run(self.st, self.cfg, 100.0))
            record = explorer.next_run(self.st, self.cfg, 5.0 + 7201)
        self.assertIsNotNone(record)
        self.assertTrue(record['run_id'].startswith('qax-'))
        self.assertEqual(record['attempted_sha'], SHA_A)

    def test_happy_path_targets_newest_verdicted(self):
        self.verdicted()
        self.st.enqueue('qa-20990101-002', SHA_B, 4.0, DAY)
        self.st.begin('qa-20990101-002', 1, 5.0)
        self.st.finish('qa-20990101-002', 'failed', SHA_B, '/r', 6.0)
        with patch.object(explorer, '_commit_known', return_value=True):
            record = explorer.next_run(self.st, self.cfg, 10.0)
        self.assertEqual(record['attempted_sha'], SHA_B)
        self.assertEqual(record['status'], 'queued')

    def test_exploration_does_not_regress_dispatch_pointer(self):
        self.verdicted()
        self.st.enqueue('qa-20990101-002', SHA_B, 4.0, DAY)
        self.st.begin('qa-20990101-002', 1, 5.0)
        self.st.finish('qa-20990101-002', 'passed', SHA_B, '/r', 6.0)
        # An exploration of the older verdicted SHA must not make the
        # relay believe SHA_B was never attempted.
        self.st.queue_dedicated('qax-20990101-001', SHA_A, 7.0, DAY)
        self.st.begin('qax-20990101-001', 1, 8.0)
        self.st.finish('qax-20990101-001', 'passed', SHA_A, '/r', 9.0)
        self.assertEqual(self.st.last_attempted_sha(), SHA_B)
        # and an inconclusive exploration never triggers a sha retry
        self.st.queue_dedicated('qax-20990101-002', SHA_B, 10.0, DAY)
        self.st.begin('qax-20990101-002', 1, 11.0)
        self.st.finish('qax-20990101-002', 'inconclusive', None, '/r', 12.0)
        runner._maybe_retry(self.st, self.cfg, 13.0, DAY)
        self.assertIsNone(self.st.next_queued('qa'))


class RenderTests(Fixture):
    def test_all_placeholders_resolve(self):
        record = {'run_id': 'qax-20990101-001', 'attempted_sha': SHA_A}
        run_dir = Path(self.cfg['state_dir']) / 'runs' / record['run_id']
        workspace = run_dir / 'workspace'
        (workspace / 'src').mkdir(parents=True)
        template = explorer.TEMPLATE.read_text()
        context = explorer.build_context(self.st, record, self.cfg,
                                         run_dir, workspace)
        prompt = explorer.render_prompt(template, context)
        self.assertNotIn('{{', prompt)
        self.assertIn('qax-20990101-001', prompt)
        self.assertIn(SHA_A, prompt)
        self.assertIn('simulation', prompt)


class ParseTests(Fixture):
    def record(self, run_id='qax-20990101-001', sha=SHA_A):
        return {'run_id': run_id, 'attempted_sha': sha}

    def write(self, run_id, doc):
        run_dir = Path(self.cfg['state_dir']) / 'runs' / run_id
        (run_dir / 'results').mkdir(parents=True)
        (run_dir / 'results' / 'agent-result.json').write_text(
            json.dumps(doc))
        return run_dir

    def test_missing_document_raises(self):
        with self.assertRaises(ValueError):
            explorer.parse_result(Path(self.cfg['state_dir']) / 'runs'
                                  / 'nowhere', self.record(), self.cfg)

    def test_full_document_maps(self):
        run_dir = self.write('qax-20990101-001', agent_doc())
        with patch.object(explorer.verify, 'fix_ancestry',
                          return_value={'checked': True, 'contained': True,
                                        'method': 'test'}):
            (scenarios, caps, infra, vers, exploration, ledger_entry,
             warnings) = explorer.parse_result(
                run_dir, self.record(), self.cfg)
        self.assertEqual(warnings, [])
        defect = scenarios[0]
        self.assertEqual(defect['module'], 'dcs-monitor')
        self.assertEqual(defect['severity'], 'high')
        self.assertEqual(defect['confidence'], 'medium')
        self.assertEqual(defect['product_cause'], True)
        self.assertEqual(defect['evidence'][0]['ref'],
                         'results/evidence/receipts.json')
        self.assertEqual(caps[0]['key'], 'no-browser')
        self.assertEqual(infra[0]['phase'], 'exploration')
        self.assertEqual(vers[0]['tested_sha'], SHA_A)
        self.assertTrue(vers[0]['fix_ancestry']['contained'])
        self.assertEqual(exploration['charter'],
                         'command-boundary-behavior')
        self.assertEqual(exploration['next_frontiers'],
                         ['stale-data-presentation'])
        self.assertEqual(ledger_entry['charter'],
                         'command-boundary-behavior')

    def test_malformed_entries_drop_with_warnings(self):
        doc = agent_doc(results=['x', {'key': 'Bad Key'},
                                 {'key': 'ok-probe', 'outcome': 'bogus'},
                                 {'key': 'ok-probe',
                                  'outcome': 'passed',
                                  'expected': 'holds'}],
                        verifications=[{'finding_key': 'x'}])
        run_dir = self.write('qax-20990101-001', doc)
        (scenarios, caps, infra, vers, exploration, ledger_entry,
         warnings) = explorer.parse_result(run_dir, self.record(),
                                           self.cfg)
        self.assertEqual([s['key'] for s in scenarios], ['ok-probe'])
        self.assertEqual(vers, [])
        self.assertTrue(warnings)

    def test_parsed_report_validates_as_schema_v3(self):
        run_dir = self.write('qax-20990101-001', agent_doc())
        with patch.object(explorer.verify, 'fix_ancestry',
                          return_value={'checked': True,
                                        'contained': True,
                                        'method': 'test'}):
            (scenarios, caps, infra, vers, exploration, ledger_entry,
             warnings) = explorer.parse_result(
                run_dir, self.record(), self.cfg)
        doc = {'schema_version': 3, 'run_id': 'qax-20990101-001',
               'attempted_sha': SHA_A, 'completed_sha': SHA_A,
               'image': None,
               'started_at': '2026-09-16T10:00:00+00:00',
               'finished_at': '2026-09-16T11:00:00+00:00',
               'outcome': 'failed', 'mode': 'simulation',
               'scenarios': scenarios,
               'capability_limitations': caps,
               'infrastructure_failures': infra,
               'verifications': vers,
               'exploration': exploration,
               'timeline': [{'t': '2026-09-16T10:00:00+00:00',
                             'event': 'run-start'}]}
        qa_report.validate_report(json.dumps(doc))

    def test_v3_fields_rejected_under_schema_v2(self):
        doc = {'schema_version': 2, 'run_id': 'qa-20990101-001',
               'attempted_sha': SHA_A, 'completed_sha': SHA_A,
               'image': None,
               'started_at': '2026-09-16T10:00:00+00:00',
               'finished_at': '2026-09-16T11:00:00+00:00',
               'outcome': 'failed',
               'scenarios': [{'key': 'k', 'title': 't', 'expected': 'e',
                              'outcome': 'failed', 'observations': [],
                              'module': 'dcs-monitor'}],
               'capability_limitations': [], 'infrastructure_failures': [],
               'timeline': [{'t': '2026-09-16T10:00:00+00:00',
                             'event': 'run-start'}]}
        with self.assertRaises(ValueError):
            qa_report.validate_report(json.dumps(doc))


class RunTests(Fixture):
    @posix_only
    def test_run_produces_failed_report_and_ledger(self):
        self.verdicted()
        src = Path(self.cfg['src_dir']) / SHA_A
        (src / 'marker').mkdir(parents=True)
        self.st.queue_dedicated('qax-20990101-001', SHA_A, 5.0, DAY)
        record = self.st.run('qax-20990101-001')

        def fake_spawn(command, cwd, log_path, env, timeout):
            results = Path(self.cfg['state_dir']) / 'runs' \
                / 'qax-20990101-001' / 'results'
            (results / 'agent-result.json').write_text(
                json.dumps(agent_doc()))
            Path(log_path).write_bytes(b'agent output')
            return 'completed', 0, 42.0

        with patch.object(runner, '_preflight_blocked',
                          return_value=False), \
                patch.object(runner, '_build_images',
                             return_value={'controller':
                                           'sha256:' + 'c' * 64,
                                           'plant': 'sha256:' + 'd' * 64}), \
                patch.object(runner, '_start_rig'), \
                patch.object(runner, '_wait_monitor', return_value=True), \
                patch.object(runner, '_teardown_rig', return_value=[]), \
                patch.object(explorer.verify, 'fix_ancestry',
                             return_value={'checked': True,
                                           'contained': True,
                                           'method': 'test'}):
            explorer.run(self.st, record, self.cfg, log=lambda m: None,
                         spawn=fake_spawn)
        rec = self.st.run('qax-20990101-001')
        self.assertEqual(rec['status'], 'finished')
        self.assertEqual(rec['outcome'], 'failed')
        report_doc = json.loads(Path(rec['report']).read_text())
        self.assertEqual(report_doc['mode'], 'simulation')
        self.assertEqual(report_doc['exploration']['charter'],
                         'command-boundary-behavior')
        defect = [s for s in report_doc['scenarios']
                  if s['outcome'] == 'failed'][0]
        self.assertEqual(defect['module'], 'dcs-monitor')
        self.assertEqual(defect['reproduction'],
                         'demote active, promote standby, GET /receipts')
        self.assertEqual(report_doc['verifications'][0]['outcome'],
                         'passed')
        self.assertEqual(len(explorer.ledger(self.st)), 1)

    def test_run_blocks_when_mirror_lacks_source(self):
        # The staged source was reaped and cfg['git_dir'] is not a real
        # repo — the run must fail closed with a named failure, not error.
        self.verdicted()
        self.st.queue_dedicated('qax-20990101-001', SHA_A, 5.0, DAY)
        record = self.st.run('qax-20990101-001')
        with patch.object(runner, '_preflight_blocked',
                          return_value=False), \
                patch.object(runner, '_teardown_rig', return_value=[]):
            explorer.run(self.st, record, self.cfg, log=lambda m: None)
        rec = self.st.run('qax-20990101-001')
        self.assertEqual(rec['outcome'], 'blocked')
        report_doc = json.loads(Path(rec['report']).read_text())
        keys = [f['key'] for f in
                report_doc['infrastructure_failures']]
        self.assertIn('preflight-source', keys)

    @posix_only
    def test_run_without_result_document_is_inconclusive(self):
        self.verdicted()
        src = Path(self.cfg['src_dir']) / SHA_A
        src.mkdir(parents=True)
        self.st.queue_dedicated('qax-20990101-001', SHA_A, 5.0, DAY)
        record = self.st.run('qax-20990101-001')

        def silent_spawn(command, cwd, log_path, env, timeout):
            Path(log_path).write_bytes(b'')
            return 'completed', 0, 3.0

        with patch.object(runner, '_preflight_blocked',
                          return_value=False), \
                patch.object(runner, '_build_images',
                             return_value={'controller':
                                           'sha256:' + 'c' * 64,
                                           'plant': 'sha256:' + 'd' * 64}), \
                patch.object(runner, '_start_rig'), \
                patch.object(runner, '_wait_monitor', return_value=True), \
                patch.object(runner, '_teardown_rig', return_value=[]):
            explorer.run(self.st, record, self.cfg, log=lambda m: None,
                         spawn=silent_spawn)
        rec = self.st.run('qax-20990101-001')
        self.assertEqual(rec['outcome'], 'inconclusive')
        report_doc = json.loads(Path(rec['report']).read_text())
        keys = [f['key'] for f in
                report_doc['infrastructure_failures']]
        self.assertIn('agent-result-invalid', keys)


if __name__ == '__main__':
    unittest.main()
