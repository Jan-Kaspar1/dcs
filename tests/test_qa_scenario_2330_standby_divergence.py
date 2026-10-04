"""The 2330_standby_divergence leg's scenario unit coverage — the feed
fakes and TestCase classes for scenario_standby_divergence, in the
tests/test_qa_scenario_NNNN_<slug>.py split layout (#940). The shared
fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam

from qa_lane import report, scenarios, verify


EXPECTED_CASES = frozenset({
    'StandbyDivergenceTests.test_registered',
    'StandbyDivergenceTests.test_clean_pair_passes_and_validates',
    'StandbyDivergenceTests.test_identical_evidence_across_runs',
    'StandbyDivergenceTests.test_evidence_files_land',
    'StandbyDivergenceTests.test_never_diverges_reports_failed',
    'StandbyDivergenceTests.test_unnamed_mismatch_reports_failed',
    'StandbyDivergenceTests.test_admitted_promote_reports_failed',
    'StandbyDivergenceTests.test_silent_detection_reports_failed',
    'StandbyDivergenceTests.test_lost_field_restore_reports_failed',
    'StandbyDivergenceTests.test_moved_field_under_a_refusal_reports_failed',
    'StandbyDivergenceTests.test_refused_claim_reports_inconclusive',
    'StandbyDivergenceTests.test_no_field_output_reports_inconclusive',
    'StandbyDivergenceTests.test_refused_poke_reports_nondeterministic',
    'StandbyDivergenceTests.test_unsettled_pair_reports_inconclusive',
})


TOKENS = {'active': 424243, 'standby': 424244}


class DivergenceHarness(unittest.TestCase):
    """The leg's rig: a planted plant double serving the claim
    arbitration the leg drives, and a monitor pair whose served
    reports and journals read that double's own state."""

    def setUp(self):
        self.plant = ClaimPlantPeer()
        self.addCleanup(self.plant.close)
        # The settled pair: the field owner's standing claim under its
        # pinned token with a live holder, exactly what a rig's active
        # controller leaves the field carrying.
        self.plant.claim = {'owner': TOKENS['active'],
                            'holders': {'controller'}}
        self.pair = DivergencePair(self.plant, TOKENS)
        self.evidence = tempfile.mkdtemp(prefix='dcs-divergence-')
        self.addCleanup(self._remove)
        self._patch()

    def _remove(self):
        import shutil
        shutil.rmtree(self.evidence, ignore_errors=True)

    def _patch(self):
        patcher = patch.object(scenarios, 'http_json', self.pair.http_json)
        patcher.start()
        self.addCleanup(patcher.stop)
        defaults = {'DIVERGENCE_SETTLE': 2, 'DIVERGENCE_DEADLINE': 2,
                    'DIVERGENCE_POLL': 0.001, 'DIVERGENCE_RECOVER': 2,
                    'DIVERGENCE_PROMOTE': 2, 'DIVERGENCE_RESTORE': 2}
        for key, value in defaults.items():
            seamer = patch.object(scenarios, key, value)
            seamer.start()
            self.addCleanup(seamer.stop)

    def _path(self, ref):
        """The evidence file a record's relative ref names."""
        return Path(self.evidence) / ref.split('/', 1)[-1]

    def _freeze_after_detection(self):
        """Doctor the planted field so the restore answers done and
        stores nothing — the field never takes the reported staged
        value, so the verdict must stand rather than latch false
        convergence."""
        original = self.pair.http_json

        def guarded(method, url, body=None, timeout=10):
            answer = original(method, url, body, timeout)
            served = self.pair.report('standby')
            if isinstance(served.get('sync'), dict) \
                    and 'diverged' in served['sync']:
                self.plant.freeze_writes = True
            return answer

        patcher = patch.object(scenarios, 'http_json', guarded)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _reset(self):
        """Return the planted rig to the settled launch shape — a
        second pass over the same fake must start from the state the
        first found, so the two passes' evidence can be compared."""
        self.plant.samples[DivergencePair.POINT] = {
            'value': dict(self.pair.STAGED), 'quality': 'good', 'tick': 0}
        self.plant.faults = {}
        self.plant.freeze_writes = False
        self.plant.claim = {'owner': TOKENS['active'],
                            'holders': {'controller'}}
        self.pair = DivergencePair(self.plant, TOKENS)
        patcher = patch.object(scenarios, 'http_json',
                               self.pair.http_json)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _ctx(self):
        return {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'plant': self.plant.address, 'plant_ctl': self.plant.ctl,
                'plant_owner': TOKENS, 'evidence_dir': self.evidence}

    def _run(self):
        return scenarios.scenario_standby_divergence(self._ctx())


class StandbyDivergenceTests(DivergenceHarness):

    def test_registered(self):
        self.assertIn(scenarios.scenario_standby_divergence,
                      scenarios.SCENARIOS)
        order = list(scenarios.SCENARIOS)
        self.assertLess(order.index(
            scenarios.scenario_checkpoint_negotiation),
            order.index(scenarios.scenario_standby_divergence))
        self.assertLess(order.index(scenarios.scenario_standby_divergence),
                        order.index(scenarios.scenario_claim_reclaim))
        self.assertIs(verify.case_function('standby-divergence'),
                      scenarios.scenario_standby_divergence)

    def test_clean_pair_passes_and_validates(self):
        record = self._run()
        report.validate_scenario(record)
        self.assertEqual(record['outcome'], 'passed',
                         json.dumps(record.get('detail'))[:400])
        self.assertGreaterEqual(len(record['evidence']), 7)
        # The gate refused, then the field restore reopened it, and the
        # pair came back to its launch roles.
        self.assertEqual(self.pair.promotes, 2)
        self.assertEqual(self.pair.demotes, 1)
        self.assertEqual(self.pair.owner, 'active')
        self.assertEqual(self.plant.samples[DivergencePair.POINT]['value'],
                         DivergencePair.STAGED)

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
        for expected in ('standby-divergence-image.json',
                         'standby-divergence-watch.json',
                         'standby-divergence-detection.json',
                         'standby-divergence-refusal.json',
                         'standby-divergence-resolution.json',
                         'standby-divergence-promoted.json',
                         'standby-divergence-restore.json'):
            self.assertIn('evidence/' + expected, names)
        for entry in record['evidence']:
            self.assertEqual(entry['kind'], 'file')
            self.assertTrue(self._path(entry['ref']).is_file())

    def test_never_diverges_reports_failed(self):
        self.pair.no_detection = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('standby-divergence-failed', record['detail'])
        self.assertIn('never convicted the standby', record['detail'])

    def test_unnamed_mismatch_reports_failed(self):
        # The gate's evidence must name the point with both sides'
        # values: a report naming nothing is the same silent verdict.
        original = self.pair.report

        def bare(name):
            served = original(name)
            if isinstance(served.get('sync'), dict) \
                    and 'diverged' in served['sync']:
                served['sync'] = {'diverged': {'mismatches': [
                    {'point': 999, 'staged': DivergencePair.STAGED,
                     'field': DivergencePair.STAGED}]}}
            return served

        self.pair.report = bare
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('does not name point', record['detail'])

    def test_admitted_promote_reports_failed(self):
        # A gate that admits the diverged peer is the defect #730
        # recorded: the field hand-off happens behind the closed gate.
        self.pair.promote_admits = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('not_converged', record['detail'])

    def test_silent_detection_reports_failed(self):
        self.pair.no_journal = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('divergence_detected', record['detail'])

    def test_lost_field_restore_reports_failed(self):
        # The restore answers done and the field never takes it: the
        # verdict must stand rather than latch false convergence.
        self._freeze_after_detection()
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('reconverged', record['detail'])

    def test_moved_field_under_a_refusal_reports_failed(self):
        # The gate refuses but the field still moves: a hand-off behind
        # the refusal is the contract's worst answer, and the active's
        # undisturbed ownership is what catches it.
        original = self.pair._switch

        def moves(name, verb):
            status, body = original(name, verb)
            if verb == 'promote' and status == 409:
                self.pair.owner = name
            return status, body

        self.pair._switch = moves
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('disturbed the active', record['detail'])

    def test_refused_claim_reports_inconclusive(self):
        # Another owner's live claim stands: the shared attachment
        # cannot join it, so the leg never reaches its induction.
        self.plant.refuse_ensure = True
        record = self._run()
        self.assertEqual(record['outcome'], 'inconclusive')
        self.assertIn('writer claim refused', record['detail'])

    def test_no_field_output_reports_inconclusive(self):
        # The field's only output is a float: the gate's comparison
        # covers booleans here, so the leg has nothing to perturb.
        self.plant.samples[DivergencePair.POINT] = {
            'value': {'float': 1.0}, 'quality': 'good', 'tick': 0}
        self.pair.STAGED = {'float': 1.0}
        record = self._run()
        self.assertEqual(record['outcome'], 'inconclusive')
        self.assertIn('no boolean field output', record['detail'])

    def test_refused_poke_reports_nondeterministic(self):
        self.plant.shared_write_fenced = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('standby-divergence-nondeterministic',
                      record['detail'])

    def test_unsettled_pair_reports_inconclusive(self):
        ctx = self._ctx()
        ctx.pop('plant_owner')
        record = scenarios.scenario_standby_divergence(ctx)
        self.assertEqual(record['outcome'], 'inconclusive')
        self.assertIn('no plant-writer owner token', record['detail'])


if __name__ == '__main__':
    unittest.main()