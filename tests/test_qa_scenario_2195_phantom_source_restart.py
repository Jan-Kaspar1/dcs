"""The 2195_phantom_source_restart leg's scenario unit coverage —
the feed fake and TestCase classes for
scenario_phantom_source_restart, split out per the #940 convention.
The shared fakes and helpers live in tests/qa_scenario_support.py.
"""
import unittest

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


class PhantomSourceRestartFeed:
    """A stubbed pair for the suppression leg: ctrl-a owns the field
    with no configured tracking source; ctrl-b tracks a through its
    configured `--standby` pull — one pull per completed scan, each
    endpoint call on a paced peer advancing its own scan tick.

    The tracking half models the contract the leg pins. The demote
    releases the field and clears ctrl-a's alignment, the armed
    ctrl-b self-promotes, and ctrl-a's first pull on the uninterrupted
    successor regresses against its own run tick — the exact shape the
    finding's phantom entry carried — while the stream still names the
    generation ctrl-a's own captures stamped, so nothing journals; the
    repeated applies that follow trail the served stream by a scan
    without journaling either. The `phantom` flag replays the pre-fix
    shape and journals that same-generation regression anyway. The cold
    restart drops the tracked source's state file: the fresh process
    mints a new generation, serves a stream regressed to tick 1, and
    the tracking peer journals exactly one entry carrying its prior
    alignment and resumed tick — unless the named defect flags stage
    otherwise."""

    HOSTS = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}
    GENERATION = 0x5eed

    def __init__(self, tmp):
        self.tmp = Path(tmp)
        self.tick = {'a': 100, 'b': 100}
        self.up = {'a': True, 'b': True}
        self.role = {'a': 'active', 'b': 'standby'}
        self.aligned = {'a': None, 'b': 100}
        self.sync = {'a': None, 'b': 'tracking'}
        self.owner = 'a'
        self.seq = {'a': 0, 'b': 0}
        self.served = {'a': [], 'b': []}
        self.journals = {}
        # The tracked line's generation: both peers' captures carry it
        # while the line continues, so a same-generation regression
        # names the peer's own tracking reset, not the source's
        # restart. The cold restart mints a new one.
        self.generation = {'a': self.GENERATION, 'b': self.GENERATION}
        self.pulls = 0
        self.pulled_after_demote = False
        # Fault injection — each named failure and instability the
        # issue calls out.
        self.unreachable = False      # the monitors never answer
        self.no_tracking = False      # ctrl-b never converges
        self.wedged_restore = False   # the walk-back's promote refuses
        self.demote_refused = False   # POST /demote is refused
        self.never_promotes = False   # the armed peer never promotes
        self.never_reconverges = False  # ctrl-a never re-tracks
        self.failover_fires = False   # ctrl-a self-promotes in-window
        self.drop_role = False        # ctrl-a's /role drops in-window
        self.stale_baseline = False   # the baseline read is degraded
        self.phantom = False          # the same-generation phantom
        self.phantom_served = True    # ... on the served monitor too
        self.restart_refused = False  # the cold restart never lands
        self.restart_silent = False   # the genuine restart journals
                                      # nothing
        self.restart_twice = False    # ... twice
        self.no_evidence = False      # ... with no named evidence
        self.nonregressing = False    # ... claiming a non-regressed
                                      # resumed tick
        self.restart_never_regresses = False  # the resumed source
                                            # serves no regressed
                                            # stream at all
        self.predates_contract = False  # served checkpoints carry no
                                        # generation stamp
        for peer in ('a', 'b'):
            path = self.tmp / ('journal-' + peer + '.jsonl')
            path.write_text(json.dumps(
                {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
            self.journals[peer] = path

    # ---- the durable journal --------------------------------------

    def _push(self, peer, kind, body, durable=True):
        """One journaled record on `peer`, appended to its served
        list and — unless the caller is planting the durable-only
        negative — its `--journal-file` the leg reads."""
        self.seq[peer] += 1
        entry = {'seq': self.seq[peer], 'tick': self.tick[peer],
                 'event': {kind: body}}
        self.served[peer].append(entry)
        if durable:
            with self.journals[peer].open('a') as handle:
                handle.write(json.dumps({'entry': entry}) + '\n')

    # ---- the tracking model ----------------------------------------

    def _pull(self, peer):
        """One tracking pull of the configured source: the pulled
        stream's position lands as the peer's alignment, its run tick
        standing where its own clock left it. Under `phantom` the
        pre-fix shape journals the same-generation regression."""
        source = 'b' if peer == 'a' else 'a'
        self.aligned[peer] = self.tick[source]
        self.sync[peer] = 'tracking'
        self.pulls += 1
        if peer == 'a':
            self.pulled_after_demote = True
        if self.phantom and peer == 'a':
            self._push(peer, 'source_restarted',
                       {'was_aligned': None,
                        'resumed_at': self.tick[source]},
                       durable=self.phantom_served)
        if self.failover_fires and peer == 'a' and self.pulls >= 2:
            self.role[peer] = 'promoting'
            self.sync[peer] = None
            self.aligned[peer] = None

    def _cold_restart(self):
        """The tracked source's container cold-restarts: its state
        file drops, the fresh process mints a new generation, and it
        serves from its own run boundary."""
        with self.journals['b'].open('a') as handle:
            handle.write(json.dumps(
                {'run_boundary': {'run': 2, 'tick': 0}}) + '\n')
        self.generation['b'] = self.GENERATION + 1
        self.role['b'] = 'standby'
        self.sync['b'] = 'unsynchronized'
        self.aligned['b'] = None
        self.owner = 'b'
        if self.restart_never_regresses:
            # The resumed process resumed its own line instead of a
            # cold run: the stream never regressed, so the induction
            # crossed no boundary.
            return
        self.tick['b'] = 1
        self.role['b'] = 'active'
        self.sync['b'] = None
        self.aligned['b'] = 1
        if self.restart_silent:
            return
        entries = [{'was_aligned': self.aligned['a'], 'resumed_at': 1}]
        if self.restart_twice:
            entries.append({'was_aligned': 1, 'resumed_at': 2})
        for entry in entries:
            if self.no_evidence:
                entry = {'was_aligned': None, 'resumed_at': None}
            elif self.nonregressing:
                entry = {'was_aligned': self.tick['a'],
                         'resumed_at': self.tick['a']}
            self._push('a', 'source_restarted', entry)

    # ---- the scan cycle --------------------------------------------

    def _scan(self, peer):
        """One completed scan: the peer's own tick advances, its role
        transition settles at the boundary, the armed standby claims a
        field nobody holds, and a tracking peer pulls its source."""
        self.tick[peer] += 1
        if self.role[peer] == 'demoting':
            self.role[peer] = 'standby'
            # The demotion released the field and cleared the demoted
            # peer's alignment — the reset its next pull regresses
            # against.
            self.sync[peer] = 'unsynchronized'
            self.aligned[peer] = None
            if self.owner == peer:
                self.owner = None
        elif self.role[peer] == 'promoting':
            self.role[peer] = 'active'
            self.sync[peer] = None
            self.owner = peer
        elif peer == 'b' and self.owner is None \
                and not self.never_promotes:
            # The armed standby's own claim: the released field is
            # unheld, so the next scan takes it.
            self.role[peer] = 'promoting'
        if self.role[peer] == 'standby' and self.owner != peer \
                and not (self.no_tracking and peer == 'b') \
                and not (self.never_reconverges and peer == 'a'):
            self._pull(peer)
        elif self.role[peer] == 'standby':
            self.sync[peer] = 'unsynchronized'
            self.aligned[peer] = None

    # ---- the runner-owned lifecycle actions ------------------------

    def cold_restart_controller(self, name):
        if self.restart_refused:
            raise RuntimeError('docker stop failed: refused')
        self._cold_restart()

    # ---- the control plane -----------------------------------------

    def _demote(self, peer):
        if self.demote_refused or self.role[peer] != 'active':
            self._raise(409, 'not_active')
        self._push(peer, 'role_changed',
                   {'from': 'active', 'to': 'demoting'})
        self.role[peer] = 'demoting'
        return 200, {'role': 'demoting', 'tick': self.tick[peer]}

    def _promote(self, peer):
        if self.wedged_restore or self.role[peer] in ('active', 'promoting'):
            self._raise(409, {'not_converged': {'sync': self.sync[peer]}})
        self.role[peer] = 'promoting'
        return 200, {'role': 'promoting', 'tick': self.tick[peer]}

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://rig', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    # ---- the endpoint dispatch -------------------------------------

    def _report(self, peer):
        report = {'role': self.role[peer], 'tick': self.tick[peer]}
        if self.role[peer] == 'standby':
            sync = self.sync[peer]
            if sync in ('tracking', 'orphaned'):
                report['sync'] = {sync: {'aligned': self.aligned[peer]}}
            elif sync == 'degraded':
                report['sync'] = {'degraded': {
                    'detail': 'fetch from ctrl-a:1: timed out'}}
            else:
                report['sync'] = sync or 'unsynchronized'
        return report

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        peer = self.HOSTS[url.split('/')[2]]
        if not self.up[peer]:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        # The paced peers: every endpoint call is one completed scan.
        self._scan(peer)
        if (method, route) == ('GET', '/role'):
            if self.drop_role and peer == 'a' \
                    and self.role['a'] == 'standby':
                raise urllib.error.URLError('timed out')
            return 200, self._report(peer)
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick[peer]}
        if (method, route) == ('GET', '/checkpoint'):
            checkpoint = {'tick': self.tick[peer],
                          'generation': self.generation[peer]}
            if self.predates_contract:
                del checkpoint['generation']
            return 200, checkpoint
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if query else 0
            return 200, [entry for entry in self.served[peer]
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class PhantomSourceRestartTests(unittest.TestCase):
    """The phantom-source-restart leg against the stubbed pair: a
    clean rig passes with identical digests and evidence — the demote
    landed, the demoted peer tracking across repeated applies with no
    journaled restart, the genuine cold restart journaling exactly one
    entry with its named evidence, the launch roles restored — each
    doctored contract breach reports
    source-restart-evidence-failed, each instability reports
    source-restart-evidence-nondeterministic, and an unreachable,
    unconverged, seam-less, or pre-contract run is inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = PhantomSourceRestartFeed(self.tmp.name)
        self.feed.aligned_before = 99
        self.feed.up = {'a': True, 'b': True}
        # The default staging: no fault injection.
        for flag in ('phantom', 'unreachable', 'no_tracking',
                     'wedged_restore', 'restart_refused',
                     'restart_silent', 'restart_twice', 'no_evidence',
                     'nonregressing', 'never_reconverges',
                     'never_promotes', 'predates_contract',
                     'drop_role'):
            setattr(self.feed, flag, False)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'journal_files': {
                   'active': str(feed.journals['a']),
                   'standby': str(feed.journals['b'])},
               'cold_restart_controller': feed.cold_restart_controller}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, feed=None, **overrides):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ORDER_SETTLE', 0.5), \
                patch.object(scenarios, 'ORDER_POLL', 0.001), \
                patch.object(scenarios, 'ORDER_HOLD', 0.5), \
                patch.object(scenarios, 'ORDER_ADOPT', 6):
            return scenarios.scenario_phantom_source_restart(
                self.ctx(feed, **overrides))

    def passes(self):
        names = ('phantom-source-restart-pass-1.json',
                 'phantom-source-restart-pass-2.json')
        for name in names:
            self.assertTrue((self.evidence / name).is_file(), name)
        return [json.loads((self.evidence / name).read_text())
                for name in names]

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The leg's window: behind the realign leg whose restore leaves
        # the launch layout standing, before the failover case that
        # layout is owed to.
        self.assertLess(
            order.index(scenarios.scenario_tracker_realign_tick_order),
            order.index(scenarios.scenario_phantom_source_restart))
        self.assertLess(
            order.index(scenarios.scenario_phantom_source_restart),
            order.index(scenarios.scenario_failover))
        self.assertIs(
            verify.case_function('phantom-source-restart'),
            scenarios.scenario_phantom_source_restart)

    def test_clean_rig_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passes = self.passes()
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(
            passes[0]['digest'],
            {'demoted': 'tracking', 'phantom': 'none',
             'restart': 'one', 'roles': 'restored'})
        first = passes[0]['record']
        # The demotion landed, the demoted peer reconverged tracking
        # across the repeated applies, and neither journal carried a
        # phantom on the same-generation pulls.
        self.assertEqual(first['demote_status'], 200)
        self.assertIsNotNone(first['reconverged'])
        self.assertEqual(first['phantom'], [])
        self.assertEqual(first['served_phantom'], [])
        self.assertTrue(first['restored'])
        # The genuine cold restart journaled exactly one entry with
        # its named evidence — a resumed tick below the prior
        # alignment it broke.
        self.assertEqual(len(first['restarts']), 1)
        entry = first['restarts'][0]
        self.assertIsInstance(entry['was_aligned'], int)
        self.assertLess(entry['resumed_at'], entry['was_aligned'])
        report.validate_scenario(record)

    def test_phantom_served_fails(self):
        # The doctored negative the issue names: a phantom entry
        # journaled on the same-generation pull, read through the
        # demoted peer's serving monitor.
        self.feed.phantom = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-failed'), record['detail'])
        self.assertIn('phantom', record['detail'])
        report.validate_scenario(record)

    def test_phantom_durable_fails(self):
        # ... and its durable half — the same phantom in the
        # --journal-file the leg reads after the window.
        feed = PhantomSourceRestartFeed(self.tmp.name)
        feed.aligned_before = 99
        feed.phantom = True
        # The served read stays clean; only the durable file carries
        # the phantom, so the durable clause is the one that fires.
        feed.http_json = self._durable_only_phantom(feed)
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-failed'), record['detail'])
        report.validate_scenario(record)

    @staticmethod
    def _durable_only_phantom(feed):
        """A dispatch whose served `/journal` never reports the
        phantom the durable file carries — the shape the durable
        clause exists to catch."""
        dispatch = feed.http_json

        def http_json(method, url, body=None, timeout=10):
            if url.endswith('/journal'):
                peer = feed.HOSTS[url.split('/')[2]]
                served = [entry for entry in feed.served[peer]
                          if entry['event'] != {'source_restarted':
                                                feed.phantom_body()}]
                return 200, served
            return dispatch(method, url, body, timeout)
        return http_json

    def test_restart_silent_fails(self):
        # The genuine restart asserted as unjournaled — suppression
        # over-reaching past its generation scope.
        self.feed.restart_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-failed'), record['detail'])
        report.validate_scenario(record)

    def test_restart_twice_reports_nondeterministic(self):
        self.feed.restart_twice = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_evidence_absent_fails(self):
        # The entry lands but names none of the evidence the contract
        # owes the record.
        self.feed.no_evidence = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-failed'), record['detail'])
        self.assertIn('evidence', record['detail'])
        report.validate_scenario(record)

    def test_nonregressing_resume_fails(self):
        # The entry claims a restart whose resumed tick never
        # regressed below the alignment it broke.
        self.feed.nonregressing = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-failed'), record['detail'])
        self.assertIn('regressed', record['detail'])
        report.validate_scenario(record)

    def test_never_reconverged_fails(self):
        # The demoted peer never reconverged tracking — the reset the
        # clause distinguishes never happened.
        self.feed.never_reconverges = True
        self.feed.no_restart_error = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-failed'), record['detail'])
        report.validate_scenario(record)

    def test_roles_unrestored_fails(self):
        self.feed.never_reconverges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('launch roles', record['detail'])
        report.validate_scenario(record)

    def test_failover_fires_reports_nondeterministic(self):
        # The demoted peer left standby inside the held window — the
        # armed failover boundary, not the reset the leg stages.
        feed = PhantomSourceRestartFeed(self.tmp.name)
        feed.aligned_before = 99
        feed.failover_fires = True
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_demote_refused_reports_nondeterministic(self):
        feed = PhantomSourceRestartFeed(self.tmp.name)
        feed.aligned_before = 99
        feed.demote_refused = True
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_promote_never_converges_reports_nondeterministic(self):
        self.feed.never_promotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_restart_refused_reports_nondeterministic(self):
        self.feed.restart_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_journal_unreadable_reports_nondeterministic(self):
        feed = PhantomSourceRestartFeed(self.tmp.name)
        feed.aligned_before = 99
        feed.journals['a'].write_text('{not json}\n')
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_window_silent_reports_nondeterministic(self):
        feed = PhantomSourceRestartFeed(self.tmp.name)
        feed.aligned_before = 99
        feed.drop_role = True
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_restart_unproven_reports_nondeterministic(self):
        # The induction produced no regressed stream at all — the
        # contract cannot speak for a run where no boundary crossed.
        feed = PhantomSourceRestartFeed(self.tmp.name)
        feed.aligned_before = 99
        feed.restart_silent = True
        feed.restart_never_regresses = True
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        with patch.object(scenarios, '_restart_evidence_digest',
                          side_effect=[{'roles': 'restored'},
                                       {'roles': 'unrestored'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'source-restart-evidence-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_silent_judge_reports_unchecked(self):
        # A judge that notes nothing lets every planted negative
        # slip — the leg's own audits can no longer catch what they
        # name.
        with patch.object(scenarios, '_judge_restart_evidence',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'phantom-source-restart-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_unreachable_pair_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_swapped_layout_reports_inconclusive(self):
        # ctrl-b already owns the field and ctrl-a never converged —
        # and the wedged restore refuses the walk back, so the
        # launch-layout pair the leg stages never settles.
        self.feed.wedged_restore = True
        self.feed.role = {'a': 'standby', 'b': 'active'}
        self.feed.sync = {'a': None, 'b': 'unsynchronized'}
        self.feed.owner = 'b'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch layout', record['detail'])
        report.validate_scenario(record)

    def test_missing_restart_action_reports_inconclusive(self):
        record = self.run_scenario(cold_restart_controller=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('cold-restart', record['detail'])
        report.validate_scenario(record)

    def test_missing_journal_files_reports_inconclusive(self):
        record = self.run_scenario(journal_files={})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal', record['detail'])
        report.validate_scenario(record)

    def test_predates_contract_reports_inconclusive(self):
        # A staged run whose served checkpoint predates the
        # generation stamp the suppression reads.
        self.feed.predates_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record['detail'])
        report.validate_scenario(record)

    def test_single_endpoint_reports_inconclusive(self):
        record = self.run_scenario(standby=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('one endpoint', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('run' + str(index))
            (evidence / 'journals').mkdir(parents=True)
            self.evidence = evidence
            feed = PhantomSourceRestartFeed(str(evidence / 'journals'))
            feed.aligned_before = 99
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in evidence.iterdir()
                                  if p.is_file()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


if __name__ == '__main__':
    unittest.main()