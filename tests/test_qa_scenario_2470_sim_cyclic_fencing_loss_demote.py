"""The 2470_sim_cyclic_fencing_loss_demote leg's scenario unit
coverage — the feed fake and TestCase class for
scenario_sim_cyclic_fencing_loss_demote, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py.


The feed stages the leg's shape: the lane's device server serves the
run's staged sim-cyclic model, the born-seat launches stand a pair on
it (the launch-active seat holding the device claim, the other member
tracking it), and restart_device() drops every attachment's control
connection so the connection-bound claim releases to nobody — after
which the promote lands the standby's claim and the ex-owner's next
staged exchange meets the fence, walks demoting to standby on the
fenced origin, journals one field_claim_lost attributed to the promoted
peer's token, and settles on census-only exchanges. Every transition
keys off the leg's lever calls so two passes emit identical digests; the
fault flags stage each named defect, each nondeterministic surface, and
each pre-contract shape the leg inconcludes on."""
import json
import unittest
import urllib.error
from pathlib import Path

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


# The staged device model the lane's server serves: one sim-cyclic
# device carrying an input and an output channel — the output is what
# the scan stages into the image the field fences.
CYCLIC_MODEL = {
    'version': 1,
    'devices': [{
        'id': 1, 'kind': 'sim-cyclic',
        'channels': {'di1': {'direction': 'in', 'value_type': 'bool'},
                     'do1': {'direction': 'out', 'value_type': 'bool'}},
        'parameters': {'address': 'dcs-hw-qa-1-bus:9005',
                       'exchange_miss_threshold': 3}}]}

# The src-relative fixture path the run config's sim_bus_device block
# names for the cyclic model — the leg reads it off the spec and hands
# it to the server launch.
CYCLIC_MODEL_FIXTURE = 'crates/dcs-demo/fixtures/cyclic_stub.json'


def _model_document(kind, outputs):
    """The staged model a fault flag asks for: the point-wise kind, or
    the cyclic kind with nothing for the scan to stage."""
    document = json.loads(json.dumps(CYCLIC_MODEL))
    device = document['devices'][0]
    device['kind'] = kind
    for channel in list(device['channels']):
        if device['channels'][channel]['direction'] == 'out' \
                and channel not in outputs:
            del device['channels'][channel]
    return document


class CyclicFeed:
    """A stubbed rig for the sim-cyclic fencing-loss demotion leg.

    The device server answers a claim probe with whoever holds the
    claim — the launch-active seat once the pair settles, nobody after
    restart_device() drops every control connection, the promoted peer
    once POST /promote lands its claim. The ex-owner's next staged
    exchange then meets the fence: its role walks active->demoting->
    standby on the fenced origin, one field_claim_lost naming the
    promoted peer's token lands on its journal, and its exchanges keep
    completing census-only. The fault flags doctor each named shape:
    the recorded defect signature (the ex-owner never demotes and its
    io_health never names a fenced exchange — the pre-contract
    revision's inconclusive shape), the issue's own doctored negatives
    (the ex-owner still active past the bound beside its promoted peer,
    two peers reporting active at one poll), the unjournaled or
    unattributed demotion, the stalled field, the ex-owner that keeps
    fencing, the pair that never reconverges, the ex-owner process that
    exits, and the pair instabilities."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-d:5': 'driven', 'ctrl-foreign:6': 'foreign'}
    OWNER_TOKEN = 424247
    PEER_TOKEN = 424246

    def __init__(self, model, journal):
        self.model = Path(model)
        self.owner_journal = Path(journal)
        self.peer_journal = Path(journal).with_name('peer-journal.jsonl')
        for path in (self.owner_journal, self.peer_journal):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('')
        self.deployed = {'active': 900, 'standby': 900}
        self.field = None        # None | 'serving' | 'severed'
        self.seats = {}          # seat -> the launched peer's state
        self.promoted = False    # the promote landed its claim
        self.moved = False       # the deployed pair's post-stage disturbance
        self.calls = []
        # Staging failures.
        self.stage_fails = False        # start_sim_bus_device raises
        self.launch_fails = False       # start_born_controller raises
        self.sever_fails = False        # restart_sim_bus_device raises
        self.promote_refused = False    # the promote answers 409
        self.silent_rig = False         # every monitor refuses
        self.state_fails = False        # born_controller_state raises
        self.starves = False            # the ex-owner's monitor goes quiet
        self.tear_journal = False       # the journal file tears mid-read
        # The pre-contract and absent-capability shapes.
        self.not_cyclic = False         # the staged model is point-wise
        self.no_outputs = False         # the cyclic device stages nothing
        self.unsettled = False          # the launch-active never claims
        self.no_tracking = False        # the peer never converges
        self.unreleased = False         # the claim survives the sever
        self.predates = False           # the ex-owner never demotes
        # The contract defects the issue names.
        self.stays_active = False       # active past the bound, fenced
        self.delayed = False            # the demotion lands a poll late
        self.silent_journal = False     # the demotion never journals
        self.request_origin = False     # the walk's origin is 'request'
        self.no_claim_loss = False      # the loss record never lands
        self.two_claim_losses = False   # a second loss record lands
        self.unattributed = False       # the loss names no claimant
        self.wrong_claimant = False     # the loss names the wrong token
        self.field_stalled = False      # the promoted peer's exchange dies
        self.still_fenced = False       # the demoted peer keeps fencing
        self.never_restored = False     # the restore never converges
        self.restarted = False          # a second run boundary lands
        self.owner_exits = False        # the ex-owner's process exits
        self.dishonest_sync = False     # the demoted peer reports 'healthy'
        # The deployed pair's instabilities.
        self.pair_moves = False
        self.pair_wedged = False

    # --- the run config's staged document ---------------------------

    def _stage_model(self):
        kind = 'sim-bus' if self.not_cyclic else 'sim-cyclic'
        outputs = set() if self.no_outputs else {'do1'}
        self.model.parent.mkdir(parents=True, exist_ok=True)
        self.model.write_text(json.dumps(_model_document(kind, outputs)))

    # --- the runner's register-protocol levers, faked ---------------

    def start_device(self, fixture=None):
        self.calls.append(('start_sim_bus_device', fixture))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        assert fixture == CYCLIC_MODEL_FIXTURE, \
            'the leg stages the spec\'s cyclic_model, got ' \
            + repr(fixture)
        self._stage_model()
        self.field = 'serving'
        # The deployed pair's disturbance lands with the leg's own
        # staging, past the settled posture the leg frames it in.
        if self.pair_moves:
            self.moved = True
        return {'container': 'dcs-hw-qa-1-bus', 'device': 1, 'port': 9005,
                'address': 'dcs-hw-qa-1-bus:9005',
                'model': str(self.model)}

    def restart_device(self):
        self.calls.append(('restart_sim_bus_device',))
        if self.sever_fails:
            raise RuntimeError('docker restart failed')
        # Every attachment's control connection drops: the
        # connection-bound claim releases with its dead holders.
        self.field = 'severed'
        if not self.unreleased:
            for seat in self.seats.values():
                seat['claim'] = False
                seat['claimed_view'] = 'unclaimed'

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
        # A born launch is cold: the runner-owned journal resets with
        # the container, so each pass's file records one lifetime.
        path = (self.owner_journal if seat == 'driven'
                else self.peer_journal)
        path.write_text('')
        self._boundary(path)
        owns = not standby and not self.unsettled
        self.seats[seat] = {
            'seat': seat, 'role': 'standby' if standby else 'active',
            'token': self.OWNER_TOKEN if seat == 'driven'
                     else self.PEER_TOKEN,
            'tracking': bool(standby), 'claim': owns,
            'claimed_view': 'held' if owns else None,
            'sync': ('degraded' if self.no_tracking else
                     {'tracking': {'aligned': 100}}) if standby
                    else None,
            'tick': 100, 'pending': True, 'fenced': False,
            'exchanges': 10, 'failed': 0, 'streak': 0, 'demoted': False,
            'delayed': False, 'exited': False, 'exit': None}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seats.pop(seat, None)

    def state(self, seat):
        if self.state_fails:
            raise RuntimeError('docker inspect failed')
        peer = self.seats.get(seat)
        if peer is None:
            return {'container': 'dcs-hw-qa-1-' + seat, 'running': False,
                    'exit': None, 'logs': '', 'absent': True}
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': not peer['exited'], 'exit': peer['exit'],
                'logs': '', 'absent': False}

    # --- the seats' durable journals ---------------------------------

    def _write(self, path, record):
        with path.open('a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _boundary(self, path):
        self._write(path, {'run_boundary': {'run': 1, 'tick': 0}})

    def _journal(self, path, event, tick=0):
        self._write(path, {'entry': {'seq': 0, 'tick': tick,
                                     'event': event}})

    def _demotion_journal(self):
        """The ex-owner's durable demotion: the fenced role walk beside
        the single claim loss on its own journal file."""
        if self.silent_journal:
            return
        origin = 'request' if self.request_origin else 'fenced'
        self._journal(self.owner_journal, {'role_changed': {
            'from': 'active', 'to': 'demoting', 'origin': origin}})
        if not self.no_claim_loss:
            claimant = None
            if not self.unattributed:
                claimant = (self.OWNER_TOKEN if self.wrong_claimant
                            else self.PEER_TOKEN)
            for _ in range(2 if self.two_claim_losses else 1):
                self._journal(self.owner_journal, {'field_claim_lost': {
                    'point': 3, 'claimant': claimant}})
        self._journal(self.owner_journal, {'role_changed': {
            'from': 'demoting', 'to': 'standby', 'origin': origin}})
        if self.restarted:
            self._boundary(self.owner_journal)
        if self.tear_journal:
            self._write(self.owner_journal, {'torn': True})

    # --- the field's arbitration and the scans that read it ----------

    def _claimant(self, seat):
        """The standing claim's owner token: the other seat's while it
        holds the device, nobody on the open field."""
        other = 'foreign' if seat == 'driven' else 'driven'
        rival = self.seats.get(other) or {}
        return rival.get('token') if rival.get('claim') else None

    def _scan(self, seat):
        """One scan of a launched seat: the exchange, then the role
        machine's reading of what the field answered."""
        peer = self.seats.get(seat)
        if peer is None or peer['exited']:
            return None
        peer['tick'] += 1
        if peer['role'] == 'promoting':
            peer['role'] = 'active'
            peer['claim'] = True
            peer['claimed_view'] = 'held'
            self._journal(self.peer_journal, {'role_changed': {
                'from': 'promoting', 'to': 'active', 'origin': 'request'}})
        elif peer['role'] == 'demoting':
            peer['role'] = 'standby'
            # The release hook drops the staged output image: the
            # demoted run's exchanges go census-only from here.
            peer['pending'] = False
            peer['claim'] = False
            peer['claimed_view'] = None
            self._journal(self.peer_journal, {'role_changed': {
                'from': 'demoting', 'to': 'standby',
                'origin': 'request'}})
        peer['exchanges'] += 1
        if self.field_stalled and seat == 'foreign' \
                and peer['role'] == 'active':
            peer['exchanges'] = 0
            peer['failed'] = 0
            peer['streak'] = 4
        elif peer['role'] == 'active' \
                and self._claimant(seat) is not None:
            peer['failed'] += 1
            peer['streak'] += 1
            self._fenced(peer)
        elif self.still_fenced and seat == 'driven' and peer['fenced']:
            # The demoted ex-owner's staged image was never released, so
            # its exchanges keep meeting the standing claim.
            peer['failed'] += 1
            peer['streak'] += 1
        else:
            peer['streak'] = 0
        if peer['role'] == 'standby' and not peer['tracking']:
            if self.never_restored:
                peer['sync'] = 'degraded'
            else:
                peer['sync'] = {'tracking': {'aligned': peer['tick']}}
                peer['tracking'] = True
        if self.dishonest_sync and seat == 'driven' \
                and peer['role'] == 'standby':
            peer['sync'] = 'healthy'
        if self.owner_exits and seat == 'driven' and peer['fenced'] \
                and peer['role'] == 'standby':
            peer['exited'] = True
            peer['exit'] = 1
        return peer

    def _fenced(self, peer):
        """The refused exchange this run stages: the fencing mark that
        demotes the ex-owner in place — or the recorded defect shapes
        where it does not."""
        if self.predates:
            # The pre-fix shape: the refused image exchange reaches
            # io_health as a point-level transport verdict, never as a
            # fenced mutation, so the claim-loss mark never fires.
            return
        peer['fenced'] = True
        if self.stays_active or peer['demoted']:
            return
        if self.delayed and not peer['delayed']:
            # The demotion one poll late: the promoted peer settles
            # active beside the still-active ex-owner.
            peer['delayed'] = True
            return
        peer['demoted'] = True
        peer['role'] = 'demoting'
        self._demotion_journal()

    def _role(self, seat):
        peer = self._scan(seat)
        if peer is None:
            return None
        report = {'role': peer['role'], 'tick': peer['tick'],
                  'field_claim': peer['claimed_view']}
        if peer['role'] != 'active':
            report['sync'] = peer['sync']
        return report

    def _io(self, seat):
        """The seat's served io_health in the wire shape the leg
        normalizes: the boundary counters, the transport's link
        verdict, the cyclic exchange counters, and the most recent
        boundary failure's named error — `fenced` once the field
        refused this run's image, `disconnected` while only the
        transport is down."""
        peer = self.seats.get(seat)
        if peer is None:
            return None
        error = None
        if peer['failed']:
            error = 'fenced' if peer['fenced'] else 'disconnected'
        return {
            'failed_exchanges': peer['failed'], 'failed_writes': 0,
            'consecutive_failures': peer['streak'],
            'last_error': ({'tick': peer['tick'], 'point': 3,
                            'direction': 'In', 'error': {error: 3}}
                           if error else None),
            'driver': {
                'link': ('disconnected' if peer['streak'] else 'connected'),
                'last_error': None,
                'exchange': {
                    'attempted': peer['exchanges'],
                    'succeeded': peer['exchanges'] - peer['failed'],
                    'working_counter_mismatches': 0,
                    'last_exchange_tick': peer['tick']}}}

    def _deployed(self, host):
        key = self.HOSTS[host]
        tick = self.deployed[key]
        if not self.pair_wedged or key == 'standby':
            self.deployed[key] += 1
        role = 'active' if key == 'active' else 'standby'
        report = {'role': 'active' if self.moved else role, 'tick': tick}
        if report['role'] != 'active':
            report['sync'] = {'tracking': {'aligned': tick}}
        return report

    # --- the monitor channel — replaces scenarios.http_json ---------

    def http_json(self, method, url, body=None, timeout=10):
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if host not in self.HOSTS:
            raise urllib.error.URLError('connection refused')
        seat = self.HOSTS[host]
        if seat in ('active', 'standby'):
            if (method, route) == ('GET', '/role'):
                return 200, self._deployed(host)
            raise AssertionError('unexpected request %s %s' % (method, url))
        if seat not in self.seats:
            raise urllib.error.URLError('connection refused')
        if self.starves and self.promoted and seat == 'driven':
            raise urllib.error.URLError('timed out')
        if (method, route) == ('GET', '/role'):
            report = self._role(seat)
            if report is None:
                # A process that stopped serving refuses the
                # connection, exactly as a dead container does.
                raise urllib.error.URLError('connection refused')
            return 200, report
        if (method, route) == ('GET', '/snapshot'):
            if self._scan(seat) is None:
                raise urllib.error.URLError('connection refused')
            return 200, {'tick': self.seats[seat]['tick'], 'points': [],
                         'io_health': self._io(seat)}
        if method == 'POST' and route in ('/promote', '/demote'):
            return self._switch(seat, route)
        raise AssertionError('unexpected request %s %s' % (method, url))

    def _switch(self, seat, route):
        """POST /promote and /demote on a launched seat: the promote
        takes the device claim — or is refused on an unconverged peer —
        and the demote stands the owner down."""
        peer = self.seats[seat]
        if peer['exited']:
            raise urllib.error.URLError('connection refused')
        if route == '/promote':
            if self.promote_refused or not peer['tracking']:
                return 409, {'error': 'not_converged'}
            peer['role'] = 'promoting'
            peer['claim'] = True
            peer['claimed_view'] = 'held'
            self.promoted = True
            self._journal(self.peer_journal, {'role_changed': {
                'from': 'standby', 'to': 'promoting',
                'origin': 'request'}})
            return 200, {'role': 'promoting'}
        peer['role'] = 'demoting'
        return 200, {'role': 'demoting'}


class CyclicFencingLossTests(unittest.TestCase):
    """scenario_sim_cyclic_fencing_loss_demote against the stubbed rig:
    the ex-owner loses the connection-bound claim to the sever, the
    promoted peer's claim fences its next staged exchange, and the
    ex-owner demotes through the named path with one attributed claim
    loss while the pair reconverges to its launch roles — two passes,
    identical digests. Each fault flag stages a named failure, a
    nondeterministic surface, or a pre-contract shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'driven-journal.jsonl'
        self.model = Path(self.tmp.name) / 'sim-bus' / 'model.json'
        self.feed = CyclicFeed(self.model, self.journal)

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'driven': 'http://ctrl-d:5',
                'foreign': 'http://ctrl-foreign:6',
                'evidence_dir': str(self.evidence),
                'sim_bus_device': {'device': 1, 'port': 9005,
                                   'cyclic_model': CYCLIC_MODEL_FIXTURE},
                'start_sim_bus_device': feed.start_device,
                'restart_sim_bus_device': feed.restart_device,
                'stop_sim_bus_device': feed.stop_device,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state,
                'plant_owner': {'driven': CyclicFeed.OWNER_TOKEN,
                                'foreign': CyclicFeed.PEER_TOKEN},
                'journal_files': {'driven': str(feed.owner_journal),
                                  'foreign': str(feed.peer_journal)}}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'LAUNCH_BOUND', 0.2), \
                patch.object(scenarios, 'FLAP_BOUND', 0.2), \
                patch.object(scenarios, 'DEMOTE_BOUND', 0.2), \
                patch.object(scenarios, 'RESTORE_BOUND', 0.2), \
                patch.object(scenarios, 'STEP_WINDOW', 0.0), \
                patch.object(scenarios, 'WATCH_POLL', 0.001), \
                patch.object(scenarios, 'SETTLE_POLL', 0.001):
            return scenarios.scenario_sim_cyclic_fencing_loss_demote(
                ctx or self._ctx(feed))

    def _pass(self, number):
        return json.loads(
            (self.evidence / ('sim-cyclic-fencing-loss-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_sim_cyclic_fencing_loss_demote,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('sim-cyclic-fencing-loss-demote'),
            scenarios.scenario_sim_cyclic_fencing_loss_demote)
        order = list(scenarios.SCENARIOS)
        mine = order.index(scenarios.scenario_sim_cyclic_fencing_loss_demote)
        self.assertLess(
            order.index(scenarios.scenario_ownerless_remote_backoff), mine)
        self.assertLess(
            order.index(scenarios.scenario_doomed_startup_claim), mine)
        self.assertLess(mine, order.index(scenarios.scenario_scan_batch_bound))

    def test_clean_passes_validate_and_tear_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/sim-cyclic-fencing-loss-pass-1.json',
             'evidence/sim-cyclic-fencing-loss-pass-2.json'])
        first = self._pass(1)
        self.assertEqual(first['violations'], {})
        self.assertEqual(first['digest'],
                         {'demotion': 'fenced', 'active': 'single',
                          'journal': 'attributed', 'field': 'stepping',
                          'pair': 'restored', 'verdict': 'fenced'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # The demotion's durable half: the fenced role walk beside one
        # claim loss attributed to the promoted peer's own token.
        walk = first['journal']['walk']
        self.assertIn(['active', 'demoting', 'fenced'], walk)
        self.assertIn(['demoting', 'standby', 'fenced'], walk)
        self.assertEqual(first['journal']['losses'],
                         [{'point': 3, 'claimant': CyclicFeed.PEER_TOKEN}])
        # One active peer at every poll after the promotion.
        self.assertEqual([poll['active'] for poll in first['watch']['polls']],
                         [1, 1])
        # Every pass ends torn down — both seats and the device server
        # removed, so the legs behind this one find them free.
        self.assertEqual(self.feed.seats, {})
        self.assertIsNone(self.feed.field)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_sim_bus_device'), 2)
        self.assertEqual(kinds.count('stop_sim_bus_device'), 2)
        self.assertEqual(kinds.count('restart_sim_bus_device'), 2)
        self.assertEqual(kinds.count('start_born_controller'), 4)
        self.assertGreaterEqual(kinds.count('stop_born_controller'), 4)

    def test_two_runs_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = self._pass(1)['digest']
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        evidence2 = Path(other.name) / 'evidence'
        evidence2.mkdir()
        feed2 = CyclicFeed(Path(other.name) / 'sim-bus' / 'model.json',
                           Path(other.name) / 'driven-journal.jsonl')
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        self.assertEqual(first, json.loads(
            (evidence2 / 'sim-cyclic-fencing-loss-pass-1.json')
            .read_text())['digest'])

    # The doctored negatives — each named defect fails the run by the
    # named diagnostic.

    def test_ex_owner_stays_active_fails(self):
        # The issue's own doctored negative: the ex-owner never demotes
        # while its staged exchange keeps meeting the fence.
        self.feed.stays_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_active_peers_fails(self):
        # A demotion one poll late: both peers report active at the poll
        # where the promoted peer settles beside the still-active
        # ex-owner.
        self.feed.delayed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        self.assertIn('two peers reported role=active',
                      record.get('detail', ''))
        self.assertEqual(self._pass(1)['violations'].get('dual-active'),
                         'cyclic-fencing-loss-failed')
        report.validate_scenario(record)

    def test_unjournaled_demotion_fails(self):
        self.feed.silent_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_request_origin_demotion_fails(self):
        # A demotion journaled as an operator request is not the fencing
        # path the contract names.
        self.feed.request_origin = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_claim_loss_fails(self):
        self.feed.no_claim_loss = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unattributed_claim_loss_fails(self):
        self.feed.unattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_claimant_fails(self):
        self.feed.wrong_claimant = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_stalled_fails(self):
        self.feed.field_stalled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_still_fenced_ex_owner_fails(self):
        # The demoted ex-owner whose staged output image was not
        # released keeps meeting the standing claim forever.
        self.feed.still_fenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unrecovered_pair_fails(self):
        self.feed.never_restored = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        self.assertIn('never reconverged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restarted_ex_owner_fails(self):
        self.feed.restarted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ex_owner_exit_fails(self):
        self.feed.owner_exits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-failed', record.get('detail', ''))
        report.validate_scenario(record)

    # A staged revision predating the contract, and every absent
    # capability, report inconclusive.

    def test_pre_contract_revision_is_inconclusive(self):
        # The recorded defect: the ex-owner's io_health never names a
        # fenced exchange and it stays active beside its promoted peer.
        self.feed.predates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        report.validate_scenario(record)

    def test_absent_field_is_inconclusive(self):
        ctx = self._ctx()
        ctx['sim_bus_device'] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no sim-bus device server', record.get('detail', ''))
        report.validate_scenario(record)

    def test_absent_cyclic_model_is_inconclusive(self):
        # The spec's device server stages but names no cyclic model —
        # the leg declines before a container exists rather than
        # fencing whatever the default fixture declares.
        ctx = self._ctx()
        del ctx['sim_bus_device']['cyclic_model']
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('cyclic_model', record.get('detail', ''))
        # The pass's teardown sweep still runs; nothing was staged.
        self.assertNotIn('start_sim_bus_device',
                         [call[0] for call in self.feed.calls])
        report.validate_scenario(record)

    def test_non_cyclic_field_is_inconclusive(self):
        self.feed.not_cyclic = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('sim-cyclic', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_without_outputs_is_inconclusive(self):
        self.feed.no_outputs = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no output channel', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_pair_is_inconclusive(self):
        self.feed.unsettled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('start_sim_bus_device', 'restart_sim_bus_device',
                    'stop_sim_bus_device', 'start_born_controller',
                    'stop_born_controller', 'born_controller_state'):
            ctx[key] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('register-protocol staging levers',
                      record.get('detail', ''))
        report.validate_scenario(record)

    # The instability the contract does not answer for reports
    # nondeterministic.

    def test_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_sever_failure_is_nondeterministic(self):
        self.feed.sever_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promote_refusal_is_nondeterministic(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_starved_watch_is_nondeterministic(self):
        # A monitor that answers no poll after the promotion: the leg
        # cannot read the demotion, and the ex-owner still stands.
        self.feed.starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_state_read_failure_is_nondeterministic(self):
        self.feed.state_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_read_failure_is_nondeterministic(self):
        self.feed.tear_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_moves_is_nondeterministic(self):
        self.feed.pair_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_wedged_is_nondeterministic(self):
        self.feed.pair_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        calls = []
        real = scenarios._cyclic_digest

        def diverging(record, violations):
            calls.append(1)
            digest = dict(real(record, violations))
            if len(calls) > 1:
                digest['pair'] = 'wiggled'
            return digest

        with patch.object(scenarios, '_cyclic_digest', diverging):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    # The leg's own auditors must catch their planted negatives.

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_judge_cyclic',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cyclic-fencing-loss-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._cyclic_self_check(), [])


if __name__ == '__main__':
    unittest.main()
