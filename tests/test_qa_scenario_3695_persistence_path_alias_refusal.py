"""The 3695_persistence_path_alias_refusal leg's scenario unit
coverage — the feed fake, the stubbed doctored-launch lever, and the
TestCase class for scenario_persistence_path_alias_refusal, per the
module-per-leg test convention (#940). The shared fakes and helpers
live in tests/qa_scenario_support.py.
"""
import shutil

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class AliasFeed:
    """A stubbed pair for the persistence-alias scenario: ctrl-a owns
    the field, ctrl-b tracks it; every monitor call is one completed
    scan. The admit_persistence lever stands the harness's
    doctored-launch staging: each aliased probe answers the named
    exit-2 parse refusal — both conflicting flags and the shared path
    in the text — and the distinct control answers exit 0 with the
    paced budget's final snapshot and its three sinks' files staged
    host-side. Doctor flags stage each named defect and inconclusive
    rig the leg's outcomes cover."""

    FLAGS = {'state_file': '--state-file',
             'journal_file': '--journal-file',
             'history_file': '--history-file'}

    def __init__(self, root):
        self.root = Path(root)
        self.tick = 0
        self.a_role = 'active'
        self.b_role = 'standby'
        self.a_tracking = False
        self.b_tracking = True
        self.calls = []
        # Doctor flags for the named-failure and inconclusive cases.
        self.pair_down = False       # the rig is unreachable
        self.predates = False        # the revision predates the
                                     # contract — aliases run, the
                                     # append-append pair meets the
                                     # writer-lock refusal
        self.lever_raises = False    # the lever's staging fails
        self.accepted = set()        # probes whose alias is accepted
        self.unnamed = set()         # probes refused without naming
        self.unread = set()          # probes returning no verdict
        self.lock_refusal = False    # journal/history fails closed on
                                     # the writer lock while the
                                     # contract is otherwise present
        self.distinct_refused = False  # the control launch refused
        self.distinct_files = True   # the staged sinks land
        self.freeze = False          # the pair stops scanning
        self.move_roles = False      # the pair moves mid-probe

    def swap(self):
        """The swapped launch layout: ctrl-b owns the field, ctrl-a
        tracks it — the undisturbed audit owes the settled roles, not
        a fixed letter."""
        self.a_role, self.b_role = 'standby', 'active'
        self.a_tracking, self.b_tracking = True, False

    def _scan(self):
        if not self.freeze:
            self.tick += 1

    def _role(self, peer):
        role = self.a_role if peer == 'a' else self.b_role
        tracking = self.a_tracking if peer == 'a' else self.b_tracking
        sync = {'tracking': {'aligned': self.tick}} \
            if role == 'standby' and tracking \
            else {'unsynchronized': {}}
        return {'role': role, 'tick': self.tick, 'sync': sync}

    def http_json(self, method, url, body=None, timeout=10):
        """The measurement channel — replaces scenarios.http_json."""
        if self.pair_down:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        peer = 'a' if host == 'ctrl-a:1' else 'b'
        self._scan()
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))

    def _alias_pair(self, paths):
        """The (flag pair, shared file) `paths` aliases, or
        (None, None) on the distinct control."""
        by_name = {}
        for key, filename in paths.items():
            by_name.setdefault(filename, []).append(key)
        for filename, keys in by_name.items():
            if len(keys) > 1:
                return keys, filename
        return None, None

    def _refusal(self, name, paths):
        """The named startup refusal the contract's parse gate
        answers: exit 2 carrying both conflicting flags and the
        shared in-container path."""
        keys, filename = self._alias_pair(paths)
        return {'name': name, 'paths': dict(paths), 'exit': 2,
                'stdout': '',
                'stderr': 'error: ' + self.FLAGS[keys[0]]
                          + ' and ' + self.FLAGS[keys[1]]
                          + ' both name /var/lib/dcs-run/'
                          + filename + ': the persistence files '
                          'must be distinct paths\n'}

    def _accepted(self, name, paths):
        """The admitted verdict: the launch paced its --ticks budget
        to exit 0 with the final snapshot on stdout — the aliased
        pair's append stream orphaned behind the checkpoint's
        rename."""
        return {'name': name, 'paths': dict(paths), 'exit': 0,
                'stdout': json.dumps({'tick': 5, 'points': []}),
                'stderr': ''}

    def _distinct(self, name, paths):
        """The distinct control's verdict — exit 0, the final
        snapshot, and each declared sink's file staged under the
        probe's scratch dir with its own format: a JSON checkpoint,
        two run_boundary-led append records."""
        directory = self.root / 'persistence-probes' / name
        directory.mkdir(parents=True, exist_ok=True)
        if self.distinct_files:
            (directory / paths['state_file']).write_text(
                json.dumps({'tick': 5}))
            for key in ('journal_file', 'history_file'):
                (directory / paths[key]).write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}})
                    + '\n')
        verdict = self._accepted(name, paths)
        verdict['dir'] = str(directory)
        return verdict

    def admit_persistence(self, name, paths):
        """The ctx['admit_persistence'] lever's stub: the probe
        name's '<leg>-<pass>' suffix picks the verdict class; the
        staged doctors override the contract's answers."""
        self.calls.append(name)
        self._scan()
        if self.lever_raises:
            raise RuntimeError('the staging mount never landed')
        leg = name.rsplit('-', 1)[0]
        if self.move_roles and leg == 'distinct':
            self.a_role, self.b_role = 'standby', 'active'
            self.a_tracking = self.b_tracking = False
        if leg in self.unread:
            return {'name': name, 'paths': dict(paths)}
        if leg in self.accepted:
            return self._accepted(name, paths)
        if leg == 'distinct':
            if self.distinct_refused:
                return {'name': name, 'paths': dict(paths),
                        'exit': 1, 'stdout': '',
                        'stderr': 'error: cannot open state file\n'}
            return self._distinct(name, paths)
        if self.predates:
            if leg == 'journal-history':
                return {'name': name, 'paths': dict(paths),
                        'exit': 1, 'stdout': '',
                        'stderr': 'error: history file '
                                  '/var/lib/dcs-run/shared.json: a '
                                  'live process already holds its '
                                  'writer lock\n'}
            return self._accepted(name, paths)
        if leg in self.unnamed:
            return {'name': name, 'paths': dict(paths),
                    'exit': 1, 'stdout': '',
                    'stderr': 'error: the persistence configuration '
                              'is invalid\n'}
        if leg == 'journal-history' and self.lock_refusal:
            return {'name': name, 'paths': dict(paths),
                    'exit': 1, 'stdout': '',
                    'stderr': 'error: history file '
                              '/var/lib/dcs-run/shared.json: a '
                              'live process already holds its '
                              'writer lock\n'}
        return self._refusal(name, paths)


class PersistenceAliasTests(unittest.TestCase):
    """scenario_persistence_path_alias_refusal against the stubbed
    rig: the fake lever answers each doctored launch's verdict while
    the pair serves its settled roles — until a doctor flag stages a
    named defect or an inconclusive rig."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.feed = AliasFeed(self.run_dir)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'admit_persistence': feed.admit_persistence,
                'evidence_dir': str(self.evidence)}
        base.update(extra)
        return base

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        patches = {'POLL_INTERVAL': 0.001, 'PERSIST_SETTLE': 3.0,
                   'PERSIST_POLL': 0.01}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios\
                .scenario_persistence_path_alias_refusal(
                    ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(
            scenarios.scenario_persistence_path_alias_refusal,
            order)
        self.assertLess(
            order.index(scenarios.scenario_failover),
            order.index(
                scenarios.scenario_persistence_path_alias_refusal))
        self.assertLess(
            order.index(
                scenarios.scenario_persistence_path_alias_refusal),
            order.index(scenarios.scenario_dcs_ctl))
        self.assertIs(
            verify.case_function(
                'persistence-path-alias-refusal'),
            scenarios.scenario_persistence_path_alias_refusal)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {
            'evidence/persistence-alias-pass-1.json',
            'evidence/persistence-alias-pass-2.json'})
        for entry in record['evidence']:
            self.assertTrue(
                (self.evidence.parent / entry['ref']).exists(),
                entry)
        payload = json.loads(
            (self.evidence / 'persistence-alias-pass-1.json')
            .read_text())
        self.assertEqual(payload['digest'], {
            'state-history': 'refused-named',
            'state-journal': 'refused-named',
            'journal-history': 'refused-named',
            'distinct': 'scanned', 'pair': 'held'})
        # Each launch was staged once per pass, in the declared
        # probe order.
        self.assertEqual(
            self.feed.calls[:4],
            ['state-history-1', 'state-journal-1',
             'journal-history-1', 'distinct-1'])
        # The launch layout stands untouched.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')
        self.assertTrue(self.feed.b_tracking)

    def test_two_passes_share_one_digest(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = [json.loads(
            (self.evidence / name).read_text())['digest']
            for name in ('persistence-alias-pass-1.json',
                         'persistence-alias-pass-2.json')]
        self.assertEqual(passes[0], passes[1])

    def test_two_runs_produce_identical_records(self):
        # The deterministic-rerun contract: two scenario runs over
        # fresh feeds record identical verdict records.
        runs = []
        for _index in range(2):
            for stale in self.evidence.iterdir():
                stale.unlink()
            shutil.rmtree(self.run_dir / 'persistence-probes',
                          ignore_errors=True)
            runs.append(self.run_scenario(
                feed=AliasFeed(self.run_dir)))
        self.assertEqual(runs[0]['outcome'], 'passed', runs[0])
        self.assertEqual(runs[0], runs[1])

    def test_scenario_ctx_carries_the_lever(self):
        calls = []
        record = {'run_id': 'qa-1', 'attempted_sha': '0' * 40}
        ctx = runner._scenario_ctx(
            dict(runner.DEFAULT_CONFIG), record, Path('src'),
            self.run_dir, 'evidence', 0,
            lambda event, detail=None: self.events.append(event))

        def fake_docker(*args, **kwargs):
            calls.append(args)
            if args[0] == 'run':
                return subprocess.CompletedProcess(
                    args, 2, stdout='',
                    stderr='error: monitor persistence paths must '
                           'be distinct files: --state-file and '
                           '--history-file both name '
                           '/var/lib/dcs-run/shared.json')
            return subprocess.CompletedProcess(
                args, 0, stdout='', stderr='')

        self.assertTrue(callable(ctx['admit_persistence']))
        paths = {'state_file': 'shared.json',
                 'journal_file': 'journal.jsonl',
                 'history_file': 'shared.json'}
        with patch.object(runner, 'docker', fake_docker):
            verdict = ctx['admit_persistence'](
                'state-history-1', paths)
        self.assertEqual(verdict['exit'], 2)
        self.assertIn('--state-file', verdict['stderr'])
        self.assertEqual(verdict['paths'], paths)
        run_argv = next(args for args in calls if args[0] == 'run')
        self.assertIn('--rm', run_argv)
        self.assertIn('--network', run_argv)
        self.assertIn('none', run_argv)
        self.assertIn('--ticks', run_argv)
        self.assertIn('--state-file', run_argv)
        self.assertIn('--history-file', run_argv)
        shared = [
            run_argv[index + 1] for index, arg in enumerate(run_argv)
            if arg in ('--state-file', '--history-file')]
        self.assertEqual(len(set(shared)), 1)
        self.assertTrue(shared[0].startswith(
            runner.CONTAINER_RUN_DIR + '/'))
        self.assertTrue(any(args[0] == 'rm' and '-f' in args
                            for args in calls))
        self.assertEqual(self.events, ['persistence-probe',
                                       'persistence-probed'])
        self.assertTrue(
            (self.run_dir / 'persistence-probes'
             / 'state-history-1').is_dir())

    def test_lever_argv_carries_the_alias(self):
        # The doctored launch shape: the rig's pacing, --listen, and
        # the caller's persistence trio — an alias is two flags
        # naming one mounted path.
        verdict = self.feed.admit_persistence(
            'state-history-9',
            {'state_file': 'shared.json',
             'journal_file': 'journal.jsonl',
             'history_file': 'shared.json'})
        self.assertEqual(verdict['exit'], 2)
        self.assertIn('--state-file', verdict['stderr'])
        self.assertIn('--history-file', verdict['stderr'])
        self.assertIn('shared.json', verdict['stderr'])

    def test_missing_lever_is_inconclusive(self):
        record = self.run_scenario(
            ctx=self._ctx(admit_persistence=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('admit_persistence', record['detail'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.pair_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.b_tracking = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])

    def test_no_active_is_failed(self):
        self.feed.a_role = 'standby'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record['detail'])

    def test_predated_revision_is_inconclusive(self):
        self.feed.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])

    def test_failed_lever_is_inconclusive(self):
        self.feed.lever_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never ran', record['detail'])

    def test_state_history_accepted_fails(self):
        self.feed.accepted.add('state-history')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-accepted',
                      record['detail'])
        self.assertIn('state-history', record['detail'])

    def test_state_journal_accepted_fails(self):
        self.feed.accepted.add('state-journal')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-accepted',
                      record['detail'])
        self.assertIn('state-journal', record['detail'])

    def test_journal_history_accepted_fails(self):
        self.feed.accepted.add('journal-history')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-accepted',
                      record['detail'])
        self.assertIn('journal-history', record['detail'])

    def test_unnamed_refusal_fails(self):
        self.feed.unnamed.add('state-history')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-accepted',
                      record['detail'])
        self.assertIn('naming', record['detail'])

    def test_lock_refusal_named_by_others_fails(self):
        # The contract present on the state aliases while the
        # append-append pair meets only the writer-lock refusal: the
        # fix's named check must refuse it too.
        self.feed.lock_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-accepted',
                      record['detail'])
        self.assertIn('journal-history', record['detail'])

    def test_unread_verdict_fails(self):
        self.feed.unread.add('state-journal')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-nondeterministic',
                      record['detail'])
        self.assertIn('state-journal', record['detail'])

    def test_distinct_refused_fails(self):
        self.feed.distinct_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-nondeterministic',
                      record['detail'])

    def test_distinct_unscanned_fails(self):
        self.feed.distinct_files = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-nondeterministic',
                      record['detail'])

    def test_moved_pair_fails(self):
        self.feed.move_roles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-nondeterministic',
                      record['detail'])
        self.assertIn('moved', record['detail'])

    def test_frozen_owner_fails(self):
        self.feed.freeze = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-nondeterministic',
                      record['detail'])

    def test_swapped_launch_layout_passes(self):
        self.feed.swap()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.b_role, 'active')
        self.assertEqual(self.feed.a_role, 'standby')

    def test_silenced_judges_report_unchecked(self):
        with patch.object(scenarios, '_judge_persist',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persistence-alias-unchecked',
                      record['detail'])


if __name__ == '__main__':
    unittest.main()
