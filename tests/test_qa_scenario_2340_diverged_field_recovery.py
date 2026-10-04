"""The 2340_diverged_field_recovery leg's scenario unit coverage — the
feed fakes and TestCase classes for
scenario_diverged_field_recovery, in the
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
    'DivergedFieldRecoveryTests.test_registered',
    'DivergedFieldRecoveryTests.test_clean_recovery_passes_and_validates',
    'DivergedFieldRecoveryTests.test_identical_evidence_across_runs',
    'DivergedFieldRecoveryTests.test_evidence_files_land',
    'DivergedFieldRecoveryTests.test_never_demotes_reports_failed',
    'DivergedFieldRecoveryTests.test_silent_loss_reports_failed',
    'DivergedFieldRecoveryTests.test_misattributed_loss_reports_failed',
    'DivergedFieldRecoveryTests.test_unconverged_survivor_reports_failed',
    'DivergedFieldRecoveryTests.test_admitted_promote_reports_failed',
    'DivergedFieldRecoveryTests.test_claimed_field_reports_failed',
    'DivergedFieldRecoveryTests.test_lost_uncommanded_value_reports_failed',
    'DivergedFieldRecoveryTests.test_never_healed_reports_failed',
    'DivergedFieldRecoveryTests.test_unreconverged_peer_reports_failed',
    'DivergedFieldRecoveryTests.test_refused_claim_reports_inconclusive',
    'DivergedFieldRecoveryTests.test_missing_lifecycle_seam_reports_inconclusive',
})


TOKENS = {'active': 424243, 'standby': 424244}


class WedgeHarness(unittest.TestCase):
    """The leg's rig: the planted plant double serving the claim
    arbitration the interposer drives, and the monitor pair whose
    served verdicts, refusals, and lifecycle actions read that
    double's own state."""

    def setUp(self):
        self.plant = ClaimPlantPeer()
        self.addCleanup(self.plant.close)
        self.plant.claim = {'owner': TOKENS['active'],
                            'holders': {'controller'}}
        self.pair = DivergencePair(self.plant, TOKENS)
        self.evidence = tempfile.mkdtemp(prefix='dcs-wedge-')
        self.addCleanup(self._remove)
        self._patch()

    def _remove(self):
        import shutil
        shutil.rmtree(self.evidence, ignore_errors=True)

    def _patch(self):
        patcher = patch.object(scenarios, 'http_json', self.pair.http_json)
        patcher.start()
        self.addCleanup(patcher.stop)
        defaults = {'WEDGE_SETTLE': 2, 'WEDGE_DEADLINE': 2,
                    'WEDGE_POLL': 0.001, 'WEDGE_RECOVER': 2,
                    'WEDGE_HEAL': 2, 'WEDGE_RESTORE': 2}
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
                'plant_owner': TOKENS, 'evidence_dir': self.evidence,
                'stop_controller': self.pair.stop,
                'start_controller': self.pair.start}

    def _fence(self):
        """The interposer's claim lands: the field owner meets the fence
        on its next write and demotes in place, the journal naming the
        claimant the field's own arbitration reported."""
        self.plant.rogue_token = scenarios.WEDGE_FOREIGN
        self.pair.fence_owner('active')

    def _run(self):
        return scenarios.scenario_diverged_field_recovery(self._ctx())


class DivergedFieldRecoveryTests(WedgeHarness):

    def test_registered(self):
        self.assertIn(scenarios.scenario_diverged_field_recovery,
                      scenarios.SCENARIOS)
        order = list(scenarios.SCENARIOS)
        self.assertLess(order.index(scenarios.scenario_divergence_resolution),
                        order.index(
                            scenarios.scenario_diverged_field_recovery))
        self.assertLess(order.index(
                            scenarios.scenario_diverged_field_recovery),
                        order.index(scenarios.scenario_claim_reclaim))
        self.assertIs(verify.case_function('diverged-field-recovery'),
                      scenarios.scenario_diverged_field_recovery)

    def test_clean_recovery_passes_and_validates(self):
        # The interposer's preempt never actually lands a fenced write
        # in this double, so the leg's own induction records the fence
        # the way the plant reports it.
        record = self._run()
        report.validate_scenario(record)
        self.assertEqual(record['outcome'], 'passed',
                         json.dumps(record.get('detail'))[:400])
        self.assertEqual(self.pair.start_calls, ['active'])
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
        for expected in ('wedge-recovery-image.json',
                         'wedge-recovery-fenced.json',
                         'wedge-recovery-unclaimed.json',
                         'wedge-recovery-relaunch.json',
                         'wedge-recovery-reconverged.json',
                         'wedge-recovery-restore.json'):
            self.assertIn('evidence/' + expected, names)
        for entry in record['evidence']:
            self.assertEqual(entry['kind'], 'file')
            self.assertTrue(self._path(entry['ref']).is_file())
        unclaimed = json.loads(self._path(
            'evidence/wedge-recovery-unclaimed.json').read_text())
        self.assertIn('field_unclaimed', unclaimed['faults'])
        self.assertEqual(unclaimed['probe']['error']['kind'], 'unclaimed')

    def test_never_demotes_reports_failed(self):
        # A preempted field owner that keeps its role never observed the
        # fence the wedge stages on.
        self.pair.no_fenced = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('never settled standby', record['detail'])

    def test_silent_loss_reports_failed(self):
        # A demotion that never journaled its fencing loss is the
        # wedge's fence evidence going missing.
        self.pair.silent_loss = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('field_claim_lost', record['detail'])

    def test_misattributed_loss_reports_failed(self):
        # The loss must name the claimant the field's own arbitration
        # reported: an unattributed or wrong-token record leaves the
        # audit unable to follow the preemption.
        self.pair.misattribute_loss = 424999
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('must attribute the preemption', record['detail'])

    def test_unconverged_survivor_reports_failed(self):
        # A survivor that keeps reporting healthy tracking never
        # observed the field the interposer skewed.
        self.pair.always_tracking = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('never served a named un-converged verdict',
                      record['detail'])

    def test_admitted_promote_reports_failed(self):
        # The wedge's gate must refuse the diverged survivor: an
        # admitted promote is the defect #730 recorded.
        self.pair.diverge_survivor = True
        self.pair.promote_admits = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('not_converged', record['detail'])

    def test_claimed_field_reports_failed(self):
        # A field still carrying a claim after the interposer's release
        # is not the unclaimed surface decision 90 records.
        self.pair.no_unclaimed = True
        original = self.pair.report

        def claiming(name):
            served = original(name)
            served['field_claim'] = 'held'
            return served

        self.pair.report = claiming
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('field_unclaimed', record['detail'])

    def test_lost_uncommanded_value_reports_failed(self):
        # The hazard is the standing actuation: a field that healed
        # behind the leg's back never held the un-commanded value the
        # wedge leaves.
        original = self.pair.report

        def healing(name):
            served = original(name)
            if name != self.pair.owner:
                self.plant.samples[DivergencePair.POINT]['value'] = \
                    dict(self.pair.STAGED)
            return served

        self.pair.report = healing
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('un-commanded actuation', record['detail'])

    def test_never_healed_reports_failed(self):
        # The relaunched owner never takes the free field, so the
        # recorded remedy did not run.
        self.pair.never_heals = True
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('never settled active holding the claim',
                      record['detail'])

    def test_unreconverged_peer_reports_failed(self):
        # The survivor never clears its verdict on the relaunched
        # owner's stream, so the remedy healed the field but not the
        # pair.
        original = self.pair.report

        def pinned(name):
            # After the remedy the field still stands off the owner's
            # image and the survivor's verdict never clears: the pair
            # is not healed in place.
            if name == 'standby' and self.pair.started:
                self.pair.was_diverged = True
                self.pair.diverge_survivor = True
                self.plant.samples[DivergencePair.POINT]['value'] = \
                    {'bool': not DivergencePair.STAGED['bool']}
            return original(name)

        self.pair.report = pinned
        record = self._run()
        self.assertEqual(record['outcome'], 'failed')
        self.assertIn('never reconverged', record['detail'])

    def test_refused_claim_reports_inconclusive(self):
        self.plant.rogue_token = scenarios.WEDGE_FOREIGN
        self.plant.refuse_rogue = True
        record = self._run()
        self.assertEqual(record['outcome'], 'inconclusive')
        self.assertIn('claim_writer answered', record['detail'])

    def test_missing_lifecycle_seam_reports_inconclusive(self):
        ctx = self._ctx()
        ctx.pop('stop_controller')
        record = scenarios.scenario_diverged_field_recovery(ctx)
        self.assertEqual(record['outcome'], 'inconclusive')
        self.assertIn('no stop_controller/start_controller seam',
                      record['detail'])


if __name__ == '__main__':
    unittest.main()