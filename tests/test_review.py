import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from agent_pool import planning, review
from agent_pool.github import GitHubError
from agent_pool.supervisor import Supervisor


BERLIN = ZoneInfo('Europe/Berlin')
PROMPT_TEMPLATE = ('run __RUN_ID__ at __BASE_SHA__ previous __PREVIOUS_SHA__ '
                   'report __REPORT_DIR__ resources __RESOURCE_DIR__ '
                   'focus __COVERAGE_FOCUS__ max __MAX_CANDIDATES__\n'
                   '__RUN_CONTEXT__')


def resource_clone(root):
    """A minimal checkout carrying verifiable architecture resources."""
    clone = Path(root) / 'clone'
    res = clone / 'agent_pool/resources/architecture'
    res.mkdir(parents=True)
    (res / 'policy.md').write_text('policy text')
    (res / 'prompt.md').write_text(PROMPT_TEMPLATE)
    (res / 'NOTICE.md').write_text('notice')
    (res / 'references/codebase-design').mkdir(parents=True)
    (res / 'references/codebase-design/SKILL.md').write_text('vocabulary')
    review.write_manifest(res, 'test-revision')
    (clone / 'AGENTS.md').write_text('vision')
    (clone / 'docs').mkdir()
    (clone / 'docs/architecture.md').write_text('decisions')
    (clone / 'docs/plan.md').write_text('plan')
    (clone / 'crates/dcs-core/src').mkdir(parents=True)
    (clone / 'crates/dcs-core/src/lib.rs').write_text('pub fn x() {}')
    return clone


def candidate(key='deepen-executor', outcome='depth', title='Deepen the executor'):
    item = dict(key=key, title=title, outcome=outcome,
                evidence=[{'path': 'crates/dcs-core/src/lib.rs', 'detail': 'shallow wrapper'}],
                affected_modules=['crates/dcs-runtime'], problem='p', proposal='q',
                caller_benefit='r', invariants=['scan order preserved'],
                test_approach='tests', alternatives='none', confidence='medium',
                effort='small', risk='low', related_issues=[], decision_conflicts=[],
                naming=None)
    if outcome != 'depth':
        item['naming'] = {'canonical': 'signal', 'aliases': ['sample'],
                          'compatibility': 'wire names unchanged'}
    return item


def report(clone, run_id, sha, status='completed', candidates=(), assessments=()):
    return {'schema_version': 1, 'run_id': run_id, 'base_sha': sha,
            'previous_reviewed_sha': None,
            'started_at': '2026-09-14T03:00:00+02:00',
            'ended_at': '2026-09-14T04:00:00+02:00',
            **review.verify_resources(clone),
            'status': status, 'scope': ['crates/dcs-core'], 'missing_context': [],
            'candidates': list(candidates), 'assessments': list(assessments)}


class ScheduleTests(unittest.TestCase):
    def cfg(self):
        return review.settings({'review': {'enabled': True}})

    def test_daily_slot(self):
        # 10:00 UTC is 12:00 CEST; today's 03:00 CEST is 01:00 UTC.
        now = datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc).timestamp()
        slot = review.current_slot(now, self.cfg())
        self.assertEqual(slot, int(datetime(2026, 9, 14, 1, 0, tzinfo=timezone.utc).timestamp()))

    def test_before_scheduled_uses_yesterday(self):
        now = datetime(2026, 9, 14, 0, 30, tzinfo=timezone.utc).timestamp()  # 02:30 CEST
        slot = review.current_slot(now, self.cfg())
        local = datetime.fromtimestamp(slot, BERLIN)
        self.assertEqual((local.day, local.hour), (13, 3))

    def test_dst_spring_forward(self):
        # 2026-03-29 02:00 CET -> 03:00 CEST; the 03:00 slot exists at 01:00 UTC.
        slot = review.current_slot(datetime(2026, 3, 29, 10, 0, tzinfo=timezone.utc).timestamp(), self.cfg())
        self.assertEqual(slot, int(datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc).timestamp()))

    def test_dst_fall_back_single_slot(self):
        # 2026-10-25 03:00 CEST -> 02:00 CET; the ambiguous local 03:00 resolves
        # once at 01:00 UTC, so the day still owes exactly one run.
        cfg = self.cfg()
        slot = review.current_slot(datetime(2026, 10, 25, 10, 0, tzinfo=timezone.utc).timestamp(), cfg)
        local = datetime.fromtimestamp(slot, BERLIN)
        self.assertEqual((local.year, local.month, local.day, local.hour, local.minute),
                         (2026, 10, 25, 3, 0))
        previous = review.current_slot(datetime(2026, 10, 24, 10, 0, tzinfo=timezone.utc).timestamp(), cfg)
        following = review.current_slot(datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc).timestamp(), cfg)
        self.assertLess(previous, slot)
        self.assertLess(slot, following)


class ReportValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.clone = resource_clone(Path(self.tmp.name))
        self.hashes = review.verify_resources(self.clone)

    def tearDown(self):
        self.tmp.cleanup()

    def validate(self, data, **kw):
        args = dict(clone=self.clone, run_id='r1', base_sha='sha-1',
                    known_issues={1}, expected_hashes=self.hashes)
        args.update(kw)
        return review.validate_report(json.dumps(data), **args)

    def test_valid_report(self):
        data = report(self.clone, 'r1', 'sha-1', candidates=[candidate()])
        self.assertEqual(self.validate(data)['status'], 'completed')

    def test_valid_empty_report(self):
        data = report(self.clone, 'r1', 'sha-1')
        self.assertEqual(self.validate(data)['candidates'], [])

    def test_rejects_bad_json_and_fields(self):
        with self.assertRaises(ValueError):
            review.validate_report('not json', self.clone, 'r1', 'sha-1')
        data = report(self.clone, 'r1', 'sha-1')
        del data['status']
        with self.assertRaises(ValueError):
            self.validate(data)
        data['extra'] = 'x'
        data['status'] = 'completed'
        with self.assertRaises(ValueError):
            self.validate(data)

    def test_rejects_wrong_run_base_and_hashes(self):
        data = report(self.clone, 'r2', 'sha-1')
        with self.assertRaises(ValueError):
            self.validate(data)
        data = report(self.clone, 'r1', 'sha-2')
        with self.assertRaises(ValueError):
            self.validate(data)
        data = report(self.clone, 'r1', 'sha-1')
        data['policy_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.validate(data)

    def test_rejects_candidate_limit_and_evidence_paths(self):
        data = report(self.clone, 'r1', 'sha-1',
                      candidates=[candidate(f'c{i}') for i in range(4)])
        with self.assertRaises(ValueError):
            self.validate(data)
        bad = report(self.clone, 'r1', 'sha-1', candidates=[candidate()])
        bad['candidates'][0]['evidence'] = [{'path': 'missing/file.rs', 'detail': 'x'}]
        with self.assertRaises(ValueError):
            self.validate(bad)
        bad['candidates'][0]['evidence'] = [{'path': '../escape', 'detail': 'x'}]
        with self.assertRaises(ValueError):
            self.validate(bad)
        bad['candidates'][0]['evidence'] = [{'path': '/etc/passwd', 'detail': 'x'}]
        with self.assertRaises(ValueError):
            self.validate(bad)

    def test_naming_rules_and_assessments(self):
        bad = report(self.clone, 'r1', 'sha-1', candidates=[candidate(outcome='naming')])
        bad['candidates'][0]['naming'] = None
        with self.assertRaises(ValueError):
            self.validate(bad)
        ok = report(self.clone, 'r1', 'sha-1', candidates=[candidate(outcome='naming')])
        self.assertEqual(self.validate(ok)['candidates'][0]['naming']['canonical'], 'signal')
        bad = report(self.clone, 'r1', 'sha-1', candidates=[candidate()])
        bad['candidates'][0]['related_issues'] = [999]
        with self.assertRaises(ValueError):
            self.validate(bad)
        bad = report(self.clone, 'r1', 'sha-1',
                     assessments=[{'key': 'deepen-executor', 'outcome': 'confirmed',
                                   'observed': 'ok', 'followup_candidate': candidate('x')}])
        with self.assertRaises(ValueError):
            self.validate(bad)
        ok = report(self.clone, 'r1', 'sha-1',
                    assessments=[{'key': 'deepen-executor', 'outcome': 'followup',
                                  'observed': 'partial', 'followup_candidate': candidate('executor-followup')}])
        self.assertEqual(len(self.validate(ok)['assessments']), 1)

    def test_depth_candidate_may_omit_naming(self):
        item = candidate()
        del item['naming']
        data = report(self.clone, 'r1', 'sha-1', candidates=[item])
        self.assertEqual(self.validate(data)['candidates'][0]['key'], 'deepen-executor')
        item['outcome'] = 'naming'
        with self.assertRaises(ValueError):
            self.validate(report(self.clone, 'r1', 'sha-1', candidates=[item]))


class FakeGitHub:
    def __init__(self, sha='sha-aaa'):
        self.items = []
        self.sha = sha
        self.pull_requests = {}
        self.checks = {'test': 'success'}
        self.comments = []
        self.fail_once = False

    def main_sha(self):
        return self.sha

    def issue(self, number):
        return next(i for i in self.items if i['number'] == number)

    def create_issue(self, title, body, labels=(), key=None):
        if self.fail_once:
            # Simulate an API timeout after GitHub accepted the issue: the row
            # exists, but the caller sees a failure.
            self.fail_once = False
            self._create(title, body, labels, key)
            raise GitHubError('HTTP 504: timeout')
        return self._create(title, body, labels, key)

    def _create(self, title, body, labels, key):
        if key:
            marker = '<!-- dcs-agent-key:' + key + ' -->'
            for item in self.items:
                if marker in item['body']:
                    return item['number']
            body += '\n\n' + marker
        number = max([0] + [i['number'] for i in self.items]) + 1
        self.items.append(dict(number=number, title=title, body=body, state='OPEN',
                               labels=[{'name': l} for l in labels]))
        return number

    def update_issue(self, number, title=None, body=None, add_labels=(), remove_labels=()):
        issue = self.issue(number)
        names = [l['name'] for l in issue['labels']]
        names = [n for n in names if n not in remove_labels] + [n for n in add_labels if n not in names]
        issue['labels'] = [{'name': n} for n in names]

    def comment(self, number, body):
        self.comments.append((number, body))


def managed_issue(number, improvement=None, priority=2, group='core', dependencies=(), state='OPEN'):
    item = dict(key=f'issue-{number}', title=f'Task {number}', scope='s', acceptance='a',
                tests='t', dependencies=list(dependencies), priority=priority,
                milestone='m', group=group)
    if improvement:
        item['improvement'] = improvement
    return dict(number=number, title=item['title'], body=planning.body(item),
                state=state, labels=[{'name': 'agent:ready'}, {'name': f'priority:P{priority}'}])


class ReviewLaneTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.clone = resource_clone(root)
        self.config = dict(state_root=str(root / 'state'), pool_root=str(root / 'pool'),
                           repository='fake/repo', timeout_seconds=7200,
                           required_checks=['test'], poll_seconds=60,
                           review={'enabled': True, 'mode': 'report', 'auto_promote': True,
                                   'time': '03:00', 'timezone': 'Europe/Berlin',
                                   'timeout_seconds': 3600, 'max_candidates': 3,
                                   'retention_days': 30})
        self.supervisor = Supervisor(self.config)
        self.github = FakeGitHub()
        self.runtime = Mock()
        self.runtime.prepare_clone.side_effect = lambda worker, **kw: self.clone
        self.runtime.spawn.side_effect = lambda key, *a, **kw: {'invocation': key, 'key': key, 'started_at': 0}
        self.runtime.poll.return_value = None
        self.runtime.run_git.return_value = 'abc123 commit'
        self.runtime.session_id.return_value = 'session-one'
        self.runtime.recover.return_value = []
        self.supervisor.github = self.github
        self.supervisor.runtime = self.runtime
        # Reviewer instructions come from the installed release; in tests the
        # fixture clone plays that role so hash checks stay self-contained.
        installed = patch.object(review, 'installed_root', return_value=self.clone)
        installed.start()
        self.addCleanup(installed.stop)

    def tearDown(self):
        self.supervisor.state.close()
        self.tmp.cleanup()

    def launched(self, issues=(), prs=()):
        self.supervisor.review(list(issues), list(prs))
        return self.supervisor.state.get('reviewer')

    def ingest(self, receipt=None, issues=()):
        record = self.supervisor.state.get('reviewer')
        self.runtime.poll.return_value = receipt or {'status': 'completed', 'exit_code': 0}
        self.supervisor.review(list(issues), [])
        return record

    def test_disabled_lane_is_inert(self):
        del self.config['review']
        supervisor = Supervisor(self.config)
        supervisor.github, supervisor.runtime = self.github, self.runtime
        supervisor.review([], [])
        self.assertIsNone(supervisor.state.get('reviewer'))
        self.assertIsNone(supervisor.state.review())
        self.runtime.spawn.assert_not_called()
        supervisor.state.close()

    def test_launch_and_ingest_completed_report(self):
        record = self.launched([managed_issue(1)])
        self.assertIsNotNone(record)
        self.assertEqual(self.supervisor.state.review(record['run_id'])['status'], 'running')
        spawn = self.runtime.spawn.call_args
        self.assertTrue(spawn.args[0].startswith('review-'))
        self.assertEqual(spawn.kwargs['timeout'], 3600)
        self.assertIn(record['report_dir'], spawn.args[2])
        report_dir = Path(record['report_dir'])
        self.assertTrue((report_dir / 'resources' / 'policy.md').is_file())
        self.assertIn(str(report_dir / 'resources'), spawn.args[2])
        data = report(self.clone, record['run_id'], 'sha-aaa', candidates=[candidate()])
        (report_dir / 'report.json').write_text(json.dumps(data))
        (report_dir / 'report.md').write_text('# review')
        self.ingest(issues=[managed_issue(1)])
        self.assertIsNone(self.supervisor.state.get('reviewer'))
        row = self.supervisor.state.review(record['run_id'])
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['completed_sha'], 'sha-aaa')
        cand = self.supervisor.state.candidate('deepen-executor')
        self.assertEqual(cand['disposition'], 'pending')
        self.assertEqual(self.supervisor.state.get('review:cursor'), 1)
        self.assertEqual(self.supervisor.review_stage(), 'pilot')

    def test_unchanged_base_skips(self):
        self.launched()
        record = self.supervisor.state.get('reviewer')
        data = report(self.clone, record['run_id'], 'sha-aaa')
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        self.supervisor.state.set('review:last_slot', 0)
        self.supervisor.review([], [])
        self.assertIsNone(self.supervisor.state.get('reviewer'))
        self.assertEqual(self.supervisor.state.review()['status'], 'skipped')
        self.assertEqual(self.runtime.spawn.call_count, 1)

    def test_invalid_report_is_inconclusive_with_one_retry(self):
        record = self.launched()
        (Path(record['report_dir']) / 'report.json').write_text('{"wrong": true}')
        self.ingest()
        self.assertEqual(self.supervisor.state.review(record['run_id'])['status'], 'inconclusive')
        # The single automatic retry launches immediately for the same slot.
        retry = self.supervisor.state.get('reviewer')
        self.assertEqual(retry['attempt'], 2)
        self.ingest(receipt={'status': 'timeout', 'exit_code': None})
        self.assertIsNone(self.supervisor.state.get('reviewer'))
        self.supervisor.review([], [])
        self.assertIsNone(self.supervisor.state.get('reviewer'))
        self.assertEqual(self.runtime.spawn.call_count, 2)

    def test_capacity_pause_and_manual_run(self):
        for n in range(1, 6):
            self.supervisor.state.reserve(n, f'worker-{n:02}', f'g{n}')
        self.assertIsNone(self.launched())
        for n in range(1, 6):
            self.supervisor.state.complete(n)
        self.supervisor.state.pause()
        self.assertIsNone(self.launched())
        self.supervisor.state.resume()
        self.supervisor.state.set('review:manual', True)
        self.assertIsNotNone(self.launched())
        self.assertFalse(self.supervisor.state.get('review:manual'))

    def test_interrupted_launch_and_recovery(self):
        self.supervisor.state.begin_review('r-stale', 1, 'sha-aaa', 1)
        self.supervisor.review([], [])
        self.assertEqual(self.supervisor.state.review('r-stale')['status'], 'inconclusive')
        self.assertIsNotNone(self.supervisor.state.get('reviewer'))
        intent = self.supervisor.state.get('reviewer:launch')
        self.supervisor.state.set('reviewer', None)
        record = {'invocation': 'inv-r', 'key': intent['key'], 'pid': 1, 'identity': 'x',
                  'cwd': str(self.clone), 'receipt': 'r'}
        self.runtime.recover.return_value = [record]
        self.runtime.poll.return_value = {'status': 'completed', 'exit_code': 0}
        self.supervisor.recover_processes()
        self.assertEqual(self.supervisor.state.get('reviewer')['run_id'], intent['run_id'])

    def test_preflight_failure_fails_explicitly(self):
        (self.clone / 'agent_pool/resources/architecture/policy.md').write_text('tampered')
        self.launched()
        self.assertIsNone(self.supervisor.state.get('reviewer'))
        row = self.supervisor.state.review()
        self.assertEqual(row['status'], 'inconclusive')
        self.assertIn('hash mismatch', row['error'])
        self.runtime.spawn.assert_not_called()

    def test_dispositions_accept_defer_reject(self):
        record = self.launched([managed_issue(1)])
        data = report(self.clone, record['run_id'], 'sha-aaa',
                      candidates=[candidate('deepen-executor'), candidate('rename-tick', 'naming', 'Rename tick'),
                                  candidate('speculative-x', 'depth', 'Speculative')])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest(issues=[managed_issue(1)])
        arch = dict(key='arch-deepen-executor', title='Deepen executor', scope='s',
                    acceptance='a', tests='t', dependencies=[], priority=2,
                    milestone='architecture', group='dcs-runtime', improvement='deepen-executor')
        proposal = {'issues': [arch], 'dispositions': [
            {'key': 'deepen-executor', 'decision': 'accept', 'reason': 'real friction'},
            {'key': 'rename-tick', 'decision': 'defer', 'reason': 'needs wire policy',
             'revisit': 'after model versioning decision'},
            {'key': 'speculative-x', 'decision': 'reject', 'reason': 'no caller benefit'}]}
        self.supervisor.state.set('pending_proposal', proposal)
        self.supervisor.planner([managed_issue(1)], [])
        self.assertEqual(self.supervisor.state.candidate('deepen-executor')['disposition'], 'accepted')
        created = next(i for i in self.github.items if i['title'] == 'Deepen executor')
        self.assertEqual(self.supervisor.state.improvement_issues('deepen-executor'), [created['number']])
        self.assertEqual(self.supervisor.state.candidate('rename-tick')['disposition'], 'deferred')
        self.assertEqual(self.supervisor.state.candidate('rename-tick')['revisit'],
                         'after model versioning decision')
        self.assertEqual(self.supervisor.state.candidate('speculative-x')['disposition'], 'rejected')

    def test_accept_without_mapped_issue_auto_defers(self):
        record = self.launched()
        data = report(self.clone, record['run_id'], 'sha-aaa', candidates=[candidate()])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        proposal = {'issues': [], 'dispositions': [
            {'key': 'deepen-executor', 'decision': 'accept', 'reason': 'forgot the issue'}]}
        self.supervisor.state.set('pending_proposal', proposal)
        self.supervisor.planner([], [])
        self.assertEqual(self.supervisor.state.candidate('deepen-executor')['disposition'], 'deferred')

    def test_duplicate_candidate_keys_suppressed(self):
        record = self.launched()
        data = report(self.clone, record['run_id'], 'sha-aaa', candidates=[candidate()])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        self.supervisor.state.disposition_candidate('deepen-executor', 'rejected', 'no benefit')
        self.supervisor.state.set('review:last_slot', 0)
        self.github.sha = 'sha-bbb'
        record = self.launched()
        data = report(self.clone, record['run_id'], 'sha-bbb', candidates=[candidate()])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        self.assertEqual(len(self.supervisor.state.candidates()), 1)
        self.assertEqual(self.supervisor.state.candidate('deepen-executor')['disposition'], 'rejected')

    def test_one_active_improvement(self):
        self.github.items = [managed_issue(10, improvement='imp-a'),
                             managed_issue(11, improvement='imp-b')]
        self.supervisor.state.map_improvement('imp-a', 10)
        self.supervisor.state.map_improvement('imp-b', 11)
        self.supervisor.dispatch(self.github.items)
        self.assertEqual(self.supervisor.state.job(10)['status'], 'working')
        self.assertIsNone(self.supervisor.state.job(11))
        self.assertEqual(self.supervisor.state.get('review:active_improvement'), 'imp-a')
        self.supervisor.state.complete(10)
        self.supervisor.dispatch(self.github.items)
        self.assertEqual(self.supervisor.state.job(11)['status'], 'working')
        self.assertEqual(self.supervisor.state.get('review:active_improvement'), 'imp-b')

    def test_priority_label_agreement(self):
        issue = managed_issue(7, priority=0)
        issue['labels'] = [{'name': 'agent:ready'}, {'name': 'priority:P3'}]
        self.github.items = [issue]
        self.supervisor.mirror(self.github.items)
        names = [l['name'] for l in self.github.issue(7)['labels']]
        self.assertIn('priority:P0', names)
        self.assertNotIn('priority:P3', names)

    def test_pending_assessment_requires_done_issues(self):
        self.supervisor.state.begin_review('r0', 1, 'sha-0', 1)
        self.supervisor.state.record_candidates('r0', [candidate()])
        self.supervisor.state.disposition_candidate('deepen-executor', 'accepted', 'ok', issue=10)
        self.supervisor.state.map_improvement('deepen-executor', 10)
        self.assertEqual(self.supervisor.state.pending_assessments(), [])
        self.supervisor.state.reserve(10, 'worker-01', 'g')
        self.assertEqual(self.supervisor.state.pending_assessments(), [])
        self.supervisor.state.complete(10)
        self.assertEqual([c['key'] for c in self.supervisor.state.pending_assessments()],
                         ['deepen-executor'])

    def test_issue_creation_idempotent_after_uncertain_response(self):
        item = dict(key='arch-deepen-executor', title='Deepen executor', scope='s',
                    acceptance='a', tests='t', dependencies=[], priority=2,
                    milestone='architecture', group='dcs-runtime',
                    improvement='deepen-executor')
        self.supervisor.state.set('pending_proposal', {'issues': [item], 'dispositions': []})
        self.github.fail_once = True
        with self.assertRaises(GitHubError):
            self.supervisor.planner([], [])
        self.assertIsNotNone(self.supervisor.state.get('pending_proposal'))
        self.supervisor.planner([], [])
        self.assertEqual(len(self.github.items), 1)
        self.assertEqual(self.supervisor.state.improvement_issues('deepen-executor'), [1])

    def test_planner_input_gated_by_stage(self):
        self.assertIsNone(self.supervisor.planner_review_input())
        self.supervisor.state.begin_review('r0', 1, 'sha-0', 1)
        self.supervisor.state.record_candidates('r0', [candidate()])
        self.assertIsNone(self.supervisor.planner_review_input())
        self.supervisor.state.set('review:stage', 'pilot')
        fed = self.supervisor.planner_review_input()
        self.assertEqual([c['key'] for c in fed['candidates']], ['deepen-executor'])
        self.supervisor.state.set('review:stage', 'full')
        fed = self.supervisor.planner_review_input()
        self.assertEqual([c['key'] for c in fed['candidates']], ['deepen-executor'])

    def test_assessment_rejection_rolls_back_stage(self):
        self.supervisor.state.set('review:stage', 'full')
        record = self.launched()
        self.supervisor.state.record_candidates('earlier', [candidate()])
        self.supervisor.state.disposition_candidate('deepen-executor', 'accepted', 'ok', issue=10)
        self.supervisor.state.map_improvement('deepen-executor', 10)
        self.supervisor.state.reserve(10, 'worker-01', 'g')
        self.supervisor.state.complete(10)
        data = report(self.clone, record['run_id'], 'sha-aaa', assessments=[
            {'key': 'deepen-executor', 'outcome': 'rejected', 'observed': 'callers unchanged'}])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        self.assertTrue(self.supervisor.state.candidate('deepen-executor')['assessed'])
        self.assertEqual(self.supervisor.review_stage(), 'report')
        self.assertIsNotNone(self.supervisor.state.get('review:rollout_failure'))

    def test_inconclusive_report_publishes_no_findings(self):
        self.supervisor.state.record_candidates('r-old', [candidate('merged-fix')])
        self.supervisor.state.disposition_candidate('merged-fix', 'accepted', 'ok', issue=10)
        self.supervisor.state.map_improvement('merged-fix', 10)
        self.supervisor.state.reserve(10, 'worker-01', 'g')
        self.supervisor.state.complete(10)
        record = self.launched()
        data = report(self.clone, record['run_id'], 'sha-aaa', status='inconclusive',
                      candidates=[candidate()],
                      assessments=[{'key': 'merged-fix', 'outcome': 'confirmed',
                                    'observed': 'looks good'}])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        row = self.supervisor.state.review(record['run_id'])
        self.assertEqual(row['status'], 'inconclusive')
        self.assertIsNone(self.supervisor.state.candidate('deepen-executor'))
        self.assertEqual(self.supervisor.state.candidate('merged-fix')['assessed'], 0)
        self.assertEqual(self.supervisor.review_stage(), 'report')

    def test_malformed_candidate_report_is_bounded_inconclusive(self):
        record = self.launched()
        data = report(self.clone, record['run_id'], 'sha-aaa', candidates=[candidate()])
        del data['candidates'][0]['problem']
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        row = self.supervisor.state.review(record['run_id'])
        self.assertEqual(row['status'], 'inconclusive')
        self.assertIn('Invalid report', row['error'])
        # The single automatic retry launched; no findings were recorded.
        self.assertEqual(self.supervisor.state.get('reviewer')['attempt'], 2)
        self.assertEqual(self.supervisor.state.candidates(), [])

    def test_rollout_promotes_report_to_pilot_then_full(self):
        record = self.launched([managed_issue(1)])
        data = report(self.clone, record['run_id'], 'sha-aaa', candidates=[candidate()])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest(issues=[managed_issue(1)])
        self.assertEqual(self.supervisor.review_stage(), 'pilot')
        fed = self.supervisor.planner_review_input()
        self.assertEqual([c['key'] for c in fed['candidates']], ['deepen-executor'])
        self.supervisor.state.disposition_candidate('deepen-executor', 'accepted', 'ok', issue=10)
        self.supervisor.state.map_improvement('deepen-executor', 10)
        self.supervisor.state.reserve(10, 'worker-01', 'g')
        self.supervisor.state.complete(10)
        self.supervisor.state.set('review:last_slot', 0)
        self.github.sha = 'sha-bbb'
        record = self.launched()
        data = report(self.clone, record['run_id'], 'sha-bbb', assessments=[
            {'key': 'deepen-executor', 'outcome': 'confirmed',
             'observed': 'callers measurably simpler'}])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        self.assertEqual(self.supervisor.review_stage(), 'full')

    def test_rejected_pilot_assessment_rolls_back_to_report(self):
        self.supervisor.state.set('review:stage', 'pilot')
        record = self.launched()
        self.supervisor.state.record_candidates('earlier', [candidate()])
        self.supervisor.state.disposition_candidate('deepen-executor', 'accepted', 'ok', issue=10)
        self.supervisor.state.map_improvement('deepen-executor', 10)
        self.supervisor.state.reserve(10, 'worker-01', 'g')
        self.supervisor.state.complete(10)
        data = report(self.clone, record['run_id'], 'sha-aaa', assessments=[
            {'key': 'deepen-executor', 'outcome': 'rejected', 'observed': 'callers unchanged'}])
        (Path(record['report_dir']) / 'report.json').write_text(json.dumps(data))
        self.ingest()
        self.assertEqual(self.supervisor.review_stage(), 'report')
        self.assertIsNotNone(self.supervisor.state.get('review:rollout_failure'))

    def test_reviewer_slot_counts_against_dispatch_capacity(self):
        self.supervisor.state.set('reviewer', {'process': {'pid': 1}, 'run_id': 'r1',
                                             'slot': 1, 'attempt': 1})
        self.github.items = [managed_issue(n, group=f'g{n}') for n in range(1, 8)]
        self.supervisor.dispatch(self.github.items)
        self.assertEqual(len(self.supervisor.state.jobs(('working',))), 4)
        self.assertEqual(self.supervisor.slots_used(), 5)

    def test_blocked_prerequisite_releases_improvement_slot(self):
        a1 = managed_issue(10, improvement='imp-a', group='ga')
        a2 = managed_issue(11, improvement='imp-a', group='ga2', dependencies=[10])
        self.github.items = [a1, a2]
        for n in (10, 11):
            self.supervisor.state.map_improvement('imp-a', n)
        self.supervisor.dispatch(self.github.items)
        self.assertEqual(self.supervisor.state.get('review:active_improvement'), 'imp-a')
        self.assertIsNone(self.supervisor.state.job(11))
        self.supervisor.state.update_job(10, status='blocked')
        b = managed_issue(12, improvement='imp-b', group='gb')
        self.github.items.append(b)
        self.supervisor.state.map_improvement('imp-b', 12)
        self.supervisor.dispatch(self.github.items)
        self.assertIsNone(self.supervisor.state.job(11))
        self.assertEqual(self.supervisor.state.job(12)['status'], 'working')
        self.assertEqual(self.supervisor.state.get('review:active_improvement'), 'imp-b')

    def test_improvement_waits_on_live_prerequisite(self):
        dep = managed_issue(9, group='gd')
        a1 = managed_issue(10, improvement='imp-a', group='ga')
        a2 = managed_issue(11, improvement='imp-a', group='ga2', dependencies=[9])
        self.github.items = [dep, a1, a2]
        for n in (10, 11):
            self.supervisor.state.map_improvement('imp-a', n)
        self.supervisor.dispatch(self.github.items)
        self.supervisor.state.complete(10)
        self.supervisor.dispatch(self.github.items)
        self.assertEqual(self.supervisor.state.get('review:active_improvement'), 'imp-a')
        self.assertIsNone(self.supervisor.state.job(11))
        b = managed_issue(12, improvement='imp-b', group='gb')
        self.github.items.append(b)
        self.supervisor.dispatch(self.github.items)
        self.assertIsNone(self.supervisor.state.job(12))

    def test_unresolved_accepted_returns_to_planner(self):
        self.supervisor.state.set('review:stage', 'full')
        self.supervisor.state.record_candidates('r0', [candidate()])
        self.supervisor.state.disposition_candidate('deepen-executor', 'accepted', 'ok', issue=10)
        self.supervisor.state.map_improvement('deepen-executor', 10)
        fed = self.supervisor.planner_review_input()
        self.assertEqual([c['key'] for c in fed['accepted']], ['deepen-executor'])
        self.assertEqual(fed['accepted'][0]['issues'], [10])
        self.supervisor.state.reserve(10, 'worker-01', 'g')
        self.supervisor.state.complete(10)
        self.assertIsNone(self.supervisor.planner_review_input())

    def test_deferred_accepted_candidate_retires(self):
        self.supervisor.state.set('review:stage', 'full')
        self.supervisor.state.record_candidates('r0', [candidate()])
        self.supervisor.state.disposition_candidate('deepen-executor', 'accepted', 'ok', issue=10)
        self.supervisor.state.map_improvement('deepen-executor', 10)
        proposal = {'issues': [], 'dispositions': [
            {'key': 'deepen-executor', 'decision': 'defer',
             'reason': 'prerequisite blocked permanently',
             'revisit': 'after the blocker is cleared'}]}
        self.supervisor.state.set('pending_proposal', proposal)
        self.supervisor.planner([], [])
        self.assertEqual(self.supervisor.state.candidate('deepen-executor')['disposition'], 'deferred')
        fed = self.supervisor.planner_review_input()
        self.assertEqual(fed['accepted'], [])
        self.assertEqual([c['key'] for c in fed['suppressed']], ['deepen-executor'])

    def test_legacy_pending_proposal_list_is_normalized(self):
        item = dict(key='legacy-task', title='Legacy', scope='s', acceptance='a',
                    tests='t', dependencies=[], priority=2, milestone='m', group='core')
        self.supervisor.state.set('pending_proposal', [item])
        self.supervisor.planner([], [])
        self.assertIsNone(self.supervisor.state.get('pending_proposal'))
        self.assertEqual(len(self.github.items), 1)
        self.supervisor.state.set('pending_proposal', {'unexpected': 1})
        self.supervisor.planner([], [])
        self.assertIsNone(self.supervisor.state.get('pending_proposal'))
        self.assertIn('malformed', self.supervisor.state.get('last_error'))


class ManifestAndConfigTests(unittest.TestCase):
    def test_vendored_manifest_matches_repo_resources(self):
        root = Path(__file__).resolve().parents[1]
        hashes = review.verify_resources(root)
        self.assertTrue(all(review.SHA.match(v) for v in hashes.values()))

    def test_settings_validation(self):
        cfg = review.settings({'review': {'enabled': True}})
        self.assertEqual((cfg['hour'], cfg['minute']), (3, 0))
        for bad in ({'review': {'mode': 'x'}}, {'review': {'time': '25:00'}},
                    {'review': {'time': 'noon'}}, {'review': {'max_candidates': 0}},
                    {'review': {'enabled': 'yes'}}):
            with self.assertRaises(ValueError):
                review.settings(bad)


if __name__ == '__main__':
    unittest.main()
