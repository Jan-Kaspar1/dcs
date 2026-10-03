import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from agent_pool import findings, planning
from agent_pool.state import State
from agent_pool.supervisor import Supervisor

SHA = 'a' * 40
FIX_SHA = 'f' * 40
FIXTURES = Path(__file__).resolve().parents[1] / 'qa_lane' / 'fixtures'


def scenario(key='scan-restamp', outcome='failed', **kw):
    record = {'key': key, 'title': kw.pop('title', 'Scenario ' + key),
              'expected': kw.pop('expected', 'the scenario expectation holds'),
              'outcome': outcome,
              'observations': kw.pop('observations', ['observed ' + key]),
              'evidence': kw.pop(
                  'evidence',
                  [{'kind': 'file', 'ref': 'evidence/' + key + '.json'}])}
    if outcome != 'passed':
        record['detail'] = kw.pop('detail', key + ' did not pass')
    record.update(kw)
    return record


def capability(key='no-ethercat', **kw):
    item = {'key': key,
            'detail': kw.pop('detail', 'Capability ' + key + ' is missing')}
    item.update(kw)
    return item


def infra_failure(key='runner-died', **kw):
    item = {'key': key,
            'detail': kw.pop('detail', 'Infrastructure problem ' + key)}
    item.update(kw)
    return item


def report(run_id='qa-20990101-001', scenarios=None, capabilities=None,
           infra=None, outcome=None, sha=SHA, completed=None):
    """A real schema-v1 run report (the qa_lane wire contract)."""
    scenarios = [scenario()] if scenarios is None else list(scenarios)
    if outcome is None:
        outcome = ('passed'
                   if scenarios
                   and all(s['outcome'] == 'passed' for s in scenarios)
                   else 'failed'
                   if any(s['outcome'] == 'failed' for s in scenarios)
                   else 'inconclusive')
    if completed is None:
        completed = outcome in ('passed', 'failed')
    return {'schema_version': 1, 'run_id': run_id, 'attempted_sha': sha,
            'completed_sha': sha if completed else None,
            'image': None,
            'started_at': '2026-09-15T10:00:00+00:00',
            'finished_at': '2026-09-15T11:00:00+00:00',
            'outcome': outcome,
            'host': {'name': 'lenovo', 'os': 'linux'},
            'scenarios': scenarios,
            'capability_limitations': list(capabilities or []),
            'infrastructure_failures': list(infra or []),
            'timeline': [{'t': '2026-09-15T10:00:00+00:00',
                          'event': 'run-start'}]}


def finding(key='scan-restamp', kind='defect', module='crates/dcs-core',
            severity='medium', confidence='high', **kw):
    """An internal finding record (post-adaptation), for direct ingestion."""
    item = dict(key=key, kind=kind, module=module, severity=severity,
                confidence=confidence, title='Scan restamps samples',
                summary='Inputs are restamped to the scan tick',
                evidence=[{'detail': 'observed stale stamp at boundary'}])
    if kind == 'defect':
        item.update(reproduction='run the pacing sim and observe the stamp',
                    expected='acquisition freshness preserved')
    item.update(kw)
    return item


def verification(key='scan-restamp', outcome='passed', fix=FIX_SHA,
                 tested=SHA, case=None, ancestry='default', evidence='default',
                 **kw):
    """A complete verification entry: matching case identity, the tested
    revision, the runner's ancestry proof, and real evidence."""
    if ancestry == 'default':
        ancestry = {'checked': True, 'contained': True,
                    'method': 'git merge-base --is-ancestor'}
    if evidence == 'default':
        evidence = [{'detail': 'reproduction replayed on ' + tested[:8],
                     'source': 'run evidence'}]
    entry = {'finding_key': key, 'outcome': outcome, 'fix_sha': fix,
             'case': case if case is not None else key,
             'tested_sha': tested, 'fix_ancestry': ancestry,
             'evidence': evidence}
    entry.update(kw)
    return entry


def internal(run_id='qa-20990101-002', items=(), verifications=(),
             status='completed', sha=SHA):
    """An internal post-adaptation report — the shape ingest_report consumes.
    Verification results have no schema-v1 wire channel, so tests deliver
    them through this seam."""
    return {'run_id': run_id, 'sha': sha, 'status': status,
            'outcome': 'passed' if status == 'completed' else 'inconclusive',
            'rig': 'lenovo', 'model': 'qa-lane',
            'started_at': '2026-09-15T10:00:00+00:00',
            'ended_at': '2026-09-15T11:00:00+00:00',
            'scenarios': [],
            'findings': [findings.validate_finding(dict(f)) for f in items],
            'verifications': [findings.validate_verification(dict(v))
                              for v in verifications]}


class FakeGitHub:
    """Issue + PR stand-in; create_issue honors the marker dedup contract."""
    def __init__(self):
        self.items = {}
        self.pull_requests = {}
        self.next_issue = 100
        self.next_pr = 900
        self.created = 0

    def create_issue(self, title, body, labels=(), key=None):
        marker = '<!-- dcs-agent-key:' + key + ' -->' if key else None
        if marker:
            for issue in self.items.values():
                if marker in issue['body']:
                    return issue['number']
            body += '\n\n' + marker
        self.next_issue += 1
        self.created += 1
        self.items[self.next_issue] = dict(
            number=self.next_issue, title=title, body=body, state='OPEN',
            labels=[{'name': label} for label in labels], url='')
        return self.next_issue

    def issue(self, number):
        return self.items[number]

    def issues(self):
        return list(self.items.values())

    def find_pr(self, branch):
        return next((p for p in self.pull_requests.values()
                     if p['branch'] == branch), None)

    def create_pr(self, branch, title, body):
        self.next_pr += 1
        issue = int(body.split('Closes #')[1].split()[0])
        self.pull_requests[self.next_pr] = dict(
            number=self.next_pr, branch=branch, issue=issue, state='open',
            merged=False, merge_commit_sha=None,
            head={'sha': 'head'}, base={'ref': 'main'})
        return self.next_pr

    def pr(self, number):
        return self.pull_requests[number]

    def includes_main(self, head, base):
        return True

    def check_states(self, sha):
        return {'test': 'success'}

    def checks_pass(self, pr, required):
        return True

    def merge(self, number, required):
        pr = self.pull_requests[number]
        pr['merged'] = True
        pr['state'] = 'closed'
        pr['merge_commit_sha'] = FIX_SHA
        self.items[pr['issue']]['state'] = 'CLOSED'
        return True


def make_cfg(root, **overrides):
    qa = {'enabled': True, 'mode': 'route',
          'report_dir': str(Path(root) / 'qa' / 'reports')}
    qa.update(overrides.pop('qa', {}))
    return findings.settings({'state_root': str(Path(root) / 'state'), 'qa': qa})


def drop(root, data, name=None):
    inbox = Path(root) / 'qa' / 'reports'
    inbox.mkdir(parents=True, exist_ok=True)
    name = name or data.get('run_id', 'report') + '.json'
    (inbox / name).write_text(json.dumps(data))


class LaneFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = State(self.root / 'state' / 'state.sqlite3')
        self.github = FakeGitHub()
        self.logs = []

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def log(self, text):
        self.logs.append(str(text))

    def poll(self, **overrides):
        cfg = make_cfg(self.root, **overrides)
        findings.poll(self.state, self.github, cfg, self.github.issues(), self.log)
        return cfg

    def ingest(self, doc, **overrides):
        cfg = make_cfg(self.root, **overrides)
        return findings.ingest_report(self.state, self.github, cfg, doc,
                                      self.github.issues(), self.log)


class ValidationTests(LaneFixture):
    def validate(self, data):
        return findings.validate_report(json.dumps(data))

    def test_valid_report(self):
        data = self.validate(report(scenarios=[scenario('scan-restamp')]))
        self.assertEqual(data['findings'][0]['key'], 'scan-restamp')
        self.assertEqual(data['findings'][0]['kind'], 'defect')
        self.assertEqual(data['status'], 'completed')
        self.assertEqual(data['outcome'], 'failed')
        self.assertEqual(data['sha'], SHA)

    def test_report_contract_is_qa_lane_schema(self):
        """The wire contract is qa_lane.report's — nothing else validates."""
        with self.assertRaises(ValueError):
            self.validate({'schema_version': 1, 'run_id': 'x', 'sha': SHA,
                           'model': 'm', 'rig': 'r', 'status': 'completed',
                           'started_at': '2026-09-15T10:00:00Z',
                           'ended_at': '2026-09-15T11:00:00Z',
                           'findings': []})

    def test_field_errors(self):
        mutations = {
            'schema_version': lambda d: d.update(schema_version=4),
            'run_id': lambda d: d.update(run_id='Bad Key'),
            'attempted_sha': lambda d: d.update(attempted_sha='abc'),
            'outcome': lambda d: d.update(outcome='ok'),
            'finished_at': lambda d: d.update(
                finished_at='2026-09-15T09:00:00+00:00'),
            'scenario_fields': lambda d: d['scenarios'][0].update(extra='nope'),
            'scenario_outcome': lambda d: d['scenarios'][0].update(
                outcome='sure'),
            'evidence': lambda d: d['scenarios'][0].update(
                evidence=[{'kind': 'link', 'ref': 'x'}]),
        }
        for name, mutate in mutations.items():
            data = report()
            mutate(data)
            with self.assertRaises(ValueError, msg=name):
                self.validate(data)

    def test_passed_outcome_requires_all_scenarios_passed(self):
        with self.assertRaises(ValueError):
            self.validate(report(outcome='passed'))

    def test_duplicate_scenario_key_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(report(
                scenarios=[scenario('dup'), scenario('dup')]))

    def test_verification_validation(self):
        entry = findings.validate_verification(
            {'finding_key': 'scan-restamp', 'outcome': 'passed',
             'fix_sha': FIX_SHA})
        self.assertEqual(entry['outcome'], 'passed')
        for bad in ({'finding_key': 'X', 'outcome': 'passed'},
                    {'finding_key': 'scan-restamp', 'outcome': 'meh'},
                    {'finding_key': 'scan-restamp', 'outcome': 'passed',
                     'fix_sha': 'zzz'}):
            with self.assertRaises(ValueError):
                findings.validate_verification(bad)

    def test_redaction(self):
        item = scenario('leaky', detail='token gho_' + 'x' * 30 + ' seen',
                        observations=['auth header Authorization = topsecret'])
        data = self.validate(report(scenarios=[item]))
        text = json.dumps(data)
        self.assertNotIn('gho_', text)
        self.assertNotIn('topsecret', text)
        self.assertIn('[redacted]', text)

    def test_derived_finding_kinds(self):
        data = self.validate(report(
            scenarios=[scenario('failed-case', 'failed'),
                       scenario('weak-case', 'inconclusive'),
                       scenario('ok-case', 'passed')],
            capabilities=[capability('ethercat-gap', blocking=True)],
            infra=[infra_failure('rig-down', phase='preflight')]))
        by_key = {f['key']: f for f in data['findings']}
        self.assertEqual(by_key['failed-case']['kind'], 'defect')
        self.assertEqual(by_key['failed-case']['severity'], 'medium')
        self.assertEqual(by_key['failed-case']['confidence'], 'high')
        self.assertEqual(by_key['weak-case']['kind'], 'infrastructure')
        self.assertNotIn('ok-case', by_key)
        self.assertEqual(by_key['ethercat-gap']['kind'], 'capability')
        self.assertEqual(by_key['ethercat-gap']['severity'], 'medium')
        self.assertEqual(by_key['rig-down']['kind'], 'infrastructure')
        self.assertIn('preflight', by_key['rig-down']['summary'])

    def test_interrupted_and_blocked_runs_are_not_completed(self):
        for outcome in ('interrupted', 'blocked', 'inconclusive'):
            data = self.validate(report(outcome=outcome, completed=False))
            self.assertEqual(data['status'], outcome)
            self.assertEqual(data['sha'], SHA)  # falls back to attempted

    def test_real_runner_fixtures_ingest(self):
        """The fixture reports produced by qa_lane derive real findings."""
        data = findings.validate_report(
            (FIXTURES / 'report-failed.json').read_text())
        kinds = {f['key']: f['kind'] for f in data['findings']}
        self.assertEqual(kinds['standby-tracking'], 'defect')
        self.assertEqual(kinds['no-ethercat'], 'capability')
        data = findings.validate_report(
            (FIXTURES / 'report-interrupted.json').read_text())
        self.assertEqual(data['status'], 'interrupted')
        self.assertTrue(any(f['kind'] == 'infrastructure'
                            for f in data['findings']))


class RoutingTests(LaneFixture):
    def test_defect_routes_exactly_one_issue(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.assertEqual(self.github.created, 1)
        issue = self.github.items[101]
        self.assertIn('agent:ready', [l['name'] for l in issue['labels']])
        self.assertIn('priority:P2', [l['name'] for l in issue['labels']])
        self.assertIn('area:control-runtime', [l['name'] for l in issue['labels']])
        meta = planning.metadata(issue['body'])
        self.assertEqual(meta['key'], 'qa-scan-restamp')
        self.assertEqual(meta['group'], 'scan-restamp')
        self.assertEqual(meta['area'], 'control-runtime')
        self.assertIn('dcs-agent-key:qa-scan-restamp', issue['body'])
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'issue-open')
        self.assertEqual(row['issue'], 101)
        self.assertEqual(row['cycles'], 1)

    def test_never_p0_and_per_scenario_groups(self):
        drop(self.root, report(scenarios=[scenario('a'), scenario('b')]))
        self.poll()
        labels = {i['title']: [l['name'] for l in i['labels']]
                  for i in self.github.items.values()}
        self.assertTrue(all('priority:P0' not in v for v in labels.values()))
        metas = [planning.metadata(i['body']) for i in self.github.items.values()]
        self.assertEqual(sorted(m['group'] for m in metas), ['a', 'b'])
        self.assertEqual(sorted(m['priority'] for m in metas), [2, 2])

    def test_duplicate_report_and_repeated_finding_do_not_duplicate(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.poll()  # processed already; nothing new
        drop(self.root, report(run_id='qa-20990101-002',
                               scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.assertEqual(self.github.created, 1)
        self.assertEqual(self.state.qa_finding('scan-restamp')['occurrences'], 2)
        self.assertTrue((self.root / 'qa' / 'processed'
                         / 'qa-20990101-001.json').is_file())
        self.assertTrue((self.root / 'qa' / 'processed'
                         / 'qa-20990101-002.json').is_file())

    def test_existing_open_issue_is_adopted(self):
        existing = self.github.create_issue('[QA] earlier', 'body',
                                            key='qa-scan-restamp')
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.assertEqual(self.github.created, 1)
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual((row['status'], row['issue']), ('issue-open', existing))

    def test_closed_issue_regression_redispatches(self):
        number = self.github.create_issue('[QA] earlier', 'body',
                                          key='qa-scan-restamp')
        self.github.items[number]['state'] = 'CLOSED'
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.poll()  # sweep redispatches the 'failed' finding
        self.assertEqual(self.github.created, 2)
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'redispatched')
        followup = self.github.items[row['issue']]
        self.assertIn('qa-scan-restamp-fix2', followup['body'])
        self.assertEqual(planning.metadata(followup['body'])['dependencies'],
                         [number])

    def test_capability_becomes_planner_candidate(self):
        drop(self.root, report(
            scenarios=[scenario('controller-active', 'passed')],
            capabilities=[capability('ethercat-gap', blocking=True)],
            outcome='passed'))
        self.poll()
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_finding('ethercat-gap')['status'],
                         'candidate')
        cand = self.state.candidate('ethercat-gap')
        self.assertEqual(cand['disposition'], 'pending')
        self.assertEqual(json.loads(cand['payload'])['source'], 'qa')

    def test_capability_candidate_reaches_planner_and_syncs_disposition(self):
        config = dict(state_root=str(self.root / 'sup-state'),
                      pool_root=str(self.root / 'pool'), repository='fake/repo',
                      timeout_seconds=7200, required_checks=['test'],
                      poll_seconds=60,
                      qa={'enabled': True, 'mode': 'route',
                          'report_dir': str(self.root / 'qa' / 'reports')})
        supervisor = Supervisor(config)
        self.addCleanup(supervisor.state.close)
        drop(self.root, report(
            scenarios=[scenario('controller-active', 'passed')],
            capabilities=[capability('ethercat-gap')], outcome='passed'))
        supervisor.qa(self.github.issues())
        # Review lane disabled entirely; QA candidates still feed the planner.
        fed = supervisor.planner_review_input()
        self.assertEqual([c['key'] for c in fed['candidates']],
                         ['ethercat-gap'])
        # A planner accept disposition syncs back onto the finding record.
        supervisor.state.disposition_candidate('ethercat-gap', 'accepted',
                                               'planned', issue=77)
        supervisor.qa(self.github.issues())
        row = supervisor.state.qa_finding('ethercat-gap')
        self.assertEqual((row['status'], row['issue']), ('accepted', 77))

    def test_infrastructure_is_operational_record_only(self):
        # A completed run can still carry infrastructure failures.
        drop(self.root, report(scenarios=[scenario('ok-case', 'passed')],
                               infra=[infra_failure('rig-down')],
                               outcome='passed'))
        self.ingest(internal(
            run_id='qa-20990101-099',
            items=[finding('rig-product', kind='infrastructure',
                           reproduction=None, expected=None,
                           product_cause=True)]))
        self.poll()
        self.assertEqual(self.state.qa_finding('rig-down')['status'], 'infra')
        self.assertEqual(self.github.created, 1)
        self.assertEqual(self.state.qa_finding('rig-product')['status'],
                         'issue-open')

    def test_inconclusive_report_records_without_routing(self):
        drop(self.root, report(scenarios=[scenario('flaky', 'inconclusive')],
                               outcome='inconclusive', completed=False))
        self.poll()
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_finding('flaky')['status'], 'recorded')
        self.assertTrue((self.root / 'qa' / 'processed'
                         / 'qa-20990101-001.json').is_file())

    def test_invalid_report_quarantined(self):
        drop(self.root, {'bad': True})
        self.poll()
        self.assertEqual(self.state.qa_findings(), [])
        rejected = list((self.root / 'qa' / 'rejected').glob('*.json'))
        self.assertEqual(len(rejected), 1)
        self.assertTrue(rejected[0].with_suffix('.json.error.txt').is_file())

    def test_record_mode_then_route_sweeps(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll(qa={'mode': 'record'})
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'recorded')
        self.poll(qa={'mode': 'route'})
        self.assertEqual(self.github.created, 1)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'issue-open')

    def test_disabled_lane_is_inert(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll(qa={'enabled': False})
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_findings(), [])
        self.assertTrue((self.root / 'qa' / 'reports'
                         / 'qa-20990101-001.json').is_file())

    def test_open_bound_holds_overflow(self):
        drop(self.root, report(
            scenarios=[scenario('f%d' % n) for n in range(3)]))
        self.poll(qa={'max_open': 1})
        self.assertEqual(self.github.created, 1)
        self.assertEqual(sorted(r['status'] for r in self.state.qa_findings()),
                         ['held', 'held', 'issue-open'])
        # Settle the routed finding; the next sweep promotes one held finding.
        self.state.set_qa_finding('f0', status='verified')
        self.poll(qa={'max_open': 1})
        self.assertEqual(self.github.created, 2)

    def test_per_report_issue_cap(self):
        drop(self.root, report(
            scenarios=[scenario('f%d' % n) for n in range(7)]))
        self.poll(qa={'max_issues_per_report': 5, 'max_open': 99})
        self.assertEqual(self.github.created, 5)
        # The per-pass sweep budget also caps follow-up passes.
        self.poll(qa={'max_issues_per_report': 5, 'max_open': 99})
        self.assertEqual(self.github.created, 7)

    def test_interrupted_ingest_resumes_routing(self):
        doc = internal(run_id='qa-20990101-020',
                       items=[finding('scan-restamp')])

        def flaky(*args, **kw):
            raise RuntimeError('mid-route crash')

        with unittest.mock.patch.object(findings, '_route', flaky):
            with self.assertRaises(RuntimeError):
                self.ingest(doc)
        # The crash leaves the sentinel row — not a completed ingest.
        self.assertEqual(
            self.state.qa_report('qa-20990101-020')['summary'],
            findings.INGEST_SENTINEL)
        summary = self.ingest(doc)
        self.assertEqual(summary['findings']['scan-restamp'], 'issue-open')
        self.assertEqual(self.github.created, 1)
        self.assertTrue(any('resuming' in line for line in self.logs))
        # Once complete, re-delivery dedups exactly like before.
        self.assertEqual(self.ingest(doc)['result'], 'duplicate')
        self.assertEqual(self.github.created, 1)


class VerificationChainTests(LaneFixture):
    """Verification results have no schema-v1 wire channel; they are ingested
    through the internal report seam until the schema carries them."""

    def merge_finding(self, key='scan-restamp', issue=101):
        self.state.reserve(issue, 'worker-01', 'dcs-core')
        self.state.complete(issue)
        job = self.state.job(issue)
        self.state.update_job(issue, pr=42)
        self.github.pull_requests[42] = dict(merge_commit_sha=FIX_SHA)
        self.poll()

    def seed(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        return self.state.qa_finding('scan-restamp')

    def test_merged_then_verified(self):
        row = self.seed()
        self.assertEqual(row['status'], 'issue-open')
        self.merge_finding()
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'fix-merged')
        self.assertEqual(row['fix_sha'], FIX_SHA)
        pending = self.state.pending_verifications()
        self.assertEqual([p['key'] for p in pending], ['scan-restamp'])
        self.ingest(internal(run_id='qa-20990101-010', verifications=[
            verification()]))
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'verified')

    def test_failed_fix_redispatches_linked_followup(self):
        self.seed()
        self.merge_finding()
        self.ingest(internal(run_id='qa-20990101-010', verifications=[
            verification(outcome='failed',
                         evidence=[{'detail': 'still restamps'}])]))
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'redispatched')
        self.assertEqual(row['cycles'], 2)
        followup = self.github.items[row['issue']]
        self.assertIn('qa-scan-restamp-fix2', followup['body'])
        self.assertIn('Follow-up to #101', followup['body'])
        self.assertEqual(planning.metadata(followup['body'])['dependencies'],
                         [101])

    def test_fix_cycle_bound_leaves_unresolved(self):
        self.seed()
        self.merge_finding()
        for round_ in range(2):
            self.ingest(internal(run_id='qa-20990101-01%d' % round_,
                                 verifications=[
                verification(outcome='failed')]))
            if round_ == 0:
                row = self.state.qa_finding('scan-restamp')
                self.assertEqual(row['status'], 'redispatched')
                self.merge_finding(issue=row['issue'])
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'unresolved')
        self.assertEqual(self.github.created, 2)

    def test_verification_requires_fix_merged(self):
        self.seed()
        self.ingest(internal(run_id='qa-20990101-010', verifications=[
            verification()]))
        report_row = self.state.qa_report('qa-20990101-010')
        self.assertIn('not-awaiting-verification', report_row['summary'])
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'issue-open')

    def test_interrupted_ingest_resumes_verification(self):
        self.seed()
        self.merge_finding()
        doc = internal(run_id='qa-20990101-010',
                       verifications=[verification()])
        # A crash after the report mark but before apply_verification
        # leaves the finding fix-merged; the row's sentinel must let the
        # next pass resume rather than dedup into 'duplicate'.
        with unittest.mock.patch.object(
                findings, 'apply_verification',
                side_effect=RuntimeError('mid-ingest crash')):
            with self.assertRaises(RuntimeError):
                self.ingest(doc)
        self.assertEqual(
            self.state.qa_report('qa-20990101-010')['summary'],
            findings.INGEST_SENTINEL)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')
        summary = self.ingest(doc)
        self.assertEqual(summary['verifications']['scan-restamp'],
                         'verified')
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'verified')
        self.assertEqual(self.ingest(doc)['result'], 'duplicate')

    def test_verified_finding_regression_redispatches(self):
        self.seed()
        self.merge_finding()
        self.state.set_qa_finding('scan-restamp', status='verified')
        drop(self.root, report(run_id='qa-20990101-003',
                               scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.poll()
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'redispatched')
        self.assertEqual(self.github.created, 2)


class VerificationGateTests(LaneFixture):
    """A verification certifies only a report entry that matches the
    finding key, the original case, and the tested revision, carries the
    ancestry proof and real evidence, and survives the supervisor's own
    containment lookup."""

    def seed_merged(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.state.reserve(101, 'worker-01', 'dcs-core')
        self.state.complete(101)
        self.state.update_job(101, pr=42)
        self.github.pull_requests[42] = dict(merge_commit_sha=FIX_SHA)
        self.poll()
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')

    def apply(self, entry, run_id='qa-20990101-010'):
        self.ingest(internal(run_id=run_id, verifications=[entry]))
        row = self.state.qa_report(run_id)
        return row['summary']

    def test_unrelated_case_cannot_certify(self):
        self.seed_merged()
        summary = self.apply(verification(case='controller-active'))
        self.assertIn('case-mismatch', summary)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')

    def test_missing_evidence_cannot_certify(self):
        self.seed_merged()
        summary = self.apply(verification(evidence=[]))
        self.assertIn('missing-evidence', summary)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')

    def test_unproven_ancestry_cannot_certify(self):
        self.seed_merged()
        for index, bad in enumerate(
                ({'checked': True, 'contained': False}, None,
                 {'checked': False, 'contained': None})):
            summary = self.apply(verification(ancestry=bad),
                                 run_id='qa-20990101-01%d' % index)
            self.assertIn('ancestry-unproven', summary)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')

    def test_untested_revision_cannot_certify(self):
        self.seed_merged()
        summary = self.apply(verification(tested='b' * 40))
        self.assertIn('untested-revision', summary)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')

    def test_sha_mismatch_cannot_certify(self):
        self.seed_merged()
        summary = self.apply(verification(fix='c' * 40))
        self.assertIn('sha-mismatch', summary)

    def test_unknown_fix_sha_never_advances(self):
        self.seed_merged()
        self.state.set_qa_finding('scan-restamp', fix_sha=None)
        summary = self.apply(verification(fix=None))
        self.assertIn('fix-sha-unknown', summary)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')

    def test_supervisor_containment_lookup_gates(self):
        self.seed_merged()
        self.github.includes_main = lambda head, base: False
        summary = self.apply(verification())
        self.assertIn('not-contained', summary)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')

    def test_lookup_failure_stays_pending_and_retries(self):
        self.seed_merged()
        calls = []

        def flaky(head, base):
            calls.append(1)
            raise RuntimeError('network unreachable')

        self.github.includes_main = flaky
        summary = self.apply(verification())
        self.assertIn('lookup-failed', summary)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'fix-merged')
        self.assertEqual(calls, [1])
        # The next poll retries the parked entry and settles it.
        self.github.includes_main = lambda head, base: True
        self.poll()
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'verified')
        self.assertEqual(self.state.get('qa:verification_retries'), {})

    def test_reconcile_retries_failed_merge_lookup(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.state.reserve(101, 'worker-01', 'dcs-core')
        self.state.complete(101)
        self.state.update_job(101, pr=42)

        def missing(number):
            raise RuntimeError('HTTP 503')

        self.github.pr = missing
        self.poll()
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'issue-open')
        self.github.pull_requests[42] = dict(merge_commit_sha=FIX_SHA)
        self.github.pr = lambda number: self.github.pull_requests[number]
        self.poll()
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual((row['status'], row['fix_sha']),
                         ('fix-merged', FIX_SHA))

    def test_ambiguous_merge_stays_pending(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.state.reserve(101, 'worker-01', 'dcs-core')
        self.state.complete(101)
        self.state.update_job(101, pr=42)
        self.github.pull_requests[42] = dict(merge_commit_sha=None)
        self.poll()
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'],
                         'issue-open')


class VerificationQueueTests(LaneFixture):
    """The Lenovo-facing queue doc lands beside the report inbox,
    content-hashed so re-publication is idempotent."""

    def seed_merged(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        self.state.reserve(101, 'worker-01', 'dcs-core')
        self.state.complete(101)
        self.state.update_job(101, pr=42)
        self.github.pull_requests[42] = dict(merge_commit_sha=FIX_SHA)
        self.poll()

    def queue_path(self):
        return Path(self.root) / 'qa' / 'verifications.json'

    def test_queue_doc_carries_pending_items(self):
        self.seed_merged()
        self.poll()
        path = self.queue_path()
        self.assertTrue(path.is_file())
        doc = json.loads(path.read_text())
        self.assertEqual(doc['schema'], 'qa-verifications/1')
        self.assertEqual(len(doc['items']), 1)
        item = doc['items'][0]
        self.assertEqual(item['finding_key'], 'scan-restamp')
        self.assertEqual(item['case'], 'scan-restamp')
        self.assertEqual(item['fix_sha'], FIX_SHA)
        self.assertEqual(item['issue'], 101)
        self.assertIn('scenario scan-restamp', item['reproduction'])

    def test_queue_write_is_idempotent_and_current(self):
        self.seed_merged()
        self.poll()
        first = self.queue_path().read_text()
        self.poll()
        self.assertEqual(self.queue_path().read_text(), first)
        self.assertIsNotNone(self.state.get('qa:queue_hash'))
        # Settled findings drop out of the queue on the next poll.
        self.state.set_qa_finding('scan-restamp', status='verified')
        self.poll()
        doc = json.loads(self.queue_path().read_text())
        self.assertEqual(doc['items'], [])

    def test_unknown_fix_sha_not_queued(self):
        self.seed_merged()
        self.state.set_qa_finding('scan-restamp', fix_sha=None)
        self.poll()
        doc = json.loads(self.queue_path().read_text())
        self.assertEqual(doc['items'], [])


class PublicationTests(LaneFixture):
    def test_idempotent_and_tolerant_publication(self):
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        self.poll()
        calls = []

        def runner(cmd, input=None, **kw):
            calls.append(json.loads(input.decode()))
            return Mock(returncode=0, stderr=b'')

        cfg = make_cfg(self.root, qa={'dashboard': True})
        self.assertTrue(findings.publish_dashboard(self.state, cfg, self.log,
                                                   runner))
        self.assertEqual(calls[0]['findings'][0]['key'], 'scan-restamp')
        self.assertFalse(findings.publish_dashboard(self.state, cfg, self.log,
                                                    runner))
        self.assertEqual(len(calls), 1)
        self.state.set_qa_finding('scan-restamp', status='verified')
        self.assertTrue(findings.publish_dashboard(self.state, cfg, self.log,
                                                   runner))

        def failing(cmd, input=None, **kw):
            return Mock(returncode=1, stderr=b'connection refused')

        self.state.set_qa_finding('scan-restamp', status='issue-open')
        self.assertFalse(findings.publish_dashboard(self.state, cfg, self.log,
                                                    failing))
        self.assertIn('refused', self.state.get('qa:publish_error'))

    def test_document_lists_pending_verifications_first(self):
        self.ingest(internal(items=[finding()]))
        self.state.reserve(101, 'worker-01', 'dcs-core')
        self.state.update_job(101, pr=42)
        self.state.complete(101)
        self.github.pull_requests[42] = dict(merge_commit_sha=FIX_SHA)
        self.poll()
        doc = findings.document(self.state)
        self.assertEqual(len(doc['pending_verifications']), 1)
        pending = doc['pending_verifications'][0]
        self.assertEqual(pending['fix_sha'], FIX_SHA)
        self.assertEqual(pending['reproduction'],
                         'run the pacing sim and observe the stamp')


class EndToEndTests(LaneFixture):
    """Seeded defect -> one issue -> dispatch -> merge -> fix-merged -> verify."""

    def make_supervisor(self):
        config = dict(state_root=str(self.root / 'sup-state'),
                      pool_root=str(self.root / 'pool'), repository='fake/repo',
                      timeout_seconds=7200, required_checks=['test'],
                      poll_seconds=60,
                      qa={'enabled': True, 'mode': 'route',
                          'report_dir': str(self.root / 'qa' / 'reports')})
        supervisor = Supervisor(config)
        runtime = Mock()
        runtime.prepare_clone.side_effect = lambda worker, **kw: Path(config['pool_root']) / worker
        runtime.spawn.side_effect = lambda key, *a, **kw: {'invocation': key, 'key': key, 'started_at': 0}
        runtime.poll.return_value = {'exit_code': 0}
        runtime.inspect_result.return_value = {'clean': True, 'changed': True}
        runtime.run_git.return_value = 'base'
        runtime.session_id.return_value = 'session-one'
        runtime.recover.return_value = []
        supervisor.github = self.github
        supervisor.runtime = runtime
        self.addCleanup(supervisor.state.close)
        return supervisor

    def test_seeded_defect_full_loop(self):
        supervisor = self.make_supervisor()
        state = supervisor.state
        cfg = make_cfg(self.root)
        drop(self.root, report(scenarios=[scenario('scan-restamp')]))
        supervisor.qa(self.github.issues())
        self.assertEqual(self.github.created, 1)
        issue = self.github.items[101]
        # The seeded defect enters the normal worker/CI flow.
        supervisor.dispatch(self.github.issues())
        self.assertEqual(state.job(101)['status'], 'working')
        self.assertEqual(state.job(101)['concurrency_group'], 'scan-restamp')
        supervisor.reconcile_workers(self.github.issues())
        self.assertEqual(state.job(101)['status'], 'pr-open')
        supervisor.integrate(self.github.issues())
        supervisor.integrate(self.github.issues())
        self.assertEqual(state.job(101)['status'], 'done')
        # Merge alone is not verification: the finding now awaits the QA lane.
        findings.poll(state, self.github, cfg, self.github.issues(), self.log)
        row = state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'fix-merged')
        self.assertEqual(row['fix_sha'], FIX_SHA)
        doc = findings.document(state)
        self.assertEqual(doc['pending_verifications'][0]['issue'], 101)
        self.assertIn('scenario scan-restamp of QA run',
                      doc['pending_verifications'][0]['reproduction'])
        # The original reproduction passes against the merged fix — delivered
        # through the internal seam (schema v1 carries no verifications yet).
        findings.ingest_report(state, self.github, cfg,
                               internal(run_id='qa-20990101-010',
                                        verifications=[verification()]),
            self.github.issues(), self.log)
        self.assertEqual(state.qa_finding('scan-restamp')['status'], 'verified')
        self.assertEqual(self.github.created, 1)


class ExploratoryFindingsTests(LaneFixture):
    """Schema-v3 reports from qax-* runs carry the finding fields the
    exploratory session declared; absent fields keep the conservative
    coordinator defaults."""

    def exploratory(self, **scenario_kw):
        doc = report(run_id='qax-20990101-001',
                     scenarios=[scenario('receipts-lost-on-standby-window',
                                         **scenario_kw)])
        doc['schema_version'] = 3
        doc['mode'] = 'simulation'
        doc['exploration'] = {'charter': 'command-boundary-behavior'}
        return doc

    def merge(self, issue=101):
        self.state.reserve(issue, 'worker-01', 'dcs-monitor')
        self.state.complete(issue)
        self.state.update_job(issue, pr=42)
        self.github.pull_requests[42] = dict(merge_commit_sha=FIX_SHA)
        self.poll()

    def test_explicit_fields_flow_into_finding_and_issue(self):
        drop(self.root, self.exploratory(
            module='dcs-monitor', mode='simulation',
            reproduction='demote active, promote standby, GET /receipts',
            severity='high', confidence='medium',
            test_requirements='regression test driving the transition',
            product_cause=True))
        self.poll()
        row = self.state.qa_finding('receipts-lost-on-standby-window')
        self.assertEqual(row['module'], 'dcs-monitor')
        self.assertEqual(row['severity'], 'high')
        self.assertEqual(row['confidence'], 'medium')
        self.assertEqual(row['status'], 'issue-open')
        issue = self.github.items[row['issue']]
        self.assertIn('demote active, promote standby, GET /receipts',
                      issue['body'])
        self.assertIn('regression test driving the transition',
                      issue['body'])
        meta = planning.metadata(issue['body'])
        self.assertEqual(meta['group'], 'dcs-monitor')
        self.assertEqual(meta['priority'], 1)

    def test_absent_fields_keep_defaults(self):
        drop(self.root, self.exploratory())
        self.poll()
        row = self.state.qa_finding('receipts-lost-on-standby-window')
        self.assertEqual(row['module'],
                         'qa-lane/receipts-lost-on-standby-window')
        self.assertEqual(row['severity'], 'medium')
        payload = json.loads(row['payload'])
        self.assertIn('Automated scenario', payload['reproduction'])

    def test_mode_marks_probe_reproduction(self):
        drop(self.root, self.exploratory(mode='simulation'))
        self.poll()
        payload = json.loads(
            self.state.qa_finding('receipts-lost-on-standby-window')
            ['payload'])
        self.assertIn('Exploratory probe', payload['reproduction'])
        self.assertIn('mode simulation', payload['reproduction'])

    def test_exploratory_case_marked_agent_replay(self):
        drop(self.root, self.exploratory())
        self.poll()
        self.merge()
        doc = findings.verification_queue(self.state)
        self.assertEqual(doc['items'][0]['replay'], 'agent')

    def test_deterministic_case_marked_auto_replay(self):
        drop(self.root, report(scenarios=[scenario('evidence-capture')]))
        self.poll()
        self.merge()
        doc = findings.verification_queue(self.state)
        self.assertEqual(doc['items'][0]['replay'], 'auto')


class MigrationTests(unittest.TestCase):
    def test_v1_database_gains_qa_tables(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'state.sqlite3'
            state = State(path)
            state.db.execute('DROP TABLE qa_findings')
            state.db.execute('DROP TABLE qa_reports')
            state.db.execute('PRAGMA user_version=1')
            state.db.commit()
            state.close()
            state = State(path)
            state.upsert_qa_finding(finding(), internal(), 'recorded')
            self.assertEqual(state.qa_finding('scan-restamp')['status'],
                             'recorded')
            state.close()


if __name__ == '__main__':
    unittest.main()
