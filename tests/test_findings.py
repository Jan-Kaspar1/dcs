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


def finding(key='scan-restamp', kind='defect', module='crates/dcs-core',
            severity='medium', confidence='high', **kw):
    item = dict(key=key, kind=kind, module=module, severity=severity,
                confidence=confidence, title='Scan restamps samples',
                summary='Inputs are restamped to the scan tick',
                evidence=[{'detail': 'observed stale stamp at boundary'}])
    if kind == 'defect':
        item.update(reproduction='run the pacing sim and observe the stamp',
                    expected='acquisition freshness preserved')
    item.update(kw)
    return item


def report(run_id='run-1', items=(), verifications=(), status='completed', sha=SHA):
    return {'schema_version': 1, 'run_id': run_id, 'sha': sha,
            'model': 'swe-2-high', 'rig': 'simulated',
            'started_at': '2026-09-15T10:00:00Z',
            'ended_at': '2026-09-15T11:00:00Z', 'status': status,
            'findings': list(items), 'verifications': list(verifications)}


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


class ValidationTests(LaneFixture):
    def validate(self, data):
        return findings.validate_report(json.dumps(data))

    def test_valid_report(self):
        data = self.validate(report(items=[finding()]))
        self.assertEqual(data['findings'][0]['key'], 'scan-restamp')

    def test_field_errors(self):
        mutations = {
            'schema_version': lambda d: d.update(schema_version=2),
            'key': lambda d: d['findings'][0].update(key='Bad Key'),
            'kind': lambda d: d['findings'][0].update(kind='other'),
            'severity': lambda d: d['findings'][0].update(severity='p0'),
            'confidence': lambda d: d['findings'][0].update(confidence='sure'),
            'sha': lambda d: d.update(sha='abc'),
            'status': lambda d: d.update(status='ok'),
            'ended_at': lambda d: d.update(ended_at='2026-09-15T09:00:00Z'),
            'reproduction': lambda d: d['findings'][0].pop('reproduction'),
            'evidence': lambda d: d['findings'][0].pop('evidence'),
            'extra': lambda d: d['findings'][0].update(extra='nope'),
        }
        for name, mutate in mutations.items():
            data = report(items=[finding()])
            mutate(data)
            with self.assertRaises(ValueError, msg=name):
                self.validate(data)

    def test_duplicate_key_in_one_report_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(report(items=[finding(), finding()]))

    def test_verification_validation(self):
        data = self.validate(report(verifications=[
            {'finding_key': 'scan-restamp', 'outcome': 'passed', 'fix_sha': FIX_SHA}]))
        self.assertEqual(data['verifications'][0]['outcome'], 'passed')
        for bad in ({'finding_key': 'X', 'outcome': 'passed'},
                    {'finding_key': 'scan-restamp', 'outcome': 'meh'},
                    {'finding_key': 'scan-restamp', 'outcome': 'passed', 'fix_sha': 'zzz'}):
            with self.assertRaises(ValueError):
                self.validate(report(verifications=[bad]))

    def test_redaction(self):
        item = finding(evidence=[{'detail': 'token: gho_' + 'x' * 30 + ' seen'}],
                       summary='auth header Authorization = topsecret leaked')
        data = self.validate(report(items=[item]))
        text = json.dumps(data)
        self.assertNotIn('gho_', text)
        self.assertIn('[redacted]', text)


class RoutingTests(LaneFixture):
    def test_defect_routes_exactly_one_issue(self):
        drop(self.root, report(items=[finding()]))
        self.poll()
        self.assertEqual(self.github.created, 1)
        issue = self.github.items[101]
        self.assertIn('agent:ready', [l['name'] for l in issue['labels']])
        self.assertIn('priority:P2', [l['name'] for l in issue['labels']])
        meta = planning.metadata(issue['body'])
        self.assertEqual(meta['key'], 'qa-scan-restamp')
        self.assertEqual(meta['group'], 'dcs-core')
        self.assertIn('dcs-agent-key:qa-scan-restamp', issue['body'])
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'issue-open')
        self.assertEqual(row['issue'], 101)
        self.assertEqual(row['cycles'], 1)

    def test_never_p0_and_module_groups(self):
        items = [finding('a', severity='critical', module='crates/dcs-runtime'),
                 finding('b', severity='low', module='agent_pool')]
        drop(self.root, report(items=items))
        self.poll()
        labels = {i['title']: [l['name'] for l in i['labels']]
                  for i in self.github.items.values()}
        self.assertTrue(all('priority:P0' not in v for v in labels.values()))
        metas = [planning.metadata(i['body']) for i in self.github.items.values()]
        self.assertEqual(sorted(m['group'] for m in metas), ['agent_pool', 'dcs-runtime'])
        self.assertEqual(sorted(m['priority'] for m in metas), [1, 3])

    def test_duplicate_report_and_repeated_finding_do_not_duplicate(self):
        drop(self.root, report(items=[finding()]))
        self.poll()
        self.poll()  # processed already; nothing new
        drop(self.root, report(run_id='run-2', items=[finding()]))
        self.poll()
        self.assertEqual(self.github.created, 1)
        self.assertEqual(self.state.qa_finding('scan-restamp')['occurrences'], 2)
        self.assertTrue((self.root / 'qa' / 'processed' / 'run-1.json').is_file())
        self.assertTrue((self.root / 'qa' / 'processed' / 'run-2.json').is_file())

    def test_existing_open_issue_is_adopted(self):
        existing = self.github.create_issue('[QA] earlier', 'body', key='qa-scan-restamp')
        drop(self.root, report(items=[finding()]))
        self.poll()
        self.assertEqual(self.github.created, 1)
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual((row['status'], row['issue']), ('issue-open', existing))

    def test_closed_issue_regression_redispatches(self):
        number = self.github.create_issue('[QA] earlier', 'body', key='qa-scan-restamp')
        self.github.items[number]['state'] = 'CLOSED'
        drop(self.root, report(items=[finding()]))
        self.poll()
        self.poll()  # sweep redispatches the 'failed' finding
        self.assertEqual(self.github.created, 2)
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'redispatched')
        followup = self.github.items[row['issue']]
        self.assertIn('qa-scan-restamp-fix2', followup['body'])
        self.assertEqual(planning.metadata(followup['body'])['dependencies'], [number])

    def test_capability_becomes_planner_candidate(self):
        drop(self.root, report(items=[finding('ethercat-gap', kind='capability',
                                            reproduction=None, expected=None)]))
        self.poll()
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_finding('ethercat-gap')['status'], 'candidate')
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
        drop(self.root, report(items=[finding('ethercat-gap', kind='capability')]))
        supervisor.qa(self.github.issues())
        # Review lane disabled entirely; QA candidates still feed the planner.
        fed = supervisor.planner_review_input()
        self.assertEqual([c['key'] for c in fed['candidates']], ['ethercat-gap'])
        # A planner accept disposition syncs back onto the finding record.
        supervisor.state.disposition_candidate('ethercat-gap', 'accepted', 'planned', issue=77)
        supervisor.qa(self.github.issues())
        row = supervisor.state.qa_finding('ethercat-gap')
        self.assertEqual((row['status'], row['issue']), ('accepted', 77))

    def test_infrastructure_is_operational_record_only(self):
        items = [finding('rig-down', kind='infrastructure',
                         reproduction=None, expected=None),
                 finding('rig-product', kind='infrastructure', product_cause=True)]
        drop(self.root, report(items=items))
        self.poll()
        self.assertEqual(self.state.qa_finding('rig-down')['status'], 'infra')
        self.assertEqual(self.github.created, 1)
        self.assertEqual(self.state.qa_finding('rig-product')['status'], 'issue-open')

    def test_inconclusive_report_records_without_routing(self):
        drop(self.root, report(status='inconclusive', items=[finding()]))
        self.poll()
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'], 'recorded')
        self.assertTrue((self.root / 'qa' / 'processed' / 'run-1.json').is_file())

    def test_invalid_report_quarantined(self):
        drop(self.root, {'bad': True})
        self.poll()
        self.assertEqual(self.state.qa_findings(), [])
        rejected = list((self.root / 'qa' / 'rejected').glob('*.json'))
        self.assertEqual(len(rejected), 1)
        self.assertTrue(rejected[0].with_suffix('.json.error.txt').is_file())

    def test_record_mode_then_route_sweeps(self):
        drop(self.root, report(items=[finding()]))
        self.poll(qa={'mode': 'record'})
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'], 'recorded')
        self.poll(qa={'mode': 'route'})
        self.assertEqual(self.github.created, 1)
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'], 'issue-open')

    def test_disabled_lane_is_inert(self):
        drop(self.root, report(items=[finding()]))
        self.poll(qa={'enabled': False})
        self.assertEqual(self.github.created, 0)
        self.assertEqual(self.state.qa_findings(), [])
        self.assertTrue((self.root / 'qa' / 'reports' / 'run-1.json').is_file())

    def test_open_bound_holds_overflow(self):
        items = [finding('f%d' % n) for n in range(3)]
        drop(self.root, report(items=items))
        self.poll(qa={'max_open': 1})
        self.assertEqual(self.github.created, 1)
        self.assertEqual(sorted(r['status'] for r in self.state.qa_findings()),
                         ['held', 'held', 'issue-open'])
        # Settle the routed finding; the next sweep promotes one held finding.
        self.state.set_qa_finding('f0', status='verified')
        self.poll(qa={'max_open': 1})
        self.assertEqual(self.github.created, 2)

    def test_per_report_issue_cap(self):
        items = [finding('f%d' % n) for n in range(7)]
        drop(self.root, report(items=items))
        self.poll(qa={'max_issues_per_report': 5, 'max_open': 99})
        self.assertEqual(self.github.created, 5)
        # The per-pass sweep budget also caps follow-up passes.
        self.poll(qa={'max_issues_per_report': 5, 'max_open': 99})
        self.assertEqual(self.github.created, 7)


class VerificationChainTests(LaneFixture):
    def merge_finding(self, key='scan-restamp', issue=101):
        self.state.reserve(issue, 'worker-01', 'dcs-core')
        self.state.complete(issue)
        job = self.state.job(issue)
        self.state.update_job(issue, pr=42)
        self.github.pull_requests[42] = dict(merge_commit_sha=FIX_SHA)
        self.poll()

    def seed(self):
        drop(self.root, report(items=[finding()]))
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
        drop(self.root, report(run_id='run-2', verifications=[
            {'finding_key': 'scan-restamp', 'outcome': 'passed', 'fix_sha': FIX_SHA}]))
        self.poll()
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'], 'verified')

    def test_failed_fix_redispatches_linked_followup(self):
        self.seed()
        self.merge_finding()
        drop(self.root, report(run_id='run-2', verifications=[
            {'finding_key': 'scan-restamp', 'outcome': 'failed', 'fix_sha': FIX_SHA,
             'evidence': ['still restamps']}]))
        self.poll()
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'redispatched')
        self.assertEqual(row['cycles'], 2)
        followup = self.github.items[row['issue']]
        self.assertIn('qa-scan-restamp-fix2', followup['body'])
        self.assertIn('Follow-up to #101', followup['body'])
        self.assertEqual(planning.metadata(followup['body'])['dependencies'], [101])

    def test_fix_cycle_bound_leaves_unresolved(self):
        self.seed()
        self.merge_finding()
        for round_ in range(2):
            drop(self.root, report(run_id='v%d' % round_, verifications=[
                {'finding_key': 'scan-restamp', 'outcome': 'failed'}]))
            self.poll()
            if round_ == 0:
                row = self.state.qa_finding('scan-restamp')
                self.assertEqual(row['status'], 'redispatched')
                self.merge_finding(issue=row['issue'])
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'unresolved')
        self.assertEqual(self.github.created, 2)

    def test_verification_requires_fix_merged(self):
        self.seed()
        drop(self.root, report(run_id='run-2', verifications=[
            {'finding_key': 'scan-restamp', 'outcome': 'passed'}]))
        self.poll()
        report_row = self.state.qa_report('run-2')
        self.assertIn('not-awaiting-verification', report_row['summary'])
        self.assertEqual(self.state.qa_finding('scan-restamp')['status'], 'issue-open')

    def test_verified_finding_regression_redispatches(self):
        self.seed()
        self.merge_finding()
        self.state.set_qa_finding('scan-restamp', status='verified')
        drop(self.root, report(run_id='run-3', items=[finding()]))
        self.poll()
        self.poll()
        row = self.state.qa_finding('scan-restamp')
        self.assertEqual(row['status'], 'redispatched')
        self.assertEqual(self.github.created, 2)


class PublicationTests(LaneFixture):
    def test_idempotent_and_tolerant_publication(self):
        drop(self.root, report(items=[finding()]))
        self.poll()
        calls = []

        def runner(cmd, input=None, **kw):
            calls.append(json.loads(input.decode()))
            return Mock(returncode=0, stderr=b'')

        cfg = make_cfg(self.root, qa={'dashboard': True})
        self.assertTrue(findings.publish_dashboard(self.state, cfg, self.log, runner))
        self.assertEqual(calls[0]['findings'][0]['key'], 'scan-restamp')
        self.assertFalse(findings.publish_dashboard(self.state, cfg, self.log, runner))
        self.assertEqual(len(calls), 1)
        self.state.set_qa_finding('scan-restamp', status='verified')
        self.assertTrue(findings.publish_dashboard(self.state, cfg, self.log, runner))

        def failing(cmd, input=None, **kw):
            return Mock(returncode=1, stderr=b'connection refused')

        self.state.set_qa_finding('scan-restamp', status='issue-open')
        self.assertFalse(findings.publish_dashboard(self.state, cfg, self.log, failing))
        self.assertIn('refused', self.state.get('qa:publish_error'))

    def test_document_lists_pending_verifications_first(self):
        drop(self.root, report(items=[finding()]))
        self.poll()
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
        drop(self.root, report(items=[finding()]))
        supervisor.qa(self.github.issues())
        self.assertEqual(self.github.created, 1)
        issue = self.github.items[101]
        # The seeded defect enters the normal worker/CI flow.
        supervisor.dispatch(self.github.issues())
        self.assertEqual(state.job(101)['status'], 'working')
        self.assertEqual(state.job(101)['concurrency_group'], 'dcs-core')
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
        self.assertEqual(doc['pending_verifications'][0]['reproduction'],
                         'run the pacing sim and observe the stamp')
        # The original reproduction passes against the merged fix.
        drop(self.root, report(run_id='verify-1', verifications=[
            {'finding_key': 'scan-restamp', 'outcome': 'passed', 'fix_sha': FIX_SHA}]))
        findings.poll(state, self.github, cfg, self.github.issues(), self.log)
        self.assertEqual(state.qa_finding('scan-restamp')['status'], 'verified')
        self.assertEqual(self.github.created, 1)


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
            state.upsert_qa_finding(finding(), report(), 'recorded')
            self.assertEqual(state.qa_finding('scan-restamp')['status'], 'recorded')
            state.close()


if __name__ == '__main__':
    unittest.main()
