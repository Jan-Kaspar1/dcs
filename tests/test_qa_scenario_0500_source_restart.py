"""The 0500_source_restart leg's scenario unit coverage — the feed fakes
and TestCase classes for scenario_source_restart, split out of the
test_qa_scenarios monolith (#940). The shared fakes and
helpers live in tests/qa_scenario_support.py; EXPECTED_CASES
pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'SourceRestartTests.test_registered_in_scenarios',
    'SourceRestartTests.test_clean_rig_passes_and_validates',
    'SourceRestartTests.test_rewinding_peer_fails',
    'SourceRestartTests.test_spurious_promotion_fails',
    'SourceRestartTests.test_repromoted_peer_fails',
    'SourceRestartTests.test_unjournaled_resync_fails',
    'SourceRestartTests.test_doubled_resync_entry_fails',
    'SourceRestartTests.test_misreported_alignment_fails',
    'SourceRestartTests.test_lapsed_claim_fails',
    'SourceRestartTests.test_kept_alignment_fails_the_demoted_leg',
    'SourceRestartTests.test_warm_resume_is_inconclusive',
    'SourceRestartTests.test_failed_action_is_inconclusive',
    'SourceRestartTests.test_unreturned_monitor_is_inconclusive',
    'SourceRestartTests.test_untracked_peer_is_inconclusive',
    'SourceRestartTests.test_peer_owned_field_is_inconclusive',
    'SourceRestartTests.test_missing_action_is_inconclusive',
    'SourceRestartTests.test_two_runs_produce_identical_evidence',
})


class SourceRestartFeed:
    """A stubbed pair for the source-restart scenario. ctrl-a owns the
    field — each cold restart drops its tick to the resume mark and
    opens a new run on its --journal-file — while ctrl-b tracks its
    checkpoint stream: every ctrl-b request is one completed tracking
    cycle, the pull applying under the never-rewind rule — lagging
    streams adopt at the run tick, regressed ones journal one
    source_restarted — unless a fault flag stages the
    finding's defect. ctrl-b's --journal-file is a real append-only
    file the feed writes; the plant's single-writer claim the fencing
    probes answer is a token the feed's claim/demote/cold-start
    transitions move — a stopped owner's token still fences."""

    def __init__(self, journal_a, journal_b):
        self.a_tick = 400
        self.b_tick = 400
        self.aligned = 400        # ctrl-b's applied stream alignment
        self.a_up = True
        self.a_role = 'active'
        self.b_role = 'standby'
        self.claimed = 'a'        # the plant's standing writer token
        self.journal_a = Path(journal_a)
        self.journal_b = Path(journal_b)
        self.seq_a = 1
        self.seq_b = 1
        self.runs_a = 1
        self.served_b = []        # ctrl-b's served journal ring
        self.history = []         # ctrl-b's served /history rows
        self.restarts = []
        # Fault flags staging the named failures.
        self.returns = True        # False: the cold-restart action fails
        self.serves = True         # False: the monitor never returns
        self.warm = False          # the state file survives the restart
        self.rewinds = False       # the regressed apply drops the run tick
        self.skips_journal = False  # the resync never journals
        self.double_restart = False  # two source_restarted per regression
        self.peer_promotes = False   # ctrl-b reports promoting mid-gap
        self.claim_lapses = False    # the plant drops the claim mid-gap
        self.never_tracks = False    # ctrl-b never reports tracking
        self.wrong_fields = False    # the journaled resync misreports
        self.keeps_alignment = False  # the demotion forgets no alignment
        self.repromotes = False      # the fenced peer re-takes the field
        self._append(self.journal_a,
                     {'run_boundary': {'run': 1, 'tick': 0}})
        self._append(self.journal_b,
                     {'run_boundary': {'run': 1, 'tick': 0}})

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _a_entry(self, event):
        entry = {'seq': self.seq_a, 'tick': self.a_tick,
                 'event': event}
        self._append(self.journal_a, {'entry': entry})
        self.seq_a += 1

    def _b_entry(self, event, tick=None):
        entry = {'seq': self.seq_b,
                 'tick': self.b_tick if tick is None else tick,
                 'event': event}
        self._append(self.journal_b, {'entry': entry})
        self.served_b.append(entry)
        self.seq_b += 1

    def _fence_demote(self):
        # The fenced-write demotion: the claim loss journals once, the
        # role walks demoting -> standby, and the alignment clears —
        # the demoted peer's next pull is the no-prior-alignment form.
        self._b_entry({'field_claim_lost': {'point': 10}})
        self._b_entry({'role_changed': {'from': 'active',
                                        'to': 'demoting'}})
        self._b_entry({'role_changed': {'from': 'demoting',
                                        'to': 'standby'}})
        self.b_role = 'standby'
        if not self.keeps_alignment:
            self.aligned = None
        self.b_tracking = False

    def _b_scan(self):
        """One tracking cycle on ctrl-b: while it owns the field the
        scan writes — a preempted claim fences it into demotion — and
        while it stands by, the pull applies under the never-rewind
        rule before the scan's tick advance."""
        if self.b_role == 'promoting':
            self.b_role = 'active'
        if self.repromotes and self.b_role == 'standby' \
                and self.aligned is None and self.a_up:
            # The defect: a demoted peer asserting the field again.
            self.b_role = 'active'
            self.claimed = 'b'
        if self.b_role == 'active':
            if self.claimed != 'b':
                self._fence_demote()
            self.b_tick += 1
            self._history_row()
            return
        if self.a_up:
            ckpt = self.a_tick
            regressed = ckpt < self.aligned if self.aligned is not None \
                else ckpt < self.b_tick
            if regressed:
                was_aligned = self.aligned
                # The adopt lands at the run tick — the never-rewind
                # hold every lagging apply gets — and the defect form
                # lands at the stream's own tick instead.
                if self.rewinds:
                    self.b_tick = ckpt
                if not self.skips_journal:
                    fields = {'was_aligned': None if self.wrong_fields
                              else was_aligned,
                              'resumed_at': ckpt}
                    self._b_entry({'source_restarted': fields})
                    if self.double_restart:
                        self._b_entry({'source_restarted':
                                       dict(fields)})
                self.aligned = ckpt
                self.b_tracking = True
            else:
                # An ordinary apply lands at the later of the two
                # clocks: the run's tick holds above a lagging stream
                # and rejoins the stream's domain once it catches up.
                self.aligned = ckpt
                self.b_tick = max(self.b_tick, ckpt)
                self.b_tracking = True
        else:
            # The pull missed — the heartbeat degrades until the
            # stream serves again.
            self.b_tracking = False
        self.b_tick += 1
        self._history_row()

    def _history_row(self):
        self.history.append({'seq': len(self.history) + 1,
                             'sample': {'tick': self.b_tick,
                                        'value': {'bool': True},
                                        'quality': 'good'}})

    def _b_sync(self):
        if self.b_role != 'standby':
            return None
        if self.never_tracks:
            return 'unsynchronized'
        if self.b_tracking:
            return {'tracking': {'aligned': self.aligned}}
        if not self.a_up:
            return {'degraded': {'detail': 'checkpoint pull failed'}}
        return 'unsynchronized'

    # The runner-owned action's simulated half: the container cycles,
    # the state file is gone (the real action's unlink is exercised in
    # the test's ctx wiring), the journal opens a new lifetime at the
    # resume tick, and the fresh process claims the field.
    def cold_restart(self, name):
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        assert name == 'active'
        persisted = self.a_tick
        self.a_up = False
        self.down_left = 2
        self.a_tick = persisted if self.warm else 0
        self.a_role = 'active'
        self.claimed = None if self.claim_lapses else 'a'
        self.runs_a += 1
        self._append(self.journal_a, {'run_boundary': {
            'run': self.runs_a, 'tick': self.a_tick}})

    def try_plant(self, ctx, request):
        """The plant-protocol fencing probe: `step` answers fenced
        while any attachment holds the single-writer claim — a stopped
        owner's token still fences."""
        if request.get('op') == 'step':
            if self.claimed is None:
                return {'error': {'kind': 'unclaimed'}}
            return {'error': {'kind': 'fenced',
                              'detail': 'the writer claim is held'}}
        raise AssertionError('unexpected plant request %s' % (request,))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if host == 'ctrl-b:2':
            self._b_scan()
            if (method, route) == ('GET', '/role'):
                role = self.b_role
                if self.peer_promotes and not self.a_up:
                    role = 'promoting'
                return 200, {'role': role, 'tick': self.b_tick,
                             'sync': self._b_sync()}
            if (method, route) == ('GET', '/snapshot'):
                return 200, {'tick': self.b_tick, 'points': [
                    {'point': 10, 'sample': {
                        'value': {'bool': True}, 'quality': 'good'}}]}
            if (method, route) == ('GET', '/history'):
                return 200, [{'point': 10, 'samples': list(
                    self.history)}]
            if (method, route) == ('GET', '/journal'):
                return 200, list(self.served_b)
            if (method, route) == ('POST', '/promote'):
                if not self.b_tracking:
                    return 409, {'refused': {'not_converged': {
                        'sync': self._b_sync()}}}
                self._b_entry({'role_changed': {'from': 'standby',
                                                'to': 'promoting'}})
                self._b_entry({'role_changed': {'from': 'promoting',
                                                'to': 'active'}})
                self.b_role = 'promoting'
                self.claimed = 'b'
                return 200, {'role': 'promoting', 'tick': self.b_tick}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.a_up:
            self.down_left -= 1
            if self.down_left <= 0 and self.serves:
                self.a_up = True
            else:
                raise urllib.error.URLError('connection refused')
        self.a_tick += 1
        if (method, route) == ('GET', '/role'):
            return 200, {'role': self.a_role, 'tick': self.a_tick,
                         'sync': None}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.a_tick, 'points': []}
        if (method, route) == ('POST', '/demote'):
            if self.a_role != 'active':
                return 409, {'refused': 'not_active'}
            self._a_entry({'role_changed': {'from': 'active',
                                            'to': 'demoting'}})
            self._a_entry({'role_changed': {'from': 'demoting',
                                            'to': 'standby'}})
            self.a_role = 'standby'
            # The demote path's release hook drops this peer's claim.
            if self.claimed == 'a':
                self.claimed = None
            return 200, {'role': 'demoting', 'tick': self.a_tick}
        if (method, route) == ('POST', '/promote'):
            return 409, {'refused': {'not_converged': {
                'sync': 'unsynchronized'}}}
        raise AssertionError('unexpected request %s %s' % (method, url))


class SourceRestartTests(unittest.TestCase):
    """scenario_source_restart against the stubbed pair: the tracked
    peer adopts each regressed checkpoint stream at its own run tick
    — monotonic served ticks, newest-last /history, exactly one
    journaled source_restarted per regression carrying the recorded
    fields, the demoted-peer leg's no-prior-alignment form, no
    spurious promotion, and the field's claim fenced throughout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.dirs = {}
        for name in ('a', 'b'):
            directory = self.run_dir / 'controllers' / name
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{"tick": 400}')
            self.dirs[name] = directory
        self.journal_a = self.dirs['a'] / 'journal.jsonl'
        self.journal_b = self.dirs['b'] / 'journal.jsonl'
        self.feed = SourceRestartFeed(self.journal_a, self.journal_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, evidence=None, ctx=None):
        feed = feed if feed is not None else self.feed
        evidence = evidence if evidence is not None else self.evidence
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return None

        def cold_restart(name):
            # The real runner action — the state.json drop under the
            # bounded run dir — then the feed's simulated resume.
            runner.cold_restart_controller(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event))
            feed.cold_restart(name)

        base = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'plant': '127.0.0.1:9999',
                'evidence_dir': str(evidence),
                'cold_restart_controller': cold_restart,
                'journal_files': {'active': str(self.journal_a),
                                  'standby': str(self.journal_b)},
                'state_files': {'active': str(self.dirs['a']
                                              / 'state.json'),
                                'standby': str(self.dirs['b']
                                               / 'state.json')}}
        if ctx is not None:
            base.update(ctx)
        patches = {'POLL_INTERVAL': 0.001, 'SOURCE_RESTART_POLL': 0.001,
                   'SOURCE_RESTART_RETURN_DEADLINE': 0.5,
                   'SOURCE_RESTART_SETTLE_DEADLINE': 0.5,
                   'SOURCE_RESTART_JOURNAL_DEADLINE': 0.3,
                   'SOURCE_RESTART_BASELINE_DEADLINE': 0.3}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_try_plant', feed.try_plant), \
                patch.object(runner, 'docker', fake_docker):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            record = scenarios.scenario_source_restart(base)
        return record, calls, events

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_source_restart,
                      scenarios.SCENARIOS)

    def test_clean_rig_passes_and_validates(self):
        record, calls, events = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.restarts, ['active', 'active'])
        # The real runner action ran each leg: both cold-restart halves
        # on the timeline and both containers' state files dropped.
        self.assertIn('controller-cold-restart', events)
        self.assertIn('controller-cold-restarted', events)
        self.assertEqual(calls.count(('start', 'dcs-hw-qa-1-a')), 2)
        self.assertFalse((self.dirs['a'] / 'state.json').exists())
        self.assertTrue((self.dirs['a'] / 'journal.jsonl').exists())
        self.assertTrue((self.dirs['b'] / 'state.json').exists())
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Both journaled forms landed: the tracked leg's prior
        # alignment and the demoted leg's no-prior-alignment form.
        restarts = scenarios._source_restarts(
            scenarios._journal_entries(self.journal_b))
        self.assertEqual(len(restarts), 2)
        self.assertIsInstance(restarts[0]['was_aligned'], int)
        self.assertIsNone(restarts[1]['was_aligned'])
        self.assertLess(restarts[0]['resumed_at'],
                        restarts[0]['was_aligned'])
        # The restarted peer's journal kept both lifetimes' cold
        # markers beside the launch's.
        bounds = [item['run_boundary'] for item in
                  scenarios._journal_entries(self.journal_a)
                  if 'run_boundary' in item]
        self.assertEqual([b['tick'] for b in bounds], [0, 0, 0])

    def test_rewinding_peer_fails(self):
        # The finding's defect: the regressed apply lands at the
        # stream's own tick — the served run tick rewinds.
        self.feed.rewinds = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rewound', record.get('detail', ''))
        report.validate_scenario(record)

    def test_spurious_promotion_fails(self):
        self.feed.peer_promotes = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('spurious', record.get('detail', ''))
        report.validate_scenario(record)

    def test_repromoted_peer_fails(self):
        # Leg two's defect: the fenced peer re-asserts the field claim
        # after its demotion instead of tracking the new stream.
        self.feed.repromotes = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('re-took', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_resync_fails(self):
        self.feed.skips_journal = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('source_restarted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_doubled_resync_entry_fails(self):
        self.feed.double_restart = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('exactly one', record.get('detail', ''))
        report.validate_scenario(record)

    def test_misreported_alignment_fails(self):
        self.feed.wrong_fields = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('prior alignment', record.get('detail', ''))
        report.validate_scenario(record)

    def test_lapsed_claim_fails(self):
        self.feed.claim_lapses = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim', record.get('detail', ''))
        report.validate_scenario(record)

    def test_kept_alignment_fails_the_demoted_leg(self):
        # The demotion never cleared the alignment: leg two's entry
        # carries a prior alignment instead of the no-alignment form.
        self.feed.keeps_alignment = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-prior-alignment', record.get('detail', ''))
        report.validate_scenario(record)

    def test_warm_resume_is_inconclusive(self):
        # The state file survived: the resumed stream never regressed,
        # so the adoption contract was never exercised.
        self.feed.warm = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never produced a regressed stream',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_action_is_inconclusive(self):
        self.feed.returns = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never completed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_monitor_is_inconclusive(self):
        self.feed.serves = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never returned', record.get('detail', ''))
        report.validate_scenario(record)

    def test_untracked_peer_is_inconclusive(self):
        self.feed.never_tracks = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no adopter', record.get('detail', ''))
        report.validate_scenario(record)

    def test_peer_owned_field_is_inconclusive(self):
        # The post-failover layout: the field owner is the rig's only
        # tracking peer — no tracked endpoint can adopt.
        self.feed.a_role = 'standby'
        self.feed.b_role = 'active'
        self.feed.claimed = 'b'
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('checkpoint-tracking peer',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_action_is_inconclusive(self):
        record, _, _ = self.run_scenario(
            ctx={'cold_restart_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no cold-restart action',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs over the same rig
        # transitions record the same report and evidence bytes — the
        # feed's behavior is call-count keyed, never wall-clock. The
        # evidence embeds the journal paths, so both runs reuse them.
        runs = []
        for _index in range(2):
            for path in (self.journal_a, self.journal_b):
                if path.exists():
                    path.unlink()
            for stale in self.evidence.iterdir():
                stale.unlink()
            for directory in self.dirs.values():
                (directory / 'state.json').write_text('{"tick": 400}')
            feed = SourceRestartFeed(self.journal_a, self.journal_b)
            record, _, _ = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
