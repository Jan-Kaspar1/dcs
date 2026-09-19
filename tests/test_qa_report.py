import json
import unittest
from pathlib import Path

from qa_lane import report as qa_report


FIXTURES = Path(__file__).resolve().parents[1] / 'qa_lane' / 'fixtures'
SHA_A = 'a' * 40
SHA_B = 'b' * 40
DIGEST = 'sha256:' + '0' * 64


def valid_report():
    return {
        'schema_version': 1,
        'run_id': 'qa-20260915-001',
        'attempted_sha': SHA_A,
        'completed_sha': SHA_A,
        'image': {'controller': DIGEST, 'plant': DIGEST},
        'started_at': '2026-09-15T10:00:00+00:00',
        'finished_at': '2026-09-15T10:10:00+00:00',
        'outcome': 'passed',
        'scenarios': [{
            'key': 'controller-active',
            'title': 'Active controller owns the field',
            'expected': 'ctrl-a reports active',
            'outcome': 'passed',
            'observations': ['role=active'],
            'evidence': [{'kind': 'file', 'ref': 'evidence/role.json'}],
        }],
        'capability_limitations': [],
        'infrastructure_failures': [],
        'timeline': [{'t': '2026-09-15T10:00:00+00:00', 'event': 'run-start'}],
    }


def check(data):
    return qa_report.validate_report(json.dumps(data))


class FixtureTests(unittest.TestCase):
    def test_checked_in_fixtures_validate(self):
        for name in ('report-passed.json', 'report-failed.json',
                     'report-interrupted.json'):
            data = qa_report.validate_report((FIXTURES / name).read_text())
            self.assertEqual(data['schema_version'], 1, name)

    def test_fixture_outcomes(self):
        self.assertEqual(qa_report.validate_report(
            (FIXTURES / 'report-passed.json').read_text())['outcome'], 'passed')
        self.assertEqual(qa_report.validate_report(
            (FIXTURES / 'report-failed.json').read_text())['outcome'], 'failed')
        self.assertEqual(qa_report.validate_report(
            (FIXTURES / 'report-interrupted.json').read_text())['outcome'],
            'interrupted')


class ValidationTests(unittest.TestCase):
    def test_valid_report(self):
        self.assertEqual(check(valid_report())['run_id'], 'qa-20260915-001')

    def test_rejects_non_json(self):
        with self.assertRaises(ValueError):
            qa_report.validate_report('not json')

    def test_rejects_unknown_top_level_field(self):
        data = valid_report()
        data['surprise'] = True
        with self.assertRaises(ValueError):
            check(data)

    def test_rejects_missing_required_field(self):
        for field in qa_report.REQUIRED_TOP:
            data = valid_report()
            del data[field]
            with self.assertRaises(ValueError, msg=field):
                check(data)

    def test_rejects_wrong_schema_version(self):
        data = valid_report()
        data['schema_version'] = 4
        with self.assertRaises(ValueError):
            check(data)

    def _verification(self, **kw):
        entry = {'finding_key': 'standby-tracking', 'case': 'standby-tracking',
                 'fix_sha': 'f' * 40, 'tested_sha': SHA_A, 'outcome': 'passed',
                 'fix_ancestry': {'checked': True, 'contained': True,
                                  'method': 'git merge-base --is-ancestor'},
                 'evidence': [{'detail': 'reproduction passed'}]}
        entry.update(kw)
        return entry

    def test_v2_verifications_channel(self):
        data = valid_report()
        data['schema_version'] = 2
        data['verifications'] = [self._verification()]
        out = check(data)
        self.assertEqual(out['verifications'][0]['finding_key'],
                         'standby-tracking')

    def test_v1_rejects_verifications(self):
        data = valid_report()
        data['verifications'] = [self._verification()]
        with self.assertRaises(ValueError):
            check(data)

    def test_verification_field_errors(self):
        mutations = {
            'finding_key': lambda e: e.update(finding_key='Bad Key'),
            'case': lambda e: e.update(case='Bad_Case'),
            'fix_sha': lambda e: e.update(fix_sha='zzz'),
            'tested_sha': lambda e: e.update(tested_sha='short'),
            'outcome': lambda e: e.update(outcome='maybe'),
            'missing_tested': lambda e: e.pop('tested_sha'),
            'ancestry': lambda e: e.update(fix_ancestry={'surprise': 1}),
            'evidence': lambda e: e.update(evidence=[{'kind': 'file'}]),
        }
        for name, mutate in mutations.items():
            data = valid_report()
            data['schema_version'] = 2
            entry = self._verification()
            mutate(entry)
            data['verifications'] = [entry]
            with self.assertRaises(ValueError, msg=name):
                check(data)

    def test_verification_verdict_requires_tested_sha_match(self):
        data = valid_report()
        data['schema_version'] = 2
        data['verifications'] = [self._verification(tested_sha=SHA_B)]
        with self.assertRaises(ValueError):
            check(data)

    def test_inconclusive_verification_on_blocked_run(self):
        data = valid_report()
        data['schema_version'] = 2
        data['outcome'] = 'blocked'
        data['completed_sha'] = None
        data['scenarios'] = []
        data['verifications'] = [self._verification(
            outcome='inconclusive',
            fix_ancestry={'checked': True, 'contained': False,
                          'method': 'git merge-base --is-ancestor',
                          'detail': 'not an ancestor'},
            detail='tested revision does not contain the fix')]
        check(data)
        # A verdict on an unassessed revision is still rejected.
        data['verifications'][0]['outcome'] = 'passed'
        with self.assertRaises(ValueError):
            check(data)

    def test_duplicate_verification_keys_rejected(self):
        data = valid_report()
        data['schema_version'] = 2
        data['verifications'] = [self._verification(), self._verification()]
        with self.assertRaises(ValueError):
            check(data)

    def test_rejects_bad_run_id(self):
        for bad in ('', 'Has_Upper', 'x' * 81, 42):
            data = valid_report()
            data['run_id'] = bad
            with self.assertRaises(ValueError, msg=str(bad)):
                check(data)

    def test_run_id_pinning(self):
        qa_report.validate_report(json.dumps(valid_report()),
                                  run_id='qa-20260915-001',
                                  attempted_sha=SHA_A)
        with self.assertRaises(ValueError):
            qa_report.validate_report(json.dumps(valid_report()),
                                      run_id='qa-other')
        with self.assertRaises(ValueError):
            qa_report.validate_report(json.dumps(valid_report()),
                                      attempted_sha=SHA_B)

    def test_rejects_bad_shas(self):
        for field in ('attempted_sha', 'completed_sha'):
            for bad in ('short', 'z' * 40, 40 * 'A', 123):
                data = valid_report()
                data[field] = bad
                with self.assertRaises(ValueError, msg=f'{field}={bad}'):
                    check(data)

    def test_blocked_run_may_omit_completed_sha(self):
        data = valid_report()
        data['outcome'] = 'blocked'
        data['completed_sha'] = None
        data['scenarios'][0]['outcome'] = 'blocked'
        check(data)

    def test_passed_requires_completed_sha_match(self):
        data = valid_report()
        data['completed_sha'] = None
        with self.assertRaises(ValueError):
            check(data)
        data['completed_sha'] = SHA_B
        with self.assertRaises(ValueError):
            check(data)

    def test_passed_requires_all_scenarios_passed(self):
        data = valid_report()
        data['scenarios'][0]['outcome'] = 'inconclusive'
        with self.assertRaises(ValueError):
            check(data)

    def test_failed_requires_a_failed_scenario(self):
        data = valid_report()
        data['outcome'] = 'failed'
        with self.assertRaises(ValueError):
            check(data)
        data['scenarios'][0]['outcome'] = 'failed'
        check(data)

    def test_rejects_finished_before_started(self):
        data = valid_report()
        data['finished_at'] = '2026-09-15T09:00:00+00:00'
        with self.assertRaises(ValueError):
            check(data)

    def test_rejects_bad_outcome(self):
        data = valid_report()
        data['outcome'] = 'green'
        with self.assertRaises(ValueError):
            check(data)

    def test_rejects_bad_image_digest(self):
        for bad in ('latest', 'sha256:xyz', '', None):
            data = valid_report()
            data['image']['controller'] = bad
            with self.assertRaises(ValueError, msg=str(bad)):
                check(data)

    def test_rejects_duplicate_scenario_keys(self):
        data = valid_report()
        data['scenarios'].append(dict(data['scenarios'][0]))
        with self.assertRaises(ValueError):
            check(data)

    def test_rejects_evidence_path_escape(self):
        for ref in ('/etc/passwd', '../state.db', 'C:/secrets.txt'):
            data = valid_report()
            data['scenarios'][0]['evidence'] = [{'kind': 'file', 'ref': ref}]
            with self.assertRaises(ValueError, msg=ref):
                check(data)

    def test_rejects_oversized_fields(self):
        data = valid_report()
        data['scenarios'][0]['observations'] = ['x' * 2001]
        with self.assertRaises(ValueError):
            check(data)
        data = valid_report()
        data['timeline'] = [
            {'t': '2026-09-15T10:00:00+00:00', 'event': 'e'}
        ] * 201
        with self.assertRaises(ValueError):
            check(data)

    def test_changed_range_must_end_at_attempted(self):
        data = valid_report()
        data['changed_range'] = {'first': SHA_B, 'last': SHA_B}
        with self.assertRaises(ValueError):
            check(data)
        data['changed_range'] = {'first': SHA_B, 'last': SHA_A}
        check(data)

    def test_infrastructure_failure_shape(self):
        data = valid_report()
        data['outcome'] = 'interrupted'
        data['completed_sha'] = None
        data['infrastructure_failures'] = [
            {'key': 'runner-killed', 'detail': 'SIGKILL', 'phase': 'scenarios'}]
        check(data)
        data['infrastructure_failures'] = [{'detail': 'no key'}]
        with self.assertRaises(ValueError):
            check(data)


if __name__ == '__main__':
    unittest.main()
