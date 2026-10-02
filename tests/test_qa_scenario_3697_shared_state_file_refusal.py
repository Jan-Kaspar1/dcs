"""The 3697_shared_state_file_refusal leg's scenario unit coverage — the
feed fake, the stubbed deployment-doctoring levers, and the TestCase
class for scenario_shared_state_file_refusal, per the module-per-leg
test convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import shutil

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'SharedStateFileTests.test_registered_in_scenarios',
    'SharedStateFileTests.test_clean_rig_passes_and_validates',
    'SharedStateFileTests.test_two_passes_share_one_digest',
    'SharedStateFileTests.test_two_runs_produce_identical_records',
    'SharedStateFileTests.test_scenario_ctx_carries_the_alias_seam',
    'SharedStateFileTests.test_restore_relaunch_mounts_its_own_directory',
    'SharedStateFileTests.test_alias_arguments_are_validated',
    'SharedStateFileTests.test_missing_relaunch_lever_is_inconclusive',
    'SharedStateFileTests.test_missing_state_probe_is_inconclusive',
    'SharedStateFileTests.test_missing_state_files_is_inconclusive',
    'SharedStateFileTests.test_single_endpoint_is_inconclusive',
    'SharedStateFileTests.test_unreachable_rig_is_inconclusive',
    'SharedStateFileTests.test_unsettled_pair_is_inconclusive',
    'SharedStateFileTests.test_no_active_is_failed',
    'SharedStateFileTests.test_alias_accepted_fails',
    'SharedStateFileTests.test_alias_exited_zero_fails',
    'SharedStateFileTests.test_alias_absent_fails',
    'SharedStateFileTests.test_unnamed_refusal_fails',
    'SharedStateFileTests.test_surviving_monitor_fails',
    'SharedStateFileTests.test_shared_collision_fails_by_name',
    'SharedStateFileTests.test_alias_write_fails',
    'SharedStateFileTests.test_sink_stalled_fails',
    'SharedStateFileTests.test_control_unread_fails',
    'SharedStateFileTests.test_declared_paths_aliased_fails',
    'SharedStateFileTests.test_owner_moved_fails',
    'SharedStateFileTests.test_owner_frozen_fails',
    'SharedStateFileTests.test_owner_silent_fails',
    'SharedStateFileTests.test_restore_stalls_fails',
    'SharedStateFileTests.test_raising_probe_restores_the_member',
    'SharedStateFileTests.test_swapped_launch_layout_passes',
    'SharedStateFileTests.test_silenced_judges_report_unchecked',
    'SharedStateFileTests.test_self_check_catches_planted_negatives',
})


class SharedFeed:
    """A stubbed pair for the shared-state-file leg: ctrl-a owns the
    field, ctrl-b tracks it, every monitor call is one completed scan,
    and each member's runner-owned checkpoint carries its own run's
    generation and the tick it persisted through. The
    relaunch_controller lever stands the harness's deployment
    doctoring: `share_state_with` names the peer whose persistence
    directory the relaunched member's --state-file resolves into, so
    the pair's identical declarations meet on one backing checkpoint.
    On a build carrying #1341's single-writer guard the relaunched
    member refuses at startup — its monitor stops answering and the
    controller_state probe reads a nonzero exit with the refusal naming
    the shared file and its .lock sidecar; where the guard is staged
    absent the launch keeps serving and each capture replaces the shared
    checkpoint's run identity, the silent state destruction the guard
    exists to refuse. The correctly-pathed relaunch restores the member.
    Doctor flags stage each named defect."""

    PEERS = {'active': 'a', 'standby': 'b'}
    GENERATIONS = {'active': 41, 'standby': 42}
    # The refusal #1341's fix produces: the writer-lock conflict on the
    # .lock sidecar the checkpoint's write-then-rename never replaces.
    REFUSAL = (
        'error: cannot lock state file /var/lib/dcs-run/state.json.lock: '
        'a live process already holds its writer lock — two writers on '
        'one --state-file overwrite each other\'s run\'s tick domain, '
        'receipts, and component state, and a restart resumes whichever '
        'wrote last; give each process its own state file (the lock is '
        'the sibling /var/lib/dcs-run/state.json.lock and its holder\'s '
        'identity is `fuser`/`lsof` on that path)\n')
    UNNAMED = 'error: the persistence configuration is invalid\n'

    def __init__(self, root):
        self.root = Path(root)
        self.tick = 0
        self.owner = 'active'
        self.tracking = True
        self.up = {'active': True, 'standby': True}
        self.calls = []
        self.aliased = None
        self.frozen_tick = None
        # Doctor flags for the named-failure and inconclusive cases.
        self.alias_accepted = False   # the guard is absent: the member
                                      # serves and both peers write one
                                      # checkpoint
        self.alias_unnamed = False    # the refusal names no path
        self.alias_exited = False     # the aliased launch exits 0
        self.alias_absent = False     # no container left to read
        self.alias_writes = False     # the aliased member captures into
                                      # its own file besides
        self.shared_collision = False  # the shared checkpoint carries a
                                       # foreign run's identity while
                                       # the refusal stands
        self.monitor_survives = False  # the refused launch's monitor
                                       # keeps answering
        self.sink_stalled = False     # the owner's checkpoint stops
                                       # advancing
        self.owner_frozen = False     # the owner serves a stopped tick
        self.owner_silent = False     # the owner stops answering
        self.owner_moves = False      # the owner leaves the field
        self.no_active = False        # neither peer holds the field
        self.freeze = False           # no member scans
        self.restore_stalls = False   # the restore never serves again
        self.state_unread = False     # the pair's own checkpoints carry
                                       # no run's identity
        self.paths_aliased = False    # both members declare one path
        for name in self.PEERS:
            self._write(name)

    def swap(self):
        """The swapped launch layout: the standby member owns the field
        and the launched active tracks it — the leg measures the settled
        roles, not a fixed letter."""
        self.owner = 'standby'

    def other(self, name):
        return 'standby' if name == 'active' else 'active'

    def _path(self, name):
        return self.root / 'controllers' / self.PEERS[name] / 'state.json'

    def state_files(self):
        """The ctx['state_files'] mapping: each member's own
        runner-owned checkpoint, aliased onto one path where the doctor
        stages the distinct-path control's failure."""
        paths = {name: str(self._path(name)) for name in self.PEERS}
        if self.paths_aliased:
            paths[self.other(self.owner)] = paths[self.owner]
        return paths

    def _write_path(self, path, generation):
        """One run's checkpoint document at `path` — its tick-domain
        generation and the tick it persisted through, or a document no
        run's identity can be read from where the doctor stages it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if self.state_unread:
            path.write_text('a document that is not a checkpoint\n')
            return
        path.write_text(json.dumps({'format_version': 1,
                                    'generation': generation,
                                    'tick': self.tick}))

    def _write(self, name):
        self._write_path(self._path(name), self.GENERATIONS[name])

    def _scan(self):
        """One completed scan: the field owner's checkpoint carries the
        tick, and where the aliased member is capturing the pair's runs
        meet on that one file."""
        if self.freeze:
            return
        self.tick += 1
        if self.sink_stalled:
            return
        if self.owner_moves and self.aliased is not None \
                and self.tick % 2 == 0:
            self.owner = self.other(self.owner)
        self._write(self.owner)
        if self.aliased is None:
            return
        if self.alias_accepted or self.shared_collision:
            self._write_path(self._path(self.owner),
                             self.GENERATIONS[self.aliased])
        if self.alias_writes:
            self._write(self.aliased)

    def _role(self, name):
        if self.no_active:
            return {'role': 'demoting', 'tick': self.tick,
                    'sync': {'unsynchronized': {}}}
        tick = self.tick
        if self.owner_frozen and name == self.owner:
            self.frozen_tick = tick if self.frozen_tick is None \
                else self.frozen_tick
            tick = self.frozen_tick
        if name == self.owner:
            return {'role': 'active', 'tick': tick}
        sync = {'tracking': {'aligned': tick}} if self.tracking \
            else {'unsynchronized': {}}
        return {'role': 'standby', 'tick': tick, 'sync': sync}

    def http_json(self, method, url, body=None, timeout=10):
        """The measurement channel — replaces scenarios.http_json."""
        host = url.split('/')[2]
        name = 'active' if host == 'ctrl-a:1' else 'standby'
        self._scan()
        alive = self.up[name] or (self.monitor_survives and name in
                                  (self.aliased, self.other(self.aliased)))
        if not alive or (self.owner_silent and name == self.owner):
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        if (method, path.partition('?')[0]) == ('GET', '/role'):
            return 200, self._role(name)
        raise AssertionError('unexpected request %s %s' % (method, url))

    def relaunch_controller(self, name, track=None, share_state_with=None):
        """The ctx['relaunch_controller'] lever's stub: the deployment
        doctoring the runner performs, recorded per call. A
        shared_state_with launch resolves into the named peer's
        directory — refused by name where the single-writer guard
        stands, serving and overwriting the shared checkpoint where it
        does not — while the correctly-pathed relaunch restores the
        member."""
        self.calls.append({'name': name, 'track': track,
                           'shared_with': share_state_with})
        self._scan()
        if share_state_with is not None:
            self.aliased = name
            self.up[name] = self.alias_accepted
        else:
            self.aliased = None
            self.up[name] = not self.restore_stalls

    def controller_state(self, name):
        """The ctx['controller_state'] probe's stub: the member
        container's Running/ExitCode verdict with the log tail the
        refusal is named in."""
        container = 'dcs-hw-qa-1-' + self.PEERS[name]
        if self.up[name]:
            return {'container': container, 'running': True, 'exit': None,
                    'logs': '', 'absent': False}
        if self.alias_absent:
            return {'container': container, 'running': False, 'exit': None,
                    'logs': '', 'absent': True}
        return {'container': container, 'running': False,
                'exit': 0 if self.alias_exited else 1,
                'logs': self.UNNAMED if self.alias_unnamed
                else self.REFUSAL,
                'absent': False}


class SharedStateFileTests(unittest.TestCase):
    """scenario_shared_state_file_refusal against the stubbed rig: the
    fake deployment doctoring answers the aliased launch's process
    verdict while the pair serves its settled roles — until a doctor
    flag stages a named defect or an inconclusive rig."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.run_dir.mkdir(parents=True)
        self.feed = SharedFeed(self.run_dir)
        self.events = []

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        base = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'relaunch_controller': feed.relaunch_controller,
                'controller_state': feed.controller_state,
                'state_files': feed.state_files(),
                'evidence_dir': str(self.evidence)}
        base.update(extra)
        return base

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        patches = {'POLL_INTERVAL': 0.001, 'SHARED_SETTLE': 3.0,
                   'SHARED_POLL': 0.01, 'SHARED_WATCH': 0.01,
                   'SHARED_WINDOW': 0.2, 'SHARED_VERDICT': 0.2}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_shared_state_file_refusal(
                ctx or self._ctx(feed))

    def _pass_payload(self, number=1):
        return json.loads(
            (self.evidence
             / ('shared-state-file-pass-%d.json' % number)).read_text())

    def _runner_ctx(self):
        return runner._scenario_ctx(
            dict(runner.DEFAULT_CONFIG),
            {'run_id': 'qa-1', 'attempted_sha': '0' * 40}, Path('src'),
            self.run_dir, 'evidence', 0,
            lambda event, detail=None: self.events.append(event))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_shared_state_file_refusal, order)
        self.assertLess(
            order.index(scenarios.scenario_persistence_path_alias_refusal),
            order.index(scenarios.scenario_shared_state_file_refusal))
        self.assertLess(
            order.index(scenarios.scenario_shared_state_file_refusal),
            order.index(scenarios.scenario_unclaimed_rearm))
        self.assertIs(
            verify.case_function('shared-state-file-refusal'),
            scenarios.scenario_shared_state_file_refusal)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = {entry['ref'] for entry in record['evidence']}
        self.assertEqual(refs, {
            'evidence/shared-state-file-pass-1.json',
            'evidence/shared-state-file-pass-2.json'})
        for entry in record['evidence']:
            self.assertTrue(
                (self.evidence.parent / entry['ref']).exists(), entry)
        self.assertEqual(self._pass_payload()['digest'], {
            'distinct': 'settled', 'refusal': 'down-named',
            'monitor': 'silent', 'checkpoint': 'single-writer',
            'owner': 'undisturbed', 'pair': 'restored'})
        # The staging and the restore, once per pass: the non-owner
        # relaunched onto the owner's directory, then back onto its own.
        self.assertEqual(self.feed.calls, [
            {'name': 'standby', 'track': None, 'shared_with': 'active'},
            {'name': 'standby', 'track': None, 'shared_with': None},
            {'name': 'standby', 'track': None, 'shared_with': 'active'},
            {'name': 'standby', 'track': None, 'shared_with': None}])
        payload = self._pass_payload()
        # The pair's own checkpoints: each carries its own run, and the
        # aliased member captured nothing anywhere.
        self.assertEqual(payload['baseline']['owner']['generation'], 41)
        self.assertEqual(payload['baseline']['member']['generation'], 42)
        self.assertFalse(payload['alias_write'])
        self.assertEqual(payload['monitor'], 'silent')
        self.assertEqual(payload['refusal']['exit'], 1)
        self.assertTrue(payload['refusal']['named'])
        self.assertIn('state.json.lock', payload['refusal']['text'])
        # The launch layout stands restored: one active plus a tracking
        # standby, neither left carrying the aliased mount.
        self.assertEqual(self.feed.owner, 'active')
        self.assertEqual(self.feed.up, {'active': True, 'standby': True})
        self.assertTrue(self.feed.tracking)

    def test_two_passes_share_one_digest(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self._pass_payload(1)['digest'],
                         self._pass_payload(2)['digest'])

    def test_two_runs_produce_identical_records(self):
        # The deterministic-rerun contract: two scenario runs over
        # fresh feeds record identical case records.
        runs = []
        for _index in range(2):
            for stale in self.evidence.iterdir():
                stale.unlink()
            shutil.rmtree(self.run_dir / 'controllers', ignore_errors=True)
            runs.append(self.run_scenario(feed=SharedFeed(self.run_dir)))
        self.assertEqual(runs[0]['outcome'], 'passed', runs[0])
        self.assertEqual(runs[0], runs[1])

    def test_scenario_ctx_carries_the_alias_seam(self):
        calls = []
        ctx = self._runner_ctx()

        def fake_docker(*args, **kwargs):
            calls.append(args)
            if args[0] == 'inspect':
                return subprocess.CompletedProcess(
                    args, 0, stdout='false 1\n', stderr='')
            if args[0] == 'logs':
                return subprocess.CompletedProcess(
                    args, 0, stdout='', stderr=SharedFeed.REFUSAL)
            return subprocess.CompletedProcess(args, 0, stdout='',
                                               stderr='')

        self.assertTrue(callable(ctx['relaunch_controller']))
        self.assertTrue(callable(ctx['controller_state']))
        with patch.object(runner, 'docker', fake_docker):
            ctx['relaunch_controller']('standby', share_state_with='active')
            state = ctx['controller_state']('standby')
        # The refused launch's exit evidence: no live process, a nonzero
        # exit, and the refusal naming the shared checkpoint.
        self.assertFalse(state['running'])
        self.assertEqual(state['exit'], 1)
        self.assertFalse(state['absent'])
        self.assertIn('state.json.lock', state['logs'])
        self.assertIn('writer lock', state['logs'])
        argv = next(argv for argv in calls if argv[0] == 'run')
        mounts = [argv[index + 1] for index, arg in enumerate(argv)
                  if arg == '-v']
        # Only the checkpoint aliases: the peer's directory answers the
        # declared persistence path, the member's own keeps serving only
        # its append sinks.
        self.assertIn(str(self.run_dir / 'controllers' / 'a')
                      + ':' + runner.CONTAINER_RUN_DIR, mounts)
        self.assertIn(str(self.run_dir / 'controllers' / 'b')
                      + ':' + runner.CONTAINER_OWN_RUN_DIR, mounts)
        self.assertEqual(argv[argv.index('--state-file') + 1],
                         runner.CONTAINER_STATE_FILE)
        self.assertEqual(argv[argv.index('--journal-file') + 1],
                         runner.CONTAINER_OWN_RUN_DIR + '/journal.jsonl')
        self.assertEqual(argv[argv.index('--history-file') + 1],
                         runner.CONTAINER_OWN_RUN_DIR + '/history.jsonl')
        inspect = next(args for args in calls if args[0] == 'inspect')
        self.assertIn('dcs-hw-qa-1-b', inspect)
        self.assertEqual(self.events, ['controller-relaunch',
                                       'controller-relaunched'])

    def test_restore_relaunch_mounts_its_own_directory(self):
        calls = []
        ctx = self._runner_ctx()

        def fake_docker(*args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, stdout='',
                                              stderr='')

        with patch.object(runner, 'docker', fake_docker):
            ctx['relaunch_controller']('standby', share_state_with='active')
            ctx['relaunch_controller']('standby')
        argv = [argv for argv in calls if argv[0] == 'run'][-1]
        mounts = [argv[index + 1] for index, arg in enumerate(argv)
                  if arg == '-v']
        self.assertIn(str(self.run_dir / 'controllers' / 'b')
                      + ':' + runner.CONTAINER_RUN_DIR, mounts)
        self.assertNotIn(runner.CONTAINER_OWN_RUN_DIR, ' '.join(mounts))
        self.assertEqual(argv[argv.index('--journal-file') + 1],
                         runner.CONTAINER_JOURNAL_FILE)
        self.assertEqual(argv[argv.index('--history-file') + 1],
                         runner.CONTAINER_HISTORY_FILE)

    def test_alias_arguments_are_validated(self):
        ctx = self._runner_ctx()
        with patch.object(
                runner, 'docker',
                lambda *args, **kwargs: subprocess.CompletedProcess(
                    args, 0, stdout='', stderr='')):
            with self.assertRaises(RuntimeError) as caught:
                ctx['relaunch_controller']('standby', share_state_with='b')
            self.assertIn('share_state_with', str(caught.exception))
            with self.assertRaises(RuntimeError) as caught:
                ctx['relaunch_controller']('standby',
                                           share_state_with='standby')
            self.assertIn('itself', str(caught.exception))
            with self.assertRaises(RuntimeError) as caught:
                ctx['relaunch_controller']('peer')
            self.assertIn('endpoint key', str(caught.exception))

    def test_missing_relaunch_lever_is_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(relaunch_controller=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('relaunch_controller', record['detail'])

    def test_missing_state_probe_is_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(controller_state=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('controller_state', record['detail'])

    def test_missing_state_files_is_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(state_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('--state-file path', record['detail'])

    def test_single_endpoint_is_inconclusive(self):
        record = self.run_scenario(ctx=self._ctx(standby=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('only one endpoint', record['detail'])

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.up = {'active': False, 'standby': False}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.tracking = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])

    def test_no_active_is_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-nondeterministic',
                      record['detail'])
        self.assertIn('role=active', record['detail'])

    def test_alias_accepted_fails(self):
        # The pre-#1341 shape: no single-writer guard, so both peers
        # persist into one checkpoint and each replaces the other's
        # run's state on every capture.
        self.feed.alias_accepted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-accepted', record['detail'])
        self.assertIn('both peers persisted', record['detail'])
        payload = self._pass_payload()
        self.assertEqual(payload['digest']['refusal'], 'running')
        self.assertEqual(payload['digest']['checkpoint'], 'overwritten')

    def test_alias_exited_zero_fails(self):
        self.feed.alias_exited = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-accepted', record['detail'])
        self.assertIn('exited 0', record['detail'])

    def test_alias_absent_fails(self):
        self.feed.alias_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-nondeterministic',
                      record['detail'])
        self.assertIn('absent', record['detail'])

    def test_unnamed_refusal_fails(self):
        self.feed.alias_unnamed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-accepted', record['detail'])
        self.assertIn('without naming', record['detail'])
        self.assertEqual(self._pass_payload()['digest']['refusal'],
                         'down-unnamed')

    def test_surviving_monitor_fails(self):
        # The container is down, yet its published monitor keeps
        # answering — the aliased launch never refused the shared file.
        self.feed.monitor_survives = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-accepted', record['detail'])
        self.assertIn('monitor kept answering', record['detail'])
        self.assertEqual(self._pass_payload()['digest']['monitor'],
                         'answered')

    def test_shared_collision_fails_by_name(self):
        # The issue's doctored negative: the refusal lands by name while
        # both peers' runs still meet on the one checkpoint file.
        self.feed.shared_collision = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-accepted', record['detail'])
        self.assertIn('both peers', record['detail'])
        digest = self._pass_payload()['digest']
        self.assertEqual(digest['refusal'], 'down-named')
        self.assertEqual(digest['checkpoint'], 'overwritten')

    def test_alias_write_fails(self):
        # The aliased member captured at all — a second live writer,
        # wherever the capture landed.
        self.feed.alias_writes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-accepted', record['detail'])
        self.assertIn('both peers', record['detail'])
        self.assertEqual(self._pass_payload()['digest']['checkpoint'],
                         'alias-write')

    def test_sink_stalled_fails(self):
        # The owner's shared checkpoint stops advancing inside the
        # staged window, so the leg cannot prove one writer held it.
        self.feed.sink_stalled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-nondeterministic',
                      record['detail'])
        self.assertIn('one-writer evidence', record['detail'])
        self.assertEqual(self._pass_payload()['digest']['checkpoint'],
                         'stalled')

    def test_control_unread_fails(self):
        self.feed.state_unread = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-nondeterministic',
                      record['detail'])
        self.assertIn('checkpoints could not be read', record['detail'])

    def test_declared_paths_aliased_fails(self):
        self.feed.paths_aliased = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.feed.paths_aliased = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-accepted', record['detail'])
        self.assertIn('one --state-file path', record['detail'])

    def test_owner_moved_fails(self):
        # The field owner changes hands inside the staged window — the
        # alias was staged behind a peer that was not standing still.
        self.feed.owner_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not undisturbed', record['detail'])
        self.assertEqual(self._pass_payload()['digest']['owner'],
                         'disturbed')

    def test_owner_frozen_fails(self):
        # The owner keeps serving and answering but its served tick
        # stops advancing — no scan evidence across the staged window.
        self.feed.owner_frozen = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-nondeterministic',
                      record['detail'])
        self.assertIn('not undisturbed', record['detail'])

    def test_owner_silent_fails(self):
        self.feed.owner_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-nondeterministic',
                      record['detail'])

    def test_restore_stalls_fails(self):
        self.feed.restore_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-nondeterministic',
                      record['detail'])
        self.assertIn('did not restore', record['detail'])

    def test_raising_probe_restores_the_member(self):
        # A lever that raises once the alias is staged must leave the
        # member recreated onto its own persistence path — the launch
        # posture the legs behind this one inherit.
        def raising(name):
            raise RuntimeError('docker inspect never answered')

        record = self.run_scenario(
            ctx=self._ctx(controller_state=raising))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never completed', record['detail'])
        # The staging and the hygiene recreate, and nothing is left
        # carrying the aliased mount.
        self.assertEqual(self.feed.calls,
                         [{'name': 'standby', 'track': None,
                           'shared_with': 'active'},
                          {'name': 'standby', 'track': None,
                           'shared_with': None}])
        self.assertTrue(self.feed.up['standby'])
        payload = self._pass_payload()
        self.assertTrue(payload['alias']['staged'])

    def test_swapped_launch_layout_passes(self):
        self.feed.swap()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual([call['name'] for call in self.feed.calls],
                         ['active', 'active', 'active', 'active'])
        self.assertEqual(self.feed.calls[0]['shared_with'], 'standby')
        self.assertEqual(self._pass_payload()['subject'],
                         {'owner': 'standby', 'member': 'active'})

    def test_silenced_judges_report_unchecked(self):
        with patch.object(scenarios, '_judge_shared',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shared-state-file-unchecked', record['detail'])

    def test_self_check_catches_planted_negatives(self):
        # The leg's own auditors must name every planted negative — the
        # guard behind the unchecked diagnostic.
        self.assertEqual(scenarios._shared_self_check(), [])
        with patch.object(scenarios, '_shared_named',
                          lambda state, shared_file: False):
            self.assertEqual(scenarios._shared_self_check(), [])
        with patch.object(scenarios, '_shared_checkpoint',
                          lambda record: 'single-writer'):
            self.assertNotEqual(scenarios._shared_self_check(), [])
        with patch.object(scenarios, '_shared_owner',
                          lambda record: 'undisturbed'):
            self.assertNotEqual(scenarios._shared_self_check(), [])


if __name__ == '__main__':
    unittest.main()