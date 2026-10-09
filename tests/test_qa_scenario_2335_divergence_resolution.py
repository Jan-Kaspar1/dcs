"""The 2335_divergence_resolution leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_divergence_resolution, in
the tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The
shared fakes and helpers live in tests/qa_scenario_support.py.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam

from qa_lane import report, scenarios, verify


TOKENS = {'active': 424243, 'standby': 424244}


class ResolutionHarness(unittest.TestCase):
    """The leg's rig: the planted plant double serving the shared claim
    and the per-point read fault the blocked-clear leg injects, and the
    monitor pair whose served verdicts read that double's own state."""

    def setUp(self):
        self.plant = ClaimPlantPeer()
        self.addCleanup(self.plant.close)
        self.plant.claim = {'owner': TOKENS['active'],
                            'holders': {'controller'}}
        self.pair = DivergencePair(self.plant, TOKENS)
        self.evidence = tempfile.mkdtemp(prefix='dcs-resolution-')
        self.addCleanup(self._remove)
        self._patch()

    def _remove(self):
        import shutil
        shutil.rmtree(self.evidence, ignore_errors=True)

    def _patch(self):
        patcher = patch.object(scenarios, 'http_json', self.pair.http_json)
        patcher.start()
        self.addCleanup(patcher.stop)
        defaults = {'RESOLUTION_SETTLE': 2, 'RESOLUTION_DEADLINE': 2,
                    'RESOLUTION_POLL': 0.001, 'RESOLUTION_BLOCKED': 0.2,
                    'RESOLUTION_BLOCKED_ROUNDS': 3,
                    'RESOLUTION_RECOVER': 2, 'RESOLUTION_PROMOTE': 2,
                    'RESOLUTION_RESTORE': 2}
        for key, value in defaults.items():
            seamer = patch.object(scenarios, key, value)
            seamer.start()
            self.addCleanup(seamer.stop)

    def _reset(self):
        self.plant.samples[DivergencePair.POINT] = {
            'value': dict(self.pair.STAGED), 'quality': 'good', 'tick': 0}
        self.plant.faults = {}
        self.plant.claim = {'owner': TOKENS['active'],
                            'holders': {'controller'}}
        self.pair = DivergencePair(self.plant, TOKENS)
        patcher = patch.object(scenarios, 'http_json', self.pair.http_json)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _path(self, ref):
        return Path(self.evidence) / ref.split('/', 1)[-1]

    def _ctx(self):
        return {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'plant': self.plant.address, 'plant_ctl': self.plant.ctl,
                'plant_owner': TOKENS, 'evidence_dir': self.evidence}

    def _run(self):
        return scenarios.scenario_divergence_resolution(self._ctx())


class DivergenceResolutionTests(ResolutionHarness):

    def test_registered(self):
        self.assertIn(scenarios.scenario_divergence_resolution,
                      scenarios.SCENARIOS)
        order = list(scenarios.SCENARIOS)
        self.assertLess(order.index(scenarios.scenario_standby_divergence),
                        order.index(
                            scenarios.scenario_divergence_resolution))
        self.assertLess(order.index(
                            scenarios.scenario_divergence_resolution),
                        order.index(scenarios.scenario_claim_reclaim))
        self.assertIs(verify.case_function('divergence-resolution'),
                      scenarios.scenario_divergence_resolution)

    def test_clean_lifecycle_passes_and_validates(self):
        record = self._run()
        report.validate_scenario(record)
        self.assertEqual(record['outcome'], 'passed',
                         json.dumps(record.get('detail'))[:400])
        # The read fault really landed on the compared surface and was
        # cleared again: the blocked-clear leg is exercised, not
        # short-circuited.
        self.assertEqual(self.plant.faults, {})
        self.assertEqual(self.pair.promotes, 3)
        self.assertEqual(self.pair.demotes, 1)
        self.assertEqual(self.pair.owner, 'active')
        detections = self.pair._journal_kind('standby',
                                             'divergence_detected')
        resolutions = self.pair._journal_kind('standby',
                                              'divergence_resolved')
        self.assertEqual(len(detections), 1)
        self.assertEqual(len(resolutions), 1)

    def test_identical_evidence_across_runs(self):
        first = self._run()
        payloads = {entry['ref']: self._path(entry['ref']).read_text()
                    for entry in first['evidence']}
        self._reset()
        second = self._run()
        again = {entry['ref']: self._path(entry['ref']).read_text()
                 for entry in second['evidence']}
        self.assertEqual(first['outcome'], 'passed')
        self.assertEqual(second['outcome'], 'passed')
        self.assertEqual(payloads, again)

    def test_evidence_files_land(self):
        record = self._run()
        self.assertEqual(record['outcome'], 'passed')
        names = [entry['ref'] for entry in record['evidence']]
        for expected in ('divergence-resolution-image.json',
                         'divergence-resolution-detection.json',
                         'divergence-resolution-gate.json',
                         'divergence-resolution-blocked.json',
                         'divergence-resolution-resolution.json',
                         'divergence-resolution-promoted.json',
                         'divergence-resolution-restore.json'):
            self.assertIn('evidence/' + expected, names)
        for entry in record['evidence']:
            self.assertEqual(entry['kind'], 'file')
            self.assertTrue(self._path(entry['ref']).is_file())
        blocked = json.loads(self._path(
            'evidence/divergence-resolution-blocked.json').read_text())
        self.assertEqual(blocked['points'], [DivergencePair.POINT])
        self.assertEqual(blocked['fault'], 'disconnected')
        self.assertNotIn(None, blocked['rounds'])

    def test_never_detects_reports_failed(self):
        self.pair.no_detection = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('divergence-resolution-failed', record['detail'])
        self.assertIn('never convicted the standby', record['detail'])

    def test_admitted_first_gate_reports_failed(self):
        self.pair.promote_admits = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('not_converged', record['detail'])

    def test_cleared_blocked_window_reports_failed(self):
        # The #541 regression: a comparison whose field reads cannot
        # complete must not clear the standing verdict.
        self.pair.clears_on_fault = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('incomplete comparison is evidence of neither',
                      record['detail'])

    def test_regression_clear_reports_failed(self):
        # The same regression reported through its audit trail: a
        # resolution journaling across the blocked window.
        self.pair.clears_on_fault = True
        self.pair.no_resolution = False
        original = self.pair.report

        def clearing(name):
            served = original(name)
            if name == 'standby' and self.plant.faults and \
                    isinstance(served.get('sync'), dict) \
                    and 'diverged' in served['sync']:
                self.pair.clears_on_fault = True
            return served

        self.pair.report = clearing
        patcher = patch.object(scenarios, 'http_json', self.pair.http_json)
        patcher.start()
        self.addCleanup(patcher.stop)
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')

    def test_silent_resolution_reports_failed(self):
        self.pair.no_resolution = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('divergence_resolved', record['detail'])

    def test_unresolved_verdict_reports_failed(self):
        # The fault never clears, so the first fully-read comparison
        # never arrives and the verdict stands open-ended.
        original = self.pair.ctl if hasattr(self.pair, 'ctl') else None
        ctl = self.plant.ctl

        def stubborn(*args):
            if args and args[0] == 'clear-fault':
                return _ctl_process(
                    stderr='the fault clear was refused', returncode=1)
            return ctl(*args)

        self.plant.ctl = stubborn
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('fault clear on point', record['detail'])
        del original

    def test_unread_resolution_evidence_reports_failed(self):
        # A journal that carries no resolution record at all is the
        # audit gap the lifecycle's third leg closes.
        self.pair.hide_resolutions = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('divergence_resolved', record['detail'])

    def test_disturbed_claim_reports_failed(self):
        # The shared-claim write must never disturb the active's
        # ownership: a peer reporting no claim at the end is the
        # lifecycle's own audit.
        original = self.pair.report

        def unclaimed(name):
            served = original(name)
            if name == 'active':
                served['field_claim'] = 'unclaimed'
            return served

        self.pair.report = unclaimed
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn("field ownership", record['detail'])

    def test_starved_watch_reports_inconclusive(self):
        # A monitor that answers nothing across the faulted window
        # never staged the blocked applies the leg judges.
        original = self.pair.http_json

        def starved(method, url, body=None, timeout=10):
            if self.plant.faults and 'ctrl-b' in url:
                raise ConnectionError('the survivor stopped answering')
            return original(method, url, body, timeout)

        patcher = patch.object(scenarios, 'http_json', starved)
        patcher.start()
        self.addCleanup(patcher.stop)
        record = self._run()
        self.assertEqual(record['outcome'], 'inconclusive')
        self.assertIn('never observed its applies', record['detail'])

    def test_refused_claim_reports_inconclusive(self):
        self.plant.refuse_ensure = True
        record = self._run()
        self.assertEqual(record['outcome'], 'inconclusive')
        self.assertIn('writer claim refused', record['detail'])


if __name__ == '__main__':
    unittest.main()