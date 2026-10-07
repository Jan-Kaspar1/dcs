"""The 2484_cyclic_failover_settle leg's scenario unit coverage — the
feed fake and TestCase class for scenario_cyclic_failover_settle, split
out per the leg-module convention (#940). The shared fakes and helpers
live in tests/qa_scenario_support.py.


The feed stages the leg's shape: the lane's device server serves the
run config's `sim-cyclic` document, a born pair stands on it — the
launch-active and the tracking member — and the documented
`POST /demote`/`POST /promote` move the roles between them. Each peer's
exchanges advance its own counters and latches its own inputs, and the
field's register census moves only while a peer publishes, so the leg
can read the transition off both monitors and the field. The fault
flags stage each named defect and each named instability, plus the
absent capabilities the leg declines on."""
import json
import tempfile
import unittest
from pathlib import Path

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


OWNER_SEAT = 'driven'
PEER_SEAT = 'foreign'
MISS_THRESHOLD = 3


class CtlAnswer:
    """The shipped control tool's own answer: the exit the leg
    classifies, with the printed response and its stderr."""

    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class CyclicFailoverFeed:
    """A stubbed rig for the cyclic demote/promote failover.

    Each born seat exchanges once per served snapshot: it advances its
    own attempted/succeeded counters and latches its own input samples
    at its own tick. Only the seat holding the claim publishes, so the
    field's register census moves while the active exchanges and stands
    still once the field moves to the other peer — which is what makes
    the field side readable across the transition. Every fault flag
    doctors one named defect or one named instability."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-d:5': OWNER_SEAT,
             'ctrl-f:7': PEER_SEAT}
    OWNER_TOKEN = 424247

    def __init__(self, model):
        self.model = Path(model)
        self.field = None
        self.seats = {}
        self.calls = []
        self.registers_tick = 40
        # Staging failures and absent surfaces.
        self.stage_fails = False
        self.launch_fails = False
        self.silent_rig = False
        self.not_cyclic = False
        self.no_outputs = False
        # The settle: a tracking member that never converges leaves the
        # promote with no converged target.
        self.no_converge = False
        # The switch refusals.
        self.refuse_demote = False
        self.refuse_promote = False
        self.raise_switch = False
        # The contract defects.
        self.no_promotion = False     # the promoted peer never goes active
        self.no_claim = False         # ... or never holds the claim
        self.no_resume = False        # ... or never resumes exchanging
        self.link_down = False
        self.skipped_boundary = False
        self.counted_failure = False
        self.degraded_latch = False
        self.unstamped = False
        self.frozen_field = False
        self.demote_kept = False      # the demoted peer stays active
        self.claim_kept = False       # ... or keeps the claim
        self.demoted_stalled = False  # ... or stops latching
        self.demoted_fenced = False   # ... or meets a fence
        self.demoted_failing = False  # ... or its census fails
        self.unjournaled = False
        self.unattributed = False
        self.unreadable_journal = False
        # The instabilities.
        self.starves = False
        self.field_silent = False
        self.no_restore = False

    # --- the staged documents ---------------------------------------

    def write_model(self):
        self.model.parent.mkdir(parents=True, exist_ok=True)
        document = {
            'version': 1,
            'devices': [{
                'id': 1, 'kind': 'sim-cyclic',
                'parameters': {'address': 'dcs-hw-qa-1-bus:9005',
                               'exchange_miss_threshold': MISS_THRESHOLD,
                               'stations': {'750-354': {'di1': 2, 'di2': 3,
                                                        'do1': 0,
                                                        'do2': 1}}},
                'channels': {'di1': {'direction': 'in',
                                     'value_type': 'bool'},
                             'di2': {'direction': 'in',
                                     'value_type': 'bool'},
                             'do1': {'direction': 'out',
                                     'value_type': 'bool'},
                             'do2': {'direction': 'out',
                                     'value_type': 'bool'}}}],
            'io_points': [{'id': 1, 'direction': 'in', 'value_type': 'bool',
                           'channel': {'device': 1, 'name': 'di1'}},
                          {'id': 2, 'direction': 'in', 'value_type': 'bool',
                           'channel': {'device': 1, 'name': 'di2'}},
                          {'id': 3, 'direction': 'out', 'value_type': 'bool',
                           'channel': {'device': 1, 'name': 'do1'}},
                          {'id': 4, 'direction': 'out', 'value_type': 'bool',
                           'channel': {'device': 1, 'name': 'do2'}}],
            'signals': [], 'components': [], 'connections': []}
        if self.not_cyclic:
            document['devices'][0]['kind'] = 'sim-bus'
        if self.no_outputs:
            for channel in ('do1', 'do2'):
                document['devices'][0]['channels'].pop(channel)
        self.model.write_text(json.dumps(document))

    # --- the shipped control tool's seam, faked ---------------------

    def ctl(self, *args):
        self.calls.append(('sim_bus_ctl',) + args)
        if args and args[0] == 'list':
            if self.field_silent or self.field is None:
                return CtlAnswer('', 1, 'connection refused')
            return CtlAnswer(json.dumps({'registers': [
                {'register': 0,
                 'sample': {'value': {'bool': False},
                            'quality': 'good',
                            'tick': self.registers_tick}}]}))
        return CtlAnswer('', 1, 'unknown command ' + repr(args))

    # --- the runner's device-server and born-seat levers, faked ------

    def start_device(self, fixture=None, timeout_ms=None):
        self.calls.append(('start_sim_bus_device', fixture))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        self.write_model()
        self.field = 'serving'
        self.registers_tick = 40
        return {'container': 'dcs-hw-qa-1-bus', 'device': 1, 'port': 9005,
                'address': 'dcs-hw-qa-1-bus:9005', 'model': str(self.model)}

    def stop_device(self):
        self.calls.append(('stop_sim_bus_device',))
        self.field = None

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby, document))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        assert remote is None, 'the cyclic model carries no --remote'
        assert document == str(self.model), \
            'the seat mounts the device server\'s staged document'
        active = seat == OWNER_SEAT
        self.seats[seat] = {
            'seat': seat, 'role': 'active' if active else 'standby',
            'sync': None if active or self.no_converge
            else {'tracking': {'aligned': 0}},
            'claim': 'held' if active else 'unclaimed',
            'token': self.OWNER_TOKEN, 'tick': 40, 'attempted': 40,
            'succeeded': 40, 'failed': 0, 'latched': 40,
            'writes': 1 if active else 0}
        return {'container': 'dcs-hw-qa-1-' + seat, 'seat': seat,
                'address': 'dcs-hw-qa-1-' + seat + ':8082', 'remote': None,
                'peer': peer, 'standby': standby, 'model': document,
                'monitor': 'http://' + _host(seat) + ':8082'}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seats.pop(seat, None)

    def state(self, seat):
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': seat in self.seats, 'exit': None, 'logs': '',
                'absent': seat not in self.seats}

    # --- the documented switch --------------------------------------

    def _journal(self, seat):
        """Append one role-change record to a seat's own journal file —
        the durable half the leg reads its attribution from."""
        if self.unreadable_journal:
            self.journals[seat].write_text('{ this line does not parse\n')
            return
        peer = self.seats[seat]
        self.lines[seat] += 1
        with self.journals[seat].open('a') as handle:
            handle.write(json.dumps({
                'run': 1, 'seq': self.lines[seat],
                'tick': peer['tick'],
                'event': {'role_changed': {'from': peer['previous'],
                                           'to': peer['role'],
                                           'origin': peer['origin']}}}) + '\n')
        peer['previous'] = peer['role']

    def _switch(self, seat, verb):
        if self.raise_switch and verb == 'promote':
            raise RuntimeError('the monitor never answered')
        peer = self.seats.get(seat)
        if peer is None:
            return 409, {'error': 'not_a_member'}
        if verb == 'demote':
            if self.refuse_demote:
                return 409, {'error': 'not_active'}
            if self.demote_kept:
                # The demotion is requested and acknowledged, and the
                # role never leaves active: two writers stand on the
                # device.
                peer['origin'] = 'qa-lane'
                return 200, {'role': 'demoting'}
            peer['previous'] = peer['role']
            peer['role'] = 'demoting'
            peer['tick'] += 1
            peer['origin'] = 'qa-lane'
            self._journal(seat)
            peer['role'] = 'standby'
            peer['sync'] = {'tracking': {'aligned': peer['tick']}}
            peer['claim'] = 'held' if self.claim_kept else 'unclaimed'
            peer['writes'] = 1 if self.claim_kept else 0
            self._journal(seat)
            return 200, {'role': 'demoting'}
        if self.refuse_promote:
            return 409, {'error': 'not_converged'}
        self.promotes = getattr(self, 'promotes', 0) + 1
        if self.no_restore and self.promotes > 1:
            # The restore's promote answers and never lands the active
            # role: the pair never reconverges to its launch posture.
            return 200, {'role': peer['role']}
        peer['previous'] = peer['role']
        peer['role'] = 'active' if not self.no_promotion else 'standby'
        peer['sync'] = None
        peer['claim'] = 'unclaimed' if self.no_claim else 'held'
        peer['writes'] = 0 if self.no_claim else 1
        peer['origin'] = 'qa-lane' if not self.unattributed else 'failover'
        if not self.unjournaled:
            self._journal(seat)
        # The peer the claim came from steps down: its claim releases and
        # its staged image stops publishing, unless the flags stage a
        # demotion that did not land.
        ex = next((other for name, other in self.seats.items()
                   if name != seat), None)
        if ex is not None and self.claim_kept:
            # The demoted peer's claim never released: two writers stand
            # on the device.
            changed = ex['role'] != 'standby'
            ex['role'] = 'standby'
            ex['sync'] = {'tracking': {'aligned': ex['tick']}}
            ex['claim'] = 'held'
            ex['writes'] = 1
            ex['origin'] = 'failover'
            if not self.unjournaled and changed:
                self._journal(ex['seat'])
        elif ex is not None and not self.demote_kept \
                and ex['role'] != 'standby':
            ex['previous'] = ex['role']
            ex['role'] = 'standby'
            ex['sync'] = {'tracking': {'aligned': ex['tick']}}
            ex['claim'] = 'unclaimed'
            ex['writes'] = 0
            ex['origin'] = 'failover'
            # A peer that was already a standby records no role change:
            # the journal carries transitions, not no-ops.
            if not self.unjournaled and ex['previous'] != 'standby':
                self._journal(ex['seat'])
        return 200, {'role': peer['role']}

    # --- the scans each seat's snapshots drive ----------------------

    def _exchange(self, seat):
        peer = self.seats.get(seat)
        if peer is None:
            return None
        peer['tick'] += 1
        if peer['role'] == 'active' and self.no_resume:
            return peer
        peer['attempted'] += 1
        if self.skipped_boundary and peer['role'] == 'active':
            # A boundary the exchange never completed: the counter
            # records the attempt, the success does not follow.
            return peer
        peer['succeeded'] += 1
        peer['latched'] = peer['tick']
        if self.counted_failure and peer['role'] == 'active':
            peer['failed'] += 1
        if peer['role'] == 'standby':
            if self.demoted_fenced:
                peer['link'] = 'disconnected'
            if self.demoted_failing:
                peer['failed'] += 2
            if self.demoted_stalled:
                peer['attempted'] -= 1
                peer['succeeded'] -= 1
        # The field's registers move while a peer publishes.
        if peer['role'] == 'active' and peer['claim'] == 'held':
            if not self.frozen_field:
                self.registers_tick = peer['tick']
        return peer

    def _snapshot(self, seat):
        peer = self.seats.get(seat)
        if self.starves and peer is not None and peer['role'] == 'active':
            return None
        peer = self._exchange(seat)
        if peer is None:
            return None
        degraded = self.degraded_latch and peer['role'] == 'active'
        points = []
        for pid in (1, 2):
            sample = {'value': False,
                      'quality': 'bad' if degraded else 'good',
                      'tick': peer['latched']}
            if self.unstamped:
                sample.pop('tick')
            points.append({'point': pid, 'sample': sample})
        return {'tick': peer['tick'], 'points': points,
                'io_health': {'failed_exchanges': peer['failed'],
                              'failed_reads': 0, 'failed_writes': 0,
                              'consecutive_failures': 0,
                              'driver': {'link': 'disconnected'
                                         if self.link_down
                                         or self.demoted_fenced
                                         and peer['role'] == 'standby'
                                         else 'connected',
                                         'last_error': None,
                                         'exchange': {
                                             'attempted': peer['attempted'],
                                             'succeeded': peer['succeeded'],
                                             'working_counter_mismatches': 0,
                                             'missed_deadlines': 0,
                                             'last_exchange_tick':
                                             peer['tick']}}}}

    def _role(self, seat):
        peer = self.seats.get(seat)
        if peer is None:
            return None
        return {'role': peer['role'], 'tick': peer['tick'],
                'field_claim': peer['claim'], 'sync': peer['sync']}

    # --- the monitor channel — replaces scenarios.http_json ---------

    def http_json(self, method, url, body=None, timeout=10):
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if host not in self.HOSTS:
            raise urllib.error.URLError('connection refused')
        seat = self.HOSTS[host]
        if (method, route) == ('POST', '/demote'):
            return self._switch(seat, 'demote')
        if (method, route) == ('POST', '/promote'):
            return self._switch(seat, 'promote')
        if seat not in self.seats:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(seat) or {'role': None}
        if (method, route) == ('GET', '/snapshot'):
            served = self._snapshot(seat)
            if served is None:
                raise urllib.error.URLError('connection refused')
            return 200, served
        raise AssertionError('unexpected request %s %s' % (method, url))


def _host(seat):
    """The rig-bridge host a born seat's monitor URL names."""
    for host, key in CyclicFailoverFeed.HOSTS.items():
        if key == seat:
            return host
    return seat


class CyclicFailoverSettleTests(unittest.TestCase):
    """The leg's pass, failure, and inconclusive outcomes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.model = self.root / 'sim-bus' / 'model.json'
        self.feed = CyclicFailoverFeed(self.model)
        # Each born seat's --journal-file exists from the launch: the
        # leg reads the durable record the switch writes into it.
        self.journals = {OWNER_SEAT: self.root / 'owner.jsonl',
                         PEER_SEAT: self.root / 'peer.jsonl'}
        for path in self.journals.values():
            path.write_text('')
        self.feed.journals = self.journals
        # The per-seat record counter the journal's own `seq` advances.
        self.lines = {OWNER_SEAT: 0, PEER_SEAT: 0}
        self.feed.lines = self.lines

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {
            'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
            'revised': 'http://ctrl-c:3', 'driven': 'http://ctrl-d:5',
            'foreign': 'http://ctrl-f:7',
            'evidence_dir': str(self.evidence),
            'sim_bus_device': {
                'device': 1, 'port': 9005,
                'model_fixture': 'crates/dcs-demo/fixtures/two_kinds_bus.json',
                'cyclic_model': 'crates/dcs-demo/fixtures/cyclic.json'},
            'sim_bus_ctl': feed.ctl,
            'start_sim_bus_device': feed.start_device,
            'stop_sim_bus_device': feed.stop_device,
            'start_born_controller': feed.start_controller,
            'stop_born_controller': feed.stop_controller,
            'born_controller_state': feed.state,
            'journal_files': {key: str(path)
                              for key, path in self.journals.items()},
        }

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx(feed)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'CFS_LAUNCH_BOUND', 1.0), \
                patch.object(scenarios, 'CFS_SWITCH_BOUND', 1.0), \
                patch.object(scenarios, 'CFS_WINDOW_BOUND', 1.0), \
                patch.object(scenarios, 'CFS_WINDOW_POLL', 0.001), \
                patch.object(scenarios, 'CFS_SETTLE_POLL', 0.001), \
                patch.object(scenarios, 'CFS_RESTORE_BOUND', 1.0):
            return scenarios.scenario_cyclic_failover_settle(ctx)

    def _pass(self, number):
        return json.loads(
            (self.evidence / ('cyclic-failover-settle-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_cyclic_failover_settle,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('cyclic-failover-settle'),
            scenarios.scenario_cyclic_failover_settle)
        order = list(scenarios.SCENARIOS)
        mine = order.index(scenarios.scenario_cyclic_failover_settle)
        self.assertLess(order.index(
            scenarios.scenario_cyclic_exchange_failure_legs), mine)
        self.assertLess(mine, order.index(
            scenarios.scenario_reclaim_convergence_gate))

    def test_clean_passes_validate_and_tear_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = sorted(entry['ref'] for entry in record['evidence'])
        self.assertEqual(
            refs,
            ['evidence/cyclic-failover-settle-pass-1.json',
             'evidence/cyclic-failover-settle-pass-2.json'])
        first = self._pass(1)
        self.assertEqual(first['violations'], {})
        self.assertEqual(
            first['digest'],
            {'promotion': 'resumed', 'boundaries': 'once',
             'latching': 'continuous', 'field': 'republished',
             'demoted': 'census-only', 'journal': 'recorded',
             'rig': 'restored'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # Both documented steps answered, and the roles moved.
        self.assertEqual(first['demote']['status'], 200)
        self.assertEqual(first['promote']['status'], 200)
        self.assertEqual(first['promoted_after']['role'], 'active')
        self.assertEqual(first['promoted_after']['claim'], 'held')
        self.assertEqual(first['owner_after']['role'], 'standby')
        self.assertEqual(first['owner_after']['claim'], 'unclaimed')
        # The promoted peer resumed at the exchange boundary with one
        # accounted boundary per exchange, and the demoted peer kept
        # latching without a fence.
        self.assertGreater(first['window']['io']['attempted'],
                           first['promoted_io']['attempted'])
        self.assertEqual(first['window']['io']['attempted']
                         - first['window']['io']['succeeded'], 0)
        self.assertEqual(first['window']['io']['failed_exchanges'], 0)
        self.assertEqual(first['owner_window']['io']['link'], 'connected')
        self.assertGreater(first['owner_window']['io']['attempted'],
                           first['owner_io']['attempted'])
        # The field's own registers moved across the transition.
        self.assertNotEqual(first['registers']['before']['samples'],
                            first['registers']['after']['samples'])
        # Both peers journaled their own half of the transition, with
        # the request's attribution, and the evidence names the files.
        self.assertEqual(first['journal']['paths'],
                         {OWNER_SEAT: str(self.journals[OWNER_SEAT]),
                          PEER_SEAT: str(self.journals[PEER_SEAT])})
        self.assertIn(('standby', 'active'),
                      [tuple(entry) for entry
                       in first['journal']['promoted']['walk']])
        self.assertEqual(first['journal']['promoted']['driven'][0]['origin'],
                         'qa-lane')
        self.assertIn(('demoting', 'standby'),
                      [tuple(entry) for entry
                       in first['journal']['demoted']['walk']])
        # The pair is restored to its launch roles and the rig swept.
        self.assertIsNone(first['restore'])
        self.assertEqual(first['rig'], {'seats': {OWNER_SEAT: True,
                                                  PEER_SEAT: True},
                                        'device_error': None})
        self.assertIn(('stop_sim_bus_device',), self.feed.calls)

    def test_two_runs_produce_identical_digests(self):
        first = self.run_scenario()
        self.assertEqual(first['outcome'], 'passed', first)
        second = self.run_scenario()
        self.assertEqual(second['outcome'], 'passed', second)
        self.assertEqual(self._pass(1)['digest'], self._pass(2)['digest'])

    def test_a_promotion_that_took_no_field_fails(self):
        self.feed.no_promotion = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promote-not-landed', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_promotion_that_took_no_claim_fails(self):
        self.feed.no_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-not-taken', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_promotion_that_published_nothing_fails(self):
        self.feed.no_resume = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-resume', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_promoted_peer_off_the_link_fails(self):
        self.feed.link_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promoted-link-down', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_skipped_boundary_fails(self):
        self.feed.skipped_boundary = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('boundary-skipped', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_switchover_counted_a_failure_fails(self):
        self.feed.counted_failure = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('switchover-counted-a-failure',
                      self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_degraded_latching_across_the_switch_fails(self):
        self.feed.degraded_latch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('latching-degraded', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unstamped_latched_sample_fails(self):
        self.feed.unstamped = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('latching-unstamped', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_frozen_field_fails(self):
        self.feed.frozen_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field-frozen', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_demotion_that_never_landed_fails(self):
        self.feed.demote_kept = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demote-not-landed', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_demotion_that_kept_the_claim_fails(self):
        self.feed.claim_kept = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim-not-released', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_demoted_peer_that_stopped_latching_fails(self):
        self.feed.demoted_stalled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demoted-stopped-latching',
                      self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_demoted_peer_behind_a_fence_fails(self):
        self.feed.demoted_fenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demoted-fenced', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_failing_demoted_census_fails(self):
        self.feed.demoted_failing = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demoted-counted-a-failure',
                      self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unjournaled_promotion_fails(self):
        self.feed.unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promote-unjournaled', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unattributed_promotion_fails(self):
        self.feed.unattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promote-unattributed', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unjournaled_demotion_fails(self):
        feed = self.feed
        original = feed._journal

        def journal(seat):
            if seat == OWNER_SEAT:
                return
            return original(seat)
        feed._journal = journal
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demote-unjournaled', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_point_wise_field_is_inconclusive(self):
        self.feed.not_cyclic = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('sim-cyclic', record['detail'])
        report.validate_scenario(record)

    def test_an_output_less_field_is_inconclusive(self):
        self.feed.no_outputs = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('output channel', record['detail'])
        report.validate_scenario(record)

    def test_absent_device_server_is_inconclusive(self):
        ctx = self._ctx()
        ctx['sim_bus_device'] = None
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('sim_bus_device', record['detail'])
        report.validate_scenario(record)

    def test_an_unsettled_pair_is_inconclusive(self):
        feed = self.feed
        original = feed.start_controller

        def start(seat, remote, peer=None, standby=None, document=None):
            # The tracking member never converges: the promote this leg
            # drives has no converged target.
            launch = original(seat, remote, peer, standby, document)
            if seat == PEER_SEAT:
                feed.no_converge = True
            return launch
        feed.start_controller = start
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking member never converged', record['detail'])
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        for key in ('start_sim_bus_device', 'stop_sim_bus_device',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state', 'sim_bus_ctl'):
            ctx = self._ctx()
            ctx[key] = None
            record = self.run_scenario(ctx=ctx)
            self.assertEqual(record['outcome'], 'inconclusive', record)
            self.assertIn(key, record['detail'])
            report.validate_scenario(record)

    def test_a_refused_demote_is_nondeterministic(self):
        self.feed.refuse_demote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        self.assertIn('demote-refused', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_refused_promote_is_nondeterministic(self):
        self.feed.refuse_promote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        self.assertIn('promote-refused', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_starved_window_is_nondeterministic(self):
        self.feed.starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        self.assertIn('window-unreadable', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unreadable_field_is_nondeterministic(self):
        self.feed.field_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        self.assertIn('field-unreadable', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unreadable_journal_is_nondeterministic(self):
        self.feed.unreadable_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        self.assertIn('journal-unreadable', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_switch_call_that_raised_is_nondeterministic(self):
        self.feed.raise_switch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        self.assertIn('switch', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unrestored_pair_is_nondeterministic(self):
        self.feed.no_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_a_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_a_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_a_silent_rig_is_inconclusive(self):
        # A rig whose monitors answer nothing never brings the pair up,
        # so there is no settled pair to switch over: the leg declines
        # rather than reading a transition out of a rig that never ran.
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record['detail'])
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        with patch.object(scenarios, '_cfs_digest',
                          side_effect=[{'promotion': 'resumed'},
                                       {'promotion': 'stalled'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-nondeterministic'), record['detail'])
        self.assertIn('digests', record['detail'])
        report.validate_scenario(record)

    def test_an_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_cfs_self_check',
                          return_value=['a planted negative slipped']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'cyclic-failover-settle-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._cfs_self_check(), [])
