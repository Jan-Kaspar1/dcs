"""The 0950_dead_active_unconverged_recovery leg's scenario unit
coverage — the feed fake and TestCase classes for
scenario_dead_active_unconverged_recovery, split out of the
test_qa_scenarios monolith (#940). The shared fakes and helpers live
in tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.
"""
import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'DeadActiveRecoveryTests.test_registered_after_superseded_before_tune',
    'DeadActiveRecoveryTests.test_clean_run_passes_and_validates',
    'DeadActiveRecoveryTests.test_two_runs_produce_identical_evidence',
    'DeadActiveRecoveryTests.test_evidence_names_every_leg',
    'DeadActiveRecoveryTests.test_the_pair_ends_on_its_launch_roles',
    'DeadActiveRecoveryTests.test_missing_lifecycle_actions_are_inconclusive',
    'DeadActiveRecoveryTests.test_missing_plant_endpoint_is_inconclusive',
    'DeadActiveRecoveryTests.test_missing_monitors_are_inconclusive',
    'DeadActiveRecoveryTests.test_missing_owner_tokens_are_inconclusive',
    'DeadActiveRecoveryTests.test_missing_journals_are_inconclusive',
    'DeadActiveRecoveryTests.test_unreachable_pair_is_inconclusive',
    'DeadActiveRecoveryTests.test_unsettled_pair_is_failed',
    'DeadActiveRecoveryTests.test_unclaimed_field_at_bring_up_fails',
    'DeadActiveRecoveryTests.test_foreign_named_claim_fails',
    'DeadActiveRecoveryTests.test_stop_lever_failure_is_inconclusive',
    'DeadActiveRecoveryTests.test_start_lever_failure_is_inconclusive',
    'DeadActiveRecoveryTests.test_unobserved_wedge_is_inconclusive',
    'DeadActiveRecoveryTests.test_never_unconverged_fails',
    'DeadActiveRecoveryTests.test_standby_promoting_itself_fails',
    'DeadActiveRecoveryTests.test_quiesced_peer_stops_serving_fails',
    'DeadActiveRecoveryTests.test_promote_admitted_fails',
    'DeadActiveRecoveryTests.test_promote_other_refusal_fails',
    'DeadActiveRecoveryTests.test_dead_claim_lapsed_fails',
    'DeadActiveRecoveryTests.test_dead_claim_moved_fails',
    'DeadActiveRecoveryTests.test_resumed_never_owns_fails',
    'DeadActiveRecoveryTests.test_resumed_dual_active_fails',
    'DeadActiveRecoveryTests.test_field_not_stepping_fails',
    'DeadActiveRecoveryTests.test_field_unfenced_after_recovery_fails',
    'DeadActiveRecoveryTests.test_field_claimed_by_another_fails',
    'DeadActiveRecoveryTests.test_standby_never_reconverges_fails',
    'DeadActiveRecoveryTests.test_unreadable_journal_is_inconclusive',
    'DeadActiveRecoveryTests.test_source_restart_is_recorded',
    'DeadActiveRecoveryTests.test_missing_source_restart_is_not_faulted',
    'DeadActiveRecoveryTests.test_restore_switch_refused_fails',
    'DeadActiveRecoveryTests.test_restore_not_settling_fails',
})


MEMBERS = ('active', 'standby')
HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby'}
DEAD_TOKEN = 0xDAED
PEER_TOKEN = 0xDAEE
POINT = 10


class DeadActivePlant(ClaimPlantPeer):
    """The claim-enforcing plant the dead-active leg fences through,
    with the writable bool in-point its snapshot leg reads."""

    def __init__(self):
        super().__init__()
        self.samples[POINT] = {'value': {'bool': False},
                               'quality': 'good', 'tick': 0}
        self.directions[POINT] = 'in'


class DeadActiveFeed:
    """A stubbed pair for the dead-active unconverged-recovery leg.

    ctrl-a is the launched active: it holds the field's single-writer
    claim under its pinned `--owner-token` through its own sim-net
    attachment. Stopping its container leaves the claim standing under
    that dead holder — the field's arbitration drops a hold only on an
    explicit `release_writer` from its own attachment, never on a
    disconnect. ctrl-b is the tracking standby: while its source is
    dead its pulls miss, so its sync report walks `degraded` and every
    `POST /promote` answers the named `not_converged` refusal, while
    `/role`, `/snapshot`, and `/journal` keep answering.

    Starting the stopped container back returns it as its configured
    active, whose cold-start claim preempts the dead hold. Fault flags
    stage each named failure and each inconclusive the leg classifies.
    """

    def __init__(self, plant, journal_a, journal_b):
        self.plant = plant
        self.tick = 500
        self.roles = {'active': 'active', 'standby': 'standby'}
        self.syncs = {'active': None, 'standby': 'tracking'}
        self.owner = 'active'
        self.down = False
        self.starts = 0
        self.stops = 0
        self.seq = {'active': 0, 'standby': 0}
        self.runs = {'active': 1, 'standby': 1}
        self.paths = {'active': Path(journal_a), 'standby': Path(journal_b)}
        self.streams = {name: self._connect() for name in MEMBERS}
        for path in self.paths.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._append(path, {'run_boundary': {'run': 1, 'tick': 0}})
        self._plant_claim()
        # Fault injection — each named failure the issue calls out.
        self.never_settles = False    # the launch shape never holds
        self.demotes = 0
        self.promotes = 0
        self.demote_refuses_at = -1   # the nth demote (0-based) refused
        self.no_claim = False         # the field stands unclaimed
        self.foreign_claim = False    # the claim names another token
        self.stop_fails = False       # the stop lever raises
        self.start_fails = False      # the start lever raises
        self.stays_converged = False  # the dead source never degrades
        self.self_promotes = False    # the quiesced peer promotes itself
        self.silent_snapshot = False  # the quiesced peer stops serving
        self.promote_admitted = False  # the unconverged promote succeeds
        self.promote_other = False    # the refusal names another cause
        self.claim_lapses = False     # the dead owner's claim drops
        self.claim_moves = False      # another token takes the field
        self.resume_stalls = False    # the relaunched owner never resumes
        self.resume_demotes = False   # the relaunch never owns the field
        self.dual_active = False      # both peers report active
        self.field_stalls = False     # the resumed owner's tick freezes
        self.probe_foreign = False    # a foreign token holds the claim
        self.probe_open = False       # the field stops fencing afterwards
        self.peer_silent = False      # the quiesced monitor never answers
        self.no_resync = False        # the standby never reconverges
        self.no_source_restart = False  # the resync record goes unwritten
        self.restore_stalls = False   # the restore never settles
        self.both_down = False        # neither monitor ever answers
        self.resynced = False         # the standby journaled its resync
        self.journal_corrupts = False  # the durable record goes unreadable

    def close(self):
        for stream in self.streams.values():
            try:
                stream.close()
            except OSError:
                pass

    # --- the plant attachment --------------------------------------

    def _connect(self):
        host, _, port = self.plant.address.rpartition(':')
        return socket.create_connection((host, int(port)), timeout=5)

    def _plant_call(self, request):
        stream = self.streams['active']
        stream.sendall(json.dumps(request).encode() + b'\n')
        line = b''
        while not line.endswith(b'\n'):
            chunk = stream.recv(65536)
            if not chunk:
                raise ConnectionError('the plant closed mid-answer')
            line += chunk
        return json.loads(line)

    def _plant_claim(self, owner=DEAD_TOKEN):
        return self._plant_call({'op': 'claim_writer', 'owner': owner})



    # --- the durable records ---------------------------------------

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, name, event):
        self.seq[name] += 1
        self._append(self.paths[name], {'entry': {
            'seq': self.seq[name], 'tick': self.tick, 'event': event}})

    def _served_journal(self, name):
        served = []
        for line in self.paths[name].read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if 'entry' in record:
                served.append(record['entry'])
        return served

    # --- the runner's lifecycle levers ------------------------------

    def stop_controller(self, name):
        self.stops += 1
        if self.stop_fails:
            raise RuntimeError('docker stop failed: no such container')
        if name != 'active':
            return
        self.down = True
        if self.claim_lapses:
            # The dead owner's connection-bound hold released with it —
            # the field left unclaimed behind a dead owner.
            self.plant.claim = None
        elif self.claim_moves:
            self._plant_claim(PEER_TOKEN)

    def start_controller(self, name):
        self.starts += 1
        if self.start_fails:
            raise RuntimeError('docker start failed: name in use')
        if name != 'active':
            return
        self.down = False
        self.runs['active'] += 1
        self.seq['active'] = 0
        self._append(self.paths['active'], {'run_boundary': {
            'run': self.runs['active'], 'tick': self.tick}})
        if self.resume_stalls:
            return
        # The launched active's cold-start claim preempts the dead
        # hold unconditionally — decision 86's recorded recovery.
        self._plant_claim(PEER_TOKEN if self.probe_foreign else DEAD_TOKEN)
        if self.probe_open:
            self.plant.open_field = True
        self.roles['active'] = 'active'
        self.syncs['active'] = None
        self.owner = 'active'
        if self.resume_demotes:
            self.roles['active'] = 'standby'
            self.syncs['active'] = 'tracking'
            self.owner = 'standby'

    # --- the monitor channel ---------------------------------------

    def _advance(self):
        if self.field_stalls and self.owner == 'active':
            return
        self.tick += 1
        self._resync_record()

    def _resync_record(self):
        """The `source_restarted` record the standby journals once it
        resumes tracking the resumed owner's new checkpoint stream — the
        stream generation regressed across the restart's boundary."""
        if not self.starts or self.resynced or self.no_source_restart:
            return
        if self.roles['standby'] != 'standby' or self.down:
            return
        self.resynced = True
        if self.journal_corrupts:
            with self.paths['standby'].open('a') as stream:
                stream.write('torn durable record\n')
        self._entry('standby', {'source_restarted': {
            'was_aligned': self.tick, 'resumed_at': 0}})

    def _report(self, name):
        if self.never_settles:
            return {'role': 'demoting', 'tick': self.tick}
        report = {'role': self.roles[name], 'tick': self.tick}
        if self.dual_active and self.starts and not self.down:
            report['role'] = 'active'
            return report
        if name == 'standby' and self.down:
            # The peer's source is dead: its pulls miss and its
            # convergence proof lapses, so it reports `degraded` — the
            # verdict a promote is refused on.
            report['sync'] = 'degraded' if not self.stays_converged \
                else {'tracking': {}}
            if self.self_promotes:
                report['role'] = 'active'
            return report
        if self.roles[name] == 'standby':
            report['sync'] = 'degraded' if (
                self.no_resync and self.starts) \
                else {self.syncs[name] or 'tracking': {}}
        return report

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _serve(self, name, method, route, url, body):
        if self.down and name == 'active':
            raise urllib.error.URLError('connection refused')
        if self.both_down or (self.down and name == 'standby'
                              and self.peer_silent):
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._report(name)
        if (method, route) == ('GET', '/snapshot'):
            if self.silent_snapshot and self.down:
                raise urllib.error.URLError('connection refused')
            return 200, {'tick': self.tick,
                         'io_health': {'failed_writes': 0},
                         'points': [{'point': POINT, 'direction': 'in',
                                     'sample': {'value': {'bool': False},
                                                'quality': 'good'}}]}
        if (method, route) == ('GET', '/journal'):
            return 200, self._served_journal(name)
        if (method, route) == ('POST', '/promote'):
            self.promotes += 1
            if (self.restore_stalls and self.promotes >= 2
                    and name == 'active'):
                self._raise(409, 'already_active')
            if self.roles[name] == 'active' and not self.down:
                self._raise(409, 'already_active')
            if name == 'standby' and self.down:
                if self.promote_admitted:
                    self.roles['standby'] = 'active'
                    self.owner = 'standby'
                    self._entry('standby', {'role_changed': {
                        'from': 'standby', 'to': 'promoting'}})
                    self._entry('standby', {'role_changed': {
                        'from': 'promoting', 'to': 'active'}})
                    return 200, {'role': 'promoting'}
                if self.promote_other:
                    self._raise(409, {'no_tracking_source': {}})
                self._raise(409, {'not_converged': {'sync': 'degraded'}})
            if self.roles[name] == 'active':
                self._raise(409, 'already_active')
            self.roles[name] = 'active'
            self.syncs[name] = None
            self.owner = name
            self._entry(name, {'role_changed': {
                'from': 'standby', 'to': 'promoting'}})
            self._entry(name, {'role_changed': {
                'from': 'promoting', 'to': 'active'}})
            if name == 'standby':
                # The resumed owner steps down in place and the standby
                # tracks the new stream.
                self.roles['active'] = 'standby'
                self.syncs['active'] = 'tracking'
                self.owner = 'standby'
                self._entry('active', {'role_changed': {
                    'from': 'demoting', 'to': 'standby'}})
            return 200, {'role': 'promoting'}
        if (method, route) == ('POST', '/demote'):
            if (self.roles[name] != 'active'
                    or self.demotes == self.demote_refuses_at):
                self._raise(409, {'not_active': {}})
            self.demotes += 1
            self.roles[name] = 'standby'
            self.syncs[name] = 'tracking'
            self.owner = 'standby'
            self._entry(name, {'role_changed': {
                'from': 'active', 'to': 'demoting'}})
            self._entry(name, {'role_changed': {
                'from': 'demoting', 'to': 'standby'}})
            return 200, {'role': 'demoting'}
        raise AssertionError('unexpected request %s %s' % (method, url))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        name = HOSTS.get(host)
        if name is None:
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        self._advance()
        return self._serve(name, method, route, url, body)


class DeadActiveRecoveryTests(unittest.TestCase):
    """scenario_dead_active_unconverged_recovery against the stubbed
    pair and the claim-enforcing plant: the wedge's unconverged walk,
    named promote refusals, kept-serving quiesced peer, standing dead
    claim, the restart-as-active re-claim, the one-active recovery
    window, the reconverged standby, and the launch-role restore —
    plus each named failure and inconclusive induction."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = DeadActivePlant()
        self.addCleanup(self.plant.close)
        self.feed = DeadActiveFeed(
            self.plant,
            Path(self.tmp.name) / 'controllers' / 'a' / 'journal.jsonl',
            Path(self.tmp.name) / 'controllers' / 'b' / 'journal.jsonl')
        self.addCleanup(self.feed.close)

    def _ctx(self, feed=None, **overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'plant_owner': {'active': DEAD_TOKEN,
                               'standby': PEER_TOKEN},
               'stop_controller': feed.stop_controller,
               'start_controller': feed.start_controller,
               'journal_files': {'active': str(feed.paths['active']),
                                 'standby': str(feed.paths['standby'])},
               'evidence_dir': str(self.evidence)}
        ctx.update(overrides)
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        defaults = {'POLL_INTERVAL': 0.001,
                    'DEAD_ACTIVE_POLL': 0.001,
                    'DEAD_ACTIVE_WEDGE': 0.5,
                    'DEAD_ACTIVE_RETURN': 0.5,
                    'DEAD_ACTIVE_RECONVERGE': 0.5,
                    'DEAD_ACTIVE_WINDOW': 0.5,
                    'DEAD_ACTIVE_SETTLE_DEADLINE': 0.5,
                    'DEAD_ACTIVE_FIELD_TICKS': 2,
                    'DEAD_ACTIVE_PROMOTES': 3}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_dead_active_unconverged_recovery(
                ctx or self._ctx(feed))

    def _read(self, ref):
        return json.loads((self.evidence.parent / ref).read_text())

    def test_registered_after_superseded_before_tune(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_superseded_restart),
            order.index(
                scenarios.scenario_dead_active_unconverged_recovery))
        self.assertLess(
            order.index(
                scenarios.scenario_dead_active_unconverged_recovery),
            order.index(scenarios.scenario_parameter_tune_carryover))

    def test_clean_run_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        self.assertEqual(self.feed.stops, 1)
        self.assertEqual(self.feed.starts, 1)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes() for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        root = Path(second_tmp.name)
        evidence2 = root / 'evidence'
        evidence2.mkdir()
        plant2 = DeadActivePlant()
        self.addCleanup(plant2.close)
        feed2 = DeadActiveFeed(
            plant2,
            root / 'controllers' / 'a' / 'journal.jsonl',
            root / 'controllers' / 'b' / 'journal.jsonl')
        self.addCleanup(feed2.close)
        ctx2 = self._ctx(feed2, evidence_dir=str(evidence2))
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_evidence_names_every_leg(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        refs = [entry['ref'] for entry in record['evidence']]
        for name in ('wedge', 'recovery', 'resync'):
            self.assertTrue(any(name in ref for ref in refs), name)
        wedge = self._read('evidence/dead-active-wedge.json')
        self.assertIn('degraded', wedge['syncs'])
        self.assertEqual(len(wedge['promotes']), 3)
        self.assertTrue(all(status == 409 for status, _body
                            in wedge['promotes']))
        self.assertTrue(all(count > 0 for count
                            in wedge['served'].values()))
        recovery = self._read('evidence/dead-active-recovery.json')
        self.assertEqual([len(poll['actives'])
                          for poll in recovery['polls']],
                         [1] * len(recovery['polls']))
        self.assertGreaterEqual(len(recovery['field_ticks']), 2)

    def test_the_pair_ends_on_its_launch_roles(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.roles['active'], 'active')
        self.assertEqual(self.feed.roles['standby'], 'standby')
        self.assertFalse(self.feed.down)

    # --- the inconclusive inductions -------------------------------

    def test_missing_lifecycle_actions_are_inconclusive(self):
        record = self.run_scenario(self._ctx(stop_controller=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('stop/start', record['detail'])
        record = self.run_scenario(self._ctx(start_controller=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_missing_plant_endpoint_is_inconclusive(self):
        record = self.run_scenario(self._ctx(plant=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('plant endpoint', record['detail'])

    def test_missing_monitors_are_inconclusive(self):
        record = self.run_scenario(self._ctx(active=None))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('monitor', record['detail'])

    def test_missing_owner_tokens_are_inconclusive(self):
        record = self.run_scenario(self._ctx(plant_owner={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('owner tokens', record['detail'])

    def test_missing_journals_are_inconclusive(self):
        record = self.run_scenario(self._ctx(journal_files={}))
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('journal-file', record['detail'])

    def test_unreachable_pair_is_inconclusive(self):
        self.feed.both_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])

    def test_unsettled_pair_is_failed(self):
        self.feed.never_settles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('launch shape', record['detail'])

    def test_unclaimed_field_at_bring_up_fails(self):
        self.feed.no_claim = True
        self.feed.plant.claim = None
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no writer claim', record['detail'])

    def test_foreign_named_claim_fails(self):
        self.feed.foreign_claim = True
        self.feed._plant_claim(PEER_TOKEN)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not the launched active', record['detail'])

    def test_stop_lever_failure_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('stop induction never completed', record['detail'])

    def test_start_lever_failure_is_inconclusive(self):
        self.feed.start_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start induction never completed', record['detail'])

    def test_unobserved_wedge_is_inconclusive(self):
        self.feed.peer_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered /role', record['detail'])

    # --- the wedge --------------------------------------------------

    def test_never_unconverged_fails(self):
        # A dead source the standby still reports converged on: its
        # stale convergence proof would admit a promotion.
        self.feed.stays_converged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never walked its sync', record['detail'])

    def test_standby_promoting_itself_fails(self):
        self.feed.self_promotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('left standby', record['detail'])

    def test_quiesced_peer_stops_serving_fails(self):
        self.feed.silent_snapshot = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped serving', record['detail'])

    def test_promote_admitted_fails(self):
        # A silent promotion over a dead source: the quiesced peer
        # leaves standby on a convergence proof it never held.
        self.feed.promote_admitted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('left standby', record['detail'])

    def test_promote_other_refusal_fails(self):
        self.feed.promote_other = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('must refuse it by name', record['detail'])

    def test_dead_claim_lapsed_fails(self):
        self.feed.claim_lapses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not refused', record['detail'])

    def test_dead_claim_moved_fails(self):
        self.feed.claim_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved off the dead owner', record['detail'])

    # --- the recovery ------------------------------------------------

    def test_resumed_never_owns_fails(self):
        # The relaunched container never took the field: the dead hold
        # survived the restart and nothing resumed as an active.
        self.feed.resume_demotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never resumed as active', record['detail'])

    def test_resumed_dual_active_fails(self):
        self.feed.dual_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('two peers reported active', record['detail'])

    def test_field_not_stepping_fails(self):
        self.feed.field_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('advanced its served field', record['detail'])

    def test_field_unfenced_after_recovery_fails(self):
        self.feed.probe_open = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped fencing', record['detail'])

    def test_field_claimed_by_another_fails(self):
        self.feed.probe_foreign = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not the resumed owner', record['detail'])

    def test_standby_never_reconverges_fails(self):
        self.feed.no_resync = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reconverged', record['detail'])

    def test_unreadable_journal_is_inconclusive(self):
        self.feed.journal_corrupts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreadable', record['detail'])

    def test_source_restart_is_recorded(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        resync = self._read('evidence/dead-active-resync.json')
        self.assertEqual(len(resync['source_restarted']), 1)
        self.assertEqual(resync['report']['sync'], {'tracking': {}})
        self.assertTrue(any('source_restarted' in observation
                            for observation in record['observations']))

    def test_missing_source_restart_is_not_faulted(self):
        # Where the stream's generation did not regress, the resync
        # record is absent by contract, not a violation.
        self.feed.no_source_restart = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        resync = self._read('evidence/dead-active-resync.json')
        self.assertEqual(resync['source_restarted'], [])

    # --- the restore -------------------------------------------------

    def test_restore_switch_refused_fails(self):
        self.feed.demote_refuses_at = 1
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restore switch', record['detail'])

    def test_restore_not_settling_fails(self):
        self.feed.restore_stalls = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('launch roles', record['detail'])


if __name__ == '__main__':
    unittest.main()