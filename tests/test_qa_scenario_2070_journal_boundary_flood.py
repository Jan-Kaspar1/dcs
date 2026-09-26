"""The 2070_journal_boundary_flood leg's scenario unit coverage — the
feed fakes and TestCase classes for scenario_journal_boundary_flood.
The shared fakes and helpers live in tests/qa_scenario_support.py;
EXPECTED_CASES pins this module's contribution to the suite's case
coverage so a dropped case fails the discovery check in
tests/test_qa_scenario_modules.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'JournalBoundaryFloodTests.test_clean_passes_and_restores',
    'JournalBoundaryFloodTests.test_dropped_boundary_fails',
    'JournalBoundaryFloodTests.test_unbounded_tail_fails',
    'JournalBoundaryFloodTests.test_hidden_gap_fails',
    'JournalBoundaryFloodTests.test_broken_cursor_fails',
    'JournalBoundaryFloodTests.test_orphaned_served_seq_fails',
    'JournalBoundaryFloodTests.test_disordered_file_fails',
    'JournalBoundaryFloodTests.test_marker_lost_file_side_fails',
    'JournalBoundaryFloodTests.test_moved_roles_fail',
    'JournalBoundaryFloodTests.test_restart_moved_owner_fails',
    'JournalBoundaryFloodTests.test_flood_refusal_fails',
    'JournalBoundaryFloodTests.test_no_active_peer_fails',
    'JournalBoundaryFloodTests.test_starved_second_pass_is_'
    'nondeterministic',
    'JournalBoundaryFloodTests.test_missing_served_boundary_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_missing_durable_boundary_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_restart_action_missing_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_journal_paths_missing_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_unreachable_rig_is_inconclusive',
    'JournalBoundaryFloodTests.test_restart_action_failure_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_unreturned_monitor_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_no_journal_surface_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_no_writable_point_is_inconclusive',
    'JournalBoundaryFloodTests.test_no_tracking_standby_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_post_failover_layout_is_'
    'inconclusive',
    'JournalBoundaryFloodTests.test_two_runs_produce_identical_'
    'evidence',
})


class PeerJournal:
    """One controller's journal pair: the append-only --journal-file
    the runner bind-mounts and the bounded served surface the monitor
    answers — a pinned run-boundary stream ahead of a `cap`-entry
    oldest-first ring, mirroring the store's contract. A restart adds
    the file's next sequential marker and the lifetime's one served
    boundary entry, seqs continuing across lifetimes. Fault flags
    stage the named violations the leg audits: evicted boundaries
    silently dropped, a ring that never enforces its bound, file
    lines the served stream never wrote, and served answers that
    fabricate the evicted window or leak past the cursor."""

    def __init__(self, path, cap):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.cap = cap
        self.ring = []         # the served retained tail
        self.pinned = []       # evicted run_boundary entries
        self.next_seq = 1
        self.run = 1
        # Fault flags.
        self.drop_boundaries = False  # eviction drops markers
        self.file_drop_left = 0       # file lines the ring still serves
        self.file_dup_left = 0        # file lines written twice
        self.cursor_leak = False      # since= answers beyond the tail
        self.fabricate = False        # renumber the tail to hide the gap
        self._write({'run_boundary': {'run': 1, 'tick': 0}})

    def _write(self, record):
        with self.path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def push(self, event, tick):
        entry = {'seq': self.next_seq, 'tick': tick, 'event': event}
        self.next_seq += 1
        if self.file_drop_left > 0:
            self.file_drop_left -= 1
        else:
            self._write({'entry': entry})
            if self.file_dup_left > 0:
                # A duplicated file record: the served ring's seqs are
                # untouched but the durable record's contiguity breaks.
                self.file_dup_left -= 1
                self._write({'entry': entry})
        self.ring.append(entry)
        while len(self.ring) > self.cap:
            evicted = self.ring.pop(0)
            if 'run_boundary' in evicted['event'] \
                    and not self.drop_boundaries:
                self.pinned.append(evicted)
        return entry

    def restart(self, tick, marker=True, boundary=True):
        """The warm restart's journal effect: the file's next marker,
        then the lifetime's one served boundary entry — both absent
        on a rig predating the contract."""
        self.run += 1
        if marker:
            self._write({'run_boundary': {'run': self.run,
                                          'tick': tick}})
        if boundary:
            self.push({'run_boundary': {'run': self.run}}, tick)

    def journal(self, since):
        answer = [entry for entry in self.pinned
                  if entry['seq'] > since] \
            + [entry for entry in self.ring if entry['seq'] > since]
        if self.fabricate:
            # The dishonest answer: the tail renumbered seamless past
            # the last boundary — the evicted window reads as no gap.
            anchor = max((entry['seq'] for entry in answer
                          if 'run_boundary' in entry['event']),
                         default=0)
            following = anchor
            answer = [dict(entry) for entry in answer]
            for entry in answer:
                if 'run_boundary' not in entry['event']:
                    following += 1
                    entry['seq'] = following
        if self.cursor_leak and since:
            answer = answer + [{'seq': self.next_seq, 'tick': 0,
                                'event': {'leak': {}}}]
        return answer


class BoundaryFeed:
    """A stubbed pair for the journal-boundary-flood scenario. ctrl-a
    owns the field and journals every receipted settlement; ctrl-b
    tracks it and adopts each settlement into its own journal a pull
    later — the served volume the leg's flood drives. The restart
    action warm-restarts ctrl-b (the rig's only restart-safe member):
    the file gains the next marker, the served journal the boundary
    entry, and the monitor blips before answering tracking again.
    Fault flags stage each named failure and inconclusive case the
    leg reports."""

    def __init__(self, journal_a, journal_b, cap=8):
        self.cap = cap
        self.tick = 100
        self.owner = PeerJournal(journal_a, cap)
        self.peer = PeerJournal(journal_b, cap)
        # The run-1 census observations both peers file.
        for journal in (self.owner, self.peer):
            journal.push({'quality_changed': {'point': 10,
                                              'from': None,
                                              'to': 'good'}}, 1)
            journal.push({'point_changed': {'point': 10,
                                            'from': None,
                                            'to': {'bool': False}}},
                         1)
        self.restarts = []
        self.submitted = 0
        self.adopted = 0
        self.plant_calls = []
        # Fault flags — the leg's named cases.
        self.unreachable = False      # nothing answers
        self.no_journal = False       # /journal predates the surface
        self.writable = True          # the model's command target
        self.owner_inactive = False   # no peer reports active
        self.post_failover = False    # ctrl-b already owns the field
        self.peer_untracked = False   # no tracking standby
        self.returns = True           # the restart action completes
        self.serves_peer = True       # the restarted monitor returns
        self.peer_down = 0            # refused polls left
        self.peer_gone = False        # ...and never returns at all
        self.served_marker = True     # restart journals the boundary
        self.file_marker = True       # restart files the marker
        self.owner_falls = False      # the restart drops the owner
        self.roles_break_at = None    # submissions before roles move
        self.roles_broken = False
        self.starve_at_run = None     # adoption stops from this run
        self.starve_after = 0         # adopted entries before starving
        self.flood_status = 200       # the flood's answered status

    def restart(self, name):
        """The runner-owned lifecycle action — replaces
        ctx['restart_controller']."""
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such '
                               'container')
        self.peer.restart(self.tick, marker=self.file_marker,
                          boundary=self.served_marker)
        self.adopted = 0      # adoptions count per lifetime
        self.peer_down = 2
        if self.owner_falls:
            self.owner_fell = True

    def plant_ctl(self, *args):
        """The ctx['plant_ctl'] seam — the shipped tool's answers plus
        the quality transitions a fault inject/clear journals on
        both field observers."""
        self.plant_calls.append(args)
        op = args[0]
        if op == 'list':
            return _ctl_process({'result': 'points', 'points': [
                {'point': 10, 'direction': 'in', 'sample': {
                    'value': {'bool': False},
                    'quality': {'quality': 'good'}}},
                {'point': 11, 'direction': 'in', 'sample': {
                    'value': {'bool': False},
                    'quality': {'quality': 'good'}}},
                {'point': 20, 'direction': 'out', 'sample': {
                    'value': {'bool': False},
                    'quality': {'quality': 'good'}}}]})
        if op in ('fault', 'clear-fault'):
            point = int(args[1])
            quality = 'bad:device_fault' if op == 'fault' else 'good'
            for journal in (self.owner, self.peer):
                journal.push({'quality_changed': {
                    'point': point, 'from': 'good',
                    'to': quality}}, self.tick)
            return _ctl_process({'result': 'done'})
        if op == 'read':
            return _ctl_process({'result': 'sample', 'sample': {
                'value': {'bool': False},
                'quality': {'quality': 'good'}}})
        raise AssertionError('plant_ctl argv outside the covered '
                             'subcommands: %s' % (args,))

    def _role_for(self, host):
        if host == 'ctrl-a:1':
            if self.post_failover or getattr(self, 'owner_fell',
                                             False) \
                    or self.roles_broken or self.owner_inactive:
                return {'role': 'standby', 'tick': self.tick,
                        'sync': {'tracking': {'aligned': self.tick}}}
            return {'role': 'active', 'tick': self.tick}
        if self.post_failover or self.roles_broken:
            return {'role': 'active', 'tick': self.tick}
        sync = {'tracking': {'aligned': self.tick}} \
            if not self.peer_untracked \
            else {'degraded': {'detail': 'checkpoint pull failed'}}
        return {'role': 'standby', 'tick': self.tick, 'sync': sync}

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        journal = self.owner if host == 'ctrl-a:1' else self.peer
        if host == 'ctrl-b:2' and self.peer_gone:
            raise urllib.error.URLError('connection refused')
        if host == 'ctrl-b:2' and self.peer_down > 0:
            self.peer_down -= 1
            if self.peer_down <= 0:
                if self.serves_peer:
                    pass
                else:
                    self.peer_gone = True
                    raise urllib.error.URLError('connection refused')
            else:
                raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role_for(host)
        if (method, route) == ('GET', '/journal'):
            if self.no_journal:
                raise urllib.error.HTTPError(
                    url, 404, 'not found', {}, None)
            since = int(query.split('=', 1)[1])
            return 200, journal.journal(since)
        if (method, route) == ('GET', '/signals'):
            points = [{'point': 10, 'signal': None, 'name': 'p101-oos',
                       'direction': 'in', 'value_type': 'bool',
                       'writable': self.writable},
                      {'point': 20, 'signal': None,
                       'name': 'level-primary', 'direction': 'in',
                       'value_type': 'float', 'writable': False}]
            return 200, {'points': points, 'components': []}
        if (method, route) == ('POST', '/command'):
            if host != 'ctrl-a:1':
                receipt = {'command': body['command'],
                           'outcome': {'rejected': {'reason': {
                               'role': 'standby'}}},
                           'actor': body.get('actor')}
                return 200, receipt
            if self.flood_status != 200:
                return self.flood_status, {'error': 'busy'}
            write = body['command']['write_value']
            receipt = {'command': body['command'],
                       'outcome': {'applied': {'tick': self.tick}},
                       'actor': body.get('actor')}
            self.submitted += 1
            event = {'command_settled': {'receipt': receipt}}
            journal.push(event, self.tick)
            if self.starve_at_run is None \
                    or self.peer.run < self.starve_at_run \
                    or self.adopted < self.starve_after:
                self.peer.push({'command_settled': {'receipt': receipt}},
                               self.tick)
                self.adopted += 1
            if self.roles_break_at is not None \
                    and self.submitted >= self.roles_break_at:
                self.roles_broken = True
            return 200, receipt
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class JournalBoundaryFloodTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_a = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal_b = Path(self.tmp.name) / 'controllers' / 'b' \
            / 'journal.jsonl'
        self.feed = BoundaryFeed(self.journal_a, self.journal_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, ctx_patches=None, **patches):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'restart_controller': feed.restart,
               'journal_files': {'active': str(self.journal_a),
                                 'standby': str(self.journal_b)},
               'plant_ctl': feed.plant_ctl}
        ctx.update(ctx_patches or {})
        defaults = {'JOURNAL_BOUND': feed.cap, 'FLOOD_MARGIN': 4,
                    'FLOOD_BATCH': 4, 'BOUNDARY_SETTLE': 0.3,
                    'BOUNDARY_POLL': 0.001, 'FLOOD_SETTLE': 0.3,
                    'FLOOD_POLL': 0.001, 'FAULT_HOLD': 0.001}
        defaults.update(patches)
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_journal_boundary_flood(ctx)

    def test_clean_passes_and_restores(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        # Two passes — the tracked peer restarted once per pass.
        self.assertEqual(self.feed.restarts,
                         ['standby', 'standby'])
        # The durable record kept every marker in order across both
        # passes.
        markers = [json.loads(line)['run_boundary']['run']
                   for line in self.journal_b.read_text().splitlines()
                   if 'run_boundary' in json.loads(line)]
        self.assertEqual(markers, [1, 2, 3])
        # The flood's quality half rode the plant seam.
        self.assertTrue(any(call[0] == 'fault'
                            for call in self.feed.plant_calls))
        self.assertTrue(any('identical' in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_dropped_boundary_fails(self):
        # The pre-#623 shape: ordinary eviction drops the boundary
        # rather than pinning it — lifetime attribution is lost.
        self.feed.peer.drop_boundaries = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('attribution', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unbounded_tail_fails(self):
        # A rig retaining past the declared bound — the peer's ring
        # caps higher than the scenario's declared JOURNAL_BOUND.
        self.feed.peer.cap = 12
        record = self.run_scenario(JOURNAL_BOUND=8)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('bound', record.get('detail', ''))
        report.validate_scenario(record)

    def test_hidden_gap_fails(self):
        # Eviction that never reads as the recorded numbering gap —
        # the dishonest answer renumbers the retained tail seamless.
        self.feed.peer.fabricate = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('silent', record.get('detail', ''))
        report.validate_scenario(record)

    def test_broken_cursor_fails(self):
        self.feed.peer.cursor_leak = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('cursor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_orphaned_served_seq_fails(self):
        # The served ring carries an entry the durable record never
        # wrote — attribution cannot be verified against the file.
        self.feed.peer.file_drop_left = 1
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('never wrote', record.get('detail', ''))
        report.validate_scenario(record)

    def test_disordered_file_fails(self):
        # The durable record's own ordering breaks: a file line
        # written twice leaves the entry seqs non-contiguous — the
        # flood's served-ring eviction must leave the file untouched.
        self.feed.peer.file_dup_left = 1
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('durable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_marker_lost_file_side_fails(self):
        # The restart journals its served boundary but the durable
        # file never gained the marker — the served entry attributes
        # to a run the file does not record.
        self.feed.file_marker = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_moved_roles_fail(self):
        # The flood moves the pair off its launch roles: ctrl-a
        # demotes mid-drive and ctrl-b takes the field.
        self.feed.roles_break_at = 3
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('roles', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restart_moved_owner_fails(self):
        self.feed.owner_falls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('field owner', record.get('detail', ''))
        report.validate_scenario(record)

    def test_flood_refusal_fails(self):
        self.feed.flood_status = 503
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        self.assertIn('receipted path', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_peer_fails(self):
        self.feed.owner_inactive = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-failed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_starved_second_pass_is_nondeterministic(self):
        # Pass 2's flood never reaches the bound — the tracked
        # peer's adoption starved, so identical passes diverge.
        self.feed.starve_at_run = 3
        self.feed.starve_after = 3
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal-boundary-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_served_boundary_is_inconclusive(self):
        # The restart grows the durable marker but the served
        # journal never carries its boundary entry — a rig
        # predating the served-marker contract.
        self.feed.served_marker = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_durable_boundary_is_inconclusive(self):
        # The restart produces neither the file's marker nor the
        # served entry — a rig predating the durable record.
        self.feed.served_marker = False
        self.feed.file_marker = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restart_action_missing_is_inconclusive(self):
        record = self.run_scenario(
            ctx_patches={'restart_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_journal_paths_missing_is_inconclusive(self):
        record = self.run_scenario(
            ctx_patches={'journal_files': {}})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restart_action_failure_is_inconclusive(self):
        self.feed.returns = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never completed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_monitor_is_inconclusive(self):
        # The restarted peer's monitor never comes back: the
        # refused-poll window never closes.
        self.feed.serves_peer = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never returned', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_journal_surface_is_inconclusive(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_writable_point_is_inconclusive(self):
        self.feed.writable = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writable', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_tracking_standby_is_inconclusive(self):
        self.feed.peer_untracked = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking', record.get('detail', ''))
        report.validate_scenario(record)

    def test_post_failover_layout_is_inconclusive(self):
        # ctrl-b already owns the field: the tracked peer is ctrl-a,
        # which the leg must never restart — the launch layout the
        # restore owes is absent.
        self.feed.post_failover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('standby', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs of the scenario
        # against the same rig layout record the same report and the
        # same evidence files.
        runs = []
        for _index in range(2):
            for path in (self.journal_a, self.journal_b):
                if path.exists():
                    path.unlink()
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = BoundaryFeed(self.journal_a, self.journal_b)
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()
