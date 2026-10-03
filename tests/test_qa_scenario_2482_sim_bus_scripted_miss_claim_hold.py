"""The 2482_sim_bus_scripted_miss_claim_hold leg's scenario unit
coverage — the feed fake and TestCase class for
scenario_sim_bus_scripted_miss_claim_hold, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py; EXPECTED_CASES pins this module's
contribution to the suite's case coverage so a dropped case fails the
discovery check in tests/test_qa_scenario_modules.py.

The feed stages the leg's shape: the lane's device server serves the
run's staged sim-cyclic model, the born-seat launch stands a lone
claim holder on it, and the shipped `dcs-sim-bus-ctl` seam queues
outcomes the holder's next scan consumes — `miss` answers in band, the
scan records the failed cycle, the claim its connection carries stays
held, no journaled claim loss or fenced walk lands, and the following
`complete` completes on the same link with the device still serving.
The fault flags stage each named defect (including the issue's own
doctored negative, a record asserting the claim survived while the
device severed the connection), each nondeterministic surface, and the
pre-contract revision whose queued miss severed the connection and
released the claim with it."""
import json
import unittest
import urllib.error
from pathlib import Path

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


EXPECTED_CASES = frozenset({
    'ScriptedMissClaimHoldTests.test_registered',
    'ScriptedMissClaimHoldTests.test_clean_passes_validate_and_tear_down',
    'ScriptedMissClaimHoldTests.test_two_runs_produce_identical_digests',
    'ScriptedMissClaimHoldTests.test_claim_survived_a_severed_link_fails',
    'ScriptedMissClaimHoldTests.test_claim_freed_by_the_in_band_miss_fails',
    'ScriptedMissClaimHoldTests.test_usurped_claim_fails',
    'ScriptedMissClaimHoldTests.test_fenced_demotion_fails',
    'ScriptedMissClaimHoldTests.test_operator_demotion_fails',
    'ScriptedMissClaimHoldTests.test_restarted_holder_fails',
    'ScriptedMissClaimHoldTests.test_miss_never_consumed_fails',
    'ScriptedMissClaimHoldTests.test_complete_never_answered_fails',
    'ScriptedMissClaimHoldTests.test_frozen_run_tick_fails',
    'ScriptedMissClaimHoldTests.test_pre_contract_revision_is_inconclusive',
    'ScriptedMissClaimHoldTests.test_absent_device_server_is_inconclusive',
    'ScriptedMissClaimHoldTests.test_absent_cyclic_model_is_inconclusive',
    'ScriptedMissClaimHoldTests.test_point_wise_field_is_inconclusive',
    'ScriptedMissClaimHoldTests.test_field_without_outputs_is_inconclusive',
    'ScriptedMissClaimHoldTests.test_unsettled_holder_is_inconclusive',
    'ScriptedMissClaimHoldTests.test_unreachable_rig_is_inconclusive',
    'ScriptedMissClaimHoldTests.test_missing_seams_are_inconclusive',
    'ScriptedMissClaimHoldTests.test_stage_failure_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_launch_failure_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_tool_refusal_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_starved_claim_view_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_unnamed_miss_answer_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_journal_read_failure_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_device_stopped_serving_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_pair_moves_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_pair_wedged_is_nondeterministic',
    'ScriptedMissClaimHoldTests.test_divergent_digests_are_nondeterministic',
    'ScriptedMissClaimHoldTests.test_unchecked_self_check_fails',
    'ScriptedMissClaimHoldTests.test_self_check_is_complete',
})


# The staged device model the lane's server serves: one sim-cyclic
# device carrying an input and an output channel — the output is what
# the scan stages into the image the exchange publishes, so the
# scripted queue has an exchange to ride.
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

# The driver's own descriptions, verbatim from the crate: the in-band
# scripted miss the protocol answers, and the connection a sever drops.
INBAND = 'the exchange did not complete: a scripted miss answered the exchange'
SEVERED = 'the exchange did not complete: no live connection to the device server'


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


class CtlAnswer:
    """The shipped control tool's own answer: the exit the leg
    classifies, with the printed response and its stderr."""

    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class ScriptedMissFeed:
    """A stubbed rig for the scripted-miss claim-hold leg.

    The device server serves the staged sim-cyclic model and answers the
    shipped `dcs-sim-bus-ctl` seam: `script-exchange` appends outcomes
    the holder's next scan consumes, `list` reports the served register
    bank. A queued `miss` answers in band — the scan records the failed
    cycle, the driver describes the missed exchange, the link and the
    claim bound to it stay up — and the `complete` queued after it
    completes on the same connection. The fault flags doctor each named
    shape: the issue's own doctored negative (the claim reported held
    while the device severed the connection), the in-band miss that
    frees the claim anyway, a claim usurped by a foreign attachment, the
    fenced and operator demotions, the restarted process, the miss no
    exchange consumed, a recovery that never completes, a frozen run
    clock, the pre-contract signature (the sever that took the claim
    with it), and every named instability.
    """

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-d:5': 'driven',
             'ctrl-foreign:6': 'foreign'}
    OWNER_TOKEN = 424247
    FOREIGN_TOKEN = 424248

    def __init__(self, model, journal):
        self.model = Path(model)
        self.journal = Path(journal)
        self.journal.parent.mkdir(parents=True, exist_ok=True)
        self.journal.write_text('')
        self.deployed = {'active': 900, 'standby': 900}
        self.field = None        # None | 'serving'
        self.seats = {}          # seat -> the launched holder's state
        self.queue = []          # the device's scripted exchange queue
        self.moved = False       # the deployed pair's post-stage disturbance
        self.calls = []
        # Staging failures and absent surfaces.
        self.stage_fails = False        # start_sim_bus_device raises
        self.launch_fails = False       # start_born_controller raises
        self.silent_rig = False         # every monitor refuses
        self.tear_journal = False       # the journal file tears mid-read
        self.starves = False            # arm the holder's monitor going quiet
        self.starved = False            # it went quiet at the missed cycle
        self.tool_refused = False       # the control tool answers nonzero
        self.device_silent = False      # the served register read refuses
        # The pre-contract and absent-capability shapes.
        self.not_cyclic = False         # the staged model is point-wise
        self.no_outputs = False         # the cyclic device stages nothing
        self.no_claim = False            # the launch never takes the claim
        self.pre_contract = False       # the miss severs and frees the claim
        # The contract defects the issue names.
        self.sever_keeps_claim = False   # severed, yet the claim reads held
        self.claim_lost = False          # the in-band miss frees the claim
        self.usurped = False             # a foreign claim takes the field
        self.fenced = False              # the fenced demotion path journals
        self.demoted = False             # an operator demotion journals
        self.restarted = False           # a second run boundary lands
        self.unrecorded = False          # the queued miss is never consumed
        self.silent_answer = False       # the failure carries no description
        self.never_recovers = False      # the link never completes again
        self.frozen_tick = False         # the run clock stops
        # The deployed pair's instabilities.
        self.pair_moves = False
        self.pair_wedged = False

    # --- the run config's staged document ---------------------------

    def _stage_model(self):
        kind = 'sim-bus' if self.not_cyclic else 'sim-cyclic'
        outputs = set() if self.no_outputs else {'do1'}
        self.model.parent.mkdir(parents=True, exist_ok=True)
        self.model.write_text(json.dumps(_model_document(kind, outputs)))

    # --- the shipped control tool's seam, faked --------------------

    def ctl(self, *args):
        self.calls.append(('sim_bus_ctl',) + args)
        command = args[0] if args else ''
        if command == 'script-exchange':
            if self.tool_refused:
                return CtlAnswer('', 1, 'device is busy')
            self.queue.extend(args[1:])
            return CtlAnswer('{\n  "result": "done"\n}')
        if command == 'list':
            if self.device_silent or self.field is None:
                return CtlAnswer('', 1, 'connection refused')
            return CtlAnswer('{\n  "registers": [\n    {\n'
                             '      "register": 0\n    }\n  ]\n}')
        return CtlAnswer('', 1, 'unknown command ' + repr(command))

    # --- the runner's register-protocol levers, faked ---------------

    def start_device(self, fixture=None, timeout_ms=None):
        self.calls.append(('start_sim_bus_device', fixture))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        assert fixture == CYCLIC_MODEL_FIXTURE, \
            'the leg stages the spec\'s cyclic_model, got ' + repr(fixture)
        self._stage_model()
        self.field = 'serving'
        self.queue = []
        if self.pair_moves:
            self.moved = True
        return {'container': 'dcs-hw-qa-1-bus', 'device': 1, 'port': 9005,
                'address': 'dcs-hw-qa-1-bus:9005',
                'model': str(self.model)}

    def stop_device(self):
        self.calls.append(('stop_sim_bus_device',))
        self.field = None
        self.queue = []

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby, document))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        assert remote is None, 'the cyclic model carries no --remote'
        assert document == str(self.model), \
            'the seat mounts the device server\'s staged document'
        assert peer is None and standby is None, \
            'the leg stages a lone claim holder'
        # A born launch is cold: the runner-owned journal resets with
        # the container, so each pass's file records one lifetime.
        self.journal.write_text('')
        self._boundary()
        owns = not self.no_claim
        self.seats[seat] = {
            'seat': seat, 'role': 'active', 'token': self.OWNER_TOKEN,
            'claim': owns, 'claimed_view': 'held' if owns else None,
            'tick': 100, 'attempted': 10, 'succeeded': 10, 'failed': 0,
            'streak': 0, 'link': 'connected', 'described': None}
        # The launch's own return: the document actually mounted, which
        # the leg evidences both ends of the register protocol through.
        return {'container': 'dcs-hw-qa-1-' + seat, 'seat': seat,
                'address': 'dcs-hw-qa-1-' + seat + ':8082',
                'remote': None, 'peer': None, 'standby': None,
                'model': document,
                'monitor': 'http://127.0.0.1:18084'}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seats.pop(seat, None)

    def state(self, seat):
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': seat in self.seats, 'exit': None,
                'logs': '', 'absent': seat not in self.seats}

    # --- the holder's durable journal -------------------------------

    def _write(self, record):
        with self.journal.open('a') as handle:
            handle.write(json.dumps(record) + '\n')

    def _boundary(self):
        self._write({'run_boundary': {'run': 1, 'tick': 0}})

    def _journal(self, event):
        self._write({'entry': {'seq': 0, 'tick': 0, 'event': event}})

    def _demotion_journal(self, origin):
        self._journal({'role_changed': {'from': 'active', 'to': 'demoting',
                                        'origin': origin}})
        if origin == 'fenced':
            self._journal({'field_claim_lost': {'point': 3,
                                                'claimant':
                                                self.FOREIGN_TOKEN}})
        self._journal({'role_changed': {'from': 'demoting',
                                        'to': 'standby',
                                        'origin': origin}})

    # --- the device's scripted outcomes and the scans reading them --

    def _scan(self, seat):
        """One scan of the launched holder: the exchange the device's
        scripted queue answers, then the run's own bookkeeping of it."""
        peer = self.seats.get(seat)
        if peer is None:
            return None
        if not self.frozen_tick:
            peer['tick'] += 1
        peer['attempted'] += 1
        outcome = None
        if not self.unrecorded and self.queue:
            outcome = self.queue.pop(0)
        if outcome == 'miss':
            self._missed(peer)
        elif self.never_recovers:
            # The link the scripted miss left never completes again:
            # no further scripted outcome can land.
            peer['failed'] += 1
            peer['streak'] += 1
            peer['link'] = 'disconnected'
        else:
            peer['succeeded'] += 1
            peer['streak'] = 0
            peer['link'] = 'connected'
        if peer['role'] == 'demoting':
            # The demotion settles on the next quiesced scan.
            peer['role'] = 'standby'
            self._journal({'role_changed': {'from': 'demoting',
                                            'to': 'standby',
                                            'origin': 'fenced'}})
        return peer

    def _missed(self, peer):
        """The queued scripted miss, answered the way the contract
        requires — in band, on a live connection whose claim stays up —
        or the way each staged defect answers instead."""
        peer['failed'] += 1
        peer['streak'] += 1
        peer['link'] = 'disconnected'
        if self.silent_answer:
            peer['described'] = None
            return
        if self.pre_contract or self.sever_keeps_claim:
            # The sever: the connection drops unanswered, so the claim
            # bound to it dies with the connection — the recorded
            # defect. The `sever_keeps_claim` flag is the issue's own
            # doctored negative: the same sever with the claim reported
            # held anyway.
            peer['described'] = SEVERED
            if self.pre_contract:
                peer['claim'] = False
                peer['claimed_view'] = None
            return
        peer['described'] = INBAND
        if self.usurped:
            # Another attachment's claim took the field: the holder's
            # own probe still answers `held`, and the refusal its
            # conditional grants meet journals the standing claimant.
            self._journal({'field_claim_observed': {
                'point': 3, 'claimant': self.FOREIGN_TOKEN}})
        if self.claim_lost:
            peer['claim'] = False
            peer['claimed_view'] = None
        if self.fenced or self.demoted:
            peer['role'] = 'demoting'
            peer['claim'] = False
            peer['claimed_view'] = None
            self._demotion_journal('fenced' if self.fenced else 'request')
        if self.restarted and not (self.fenced or self.demoted):
            # A process that restarted rather than degrading in place
            # records a second lifetime on its own journal file.
            self._boundary()
        if self.tear_journal:
            # The journal file tears mid-read once the missed cycle has
            # landed: the durable half of the audit is unreadable.
            self._write({'torn': True})
        if self.starves:
            # The holder's monitor goes quiet from here: the claim
            # evidence the leg must audit stops arriving.
            self.starved = True

    def _role(self, seat):
        peer = self._scan(seat)
        if peer is None:
            return None
        return {'role': peer['role'], 'tick': peer['tick'],
                'field_claim': peer['claimed_view']}

    def _io(self, peer):
        """The holder's served io_health in the wire shape the leg
        normalizes: the boundary counters, the transport's link verdict,
        the cyclic exchange counters, and the driver's own standing
        description of the last exchange that did not complete."""
        error = 'disconnected' if peer['failed'] else None
        return {
            'failed_exchanges': peer['failed'], 'failed_reads': 0,
            'failed_writes': 0,
            'consecutive_failures': peer['streak'],
            'last_error': ({'tick': peer['tick'], 'point': 3,
                            'direction': 'In', 'error': {error: 3}}
                           if error else None),
            'driver': {'link': peer['link'],
                       'last_error': peer['described'],
                       'exchange': {'attempted': peer['attempted'],
                                    'succeeded': peer['succeeded'],
                                    'working_counter_mismatches': 0,
                                    'last_exchange_tick': peer['tick'],
                                    'missed_deadlines': 0}}}

    def _deployed(self, host):
        key = self.HOSTS[host]
        tick = self.deployed[key]
        if not self.pair_wedged or key == 'standby':
            self.deployed[key] += 1
        role = 'standby' if key == 'standby' else 'active'
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
        if self.starved and seat == 'driven':
            raise urllib.error.URLError('timed out')
        if (method, route) == ('GET', '/role'):
            report = self._role(seat)
            return 200, report if report else {'role': None}
        if (method, route) == ('GET', '/snapshot'):
            peer = self._scan(seat)
            if peer is None:
                raise urllib.error.URLError('connection refused')
            return 200, {'tick': peer['tick'], 'points': [],
                         'io_health': self._io(peer)}
        raise AssertionError('unexpected request %s %s' % (method, url))


class ScriptedMissClaimHoldTests(unittest.TestCase):
    """scenario_sim_bus_scripted_miss_claim_hold against the stubbed
    rig: a lone born-active holds the staged device's write-ownership
    claim, a queued scripted `miss` faults its next exchange in band
    while the claim and the link stay up with no journaled claim loss
    and no fenced walk, and the `complete` queued after it completes
    on the same link with the device still serving — two passes,
    identical digests. Each fault flag stages a named failure, a
    nondeterministic surface, or the pre-contract shape."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'driven-journal.jsonl'
        self.model = Path(self.tmp.name) / 'sim-bus' / 'model.json'
        self.feed = ScriptedMissFeed(self.model, self.journal)

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
                'stop_sim_bus_device': feed.stop_device,
                'sim_bus_ctl': feed.ctl,
                'start_born_controller': feed.start_controller,
                'stop_born_controller': feed.stop_controller,
                'born_controller_state': feed.state,
                'plant_owner': {'driven': ScriptedMissFeed.OWNER_TOKEN},
                'journal_files': {'driven': str(feed.journal)}}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'SETTLE_BOUND', 0.2), \
                patch.object(scenarios, 'MISS_BOUND', 0.2), \
                patch.object(scenarios, 'RECOVER_BOUND', 0.2), \
                patch.object(scenarios, 'SETTLE_POLL', 0.001), \
                patch.object(scenarios, 'MISS_POLL', 0.001):
            return scenarios.scenario_sim_bus_scripted_miss_claim_hold(
                ctx or self._ctx(feed))

    def _pass(self, number):
        return json.loads(
            (self.evidence / ('sim-bus-scripted-miss-claim-hold-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_sim_bus_scripted_miss_claim_hold,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('sim-bus-scripted-miss-claim-hold'),
            scenarios.scenario_sim_bus_scripted_miss_claim_hold)
        order = list(scenarios.SCENARIOS)
        mine = order.index(scenarios.scenario_sim_bus_scripted_miss_claim_hold)
        self.assertLess(
            order.index(
                scenarios.scenario_sim_bus_startup_claim_refusal), mine)
        self.assertLess(
            mine, order.index(scenarios.scenario_reclaim_convergence_gate))

    def test_clean_passes_validate_and_tear_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = [entry['ref'] for entry in record['evidence']]
        self.assertEqual(
            sorted(refs),
            ['evidence/sim-bus-scripted-miss-claim-hold-pass-1.json',
             'evidence/sim-bus-scripted-miss-claim-hold-pass-2.json'])
        first = self._pass(1)
        self.assertEqual(first['violations'], {})
        self.assertEqual(
            first['digest'],
            {'script': 'consumed', 'answer': 'in-band', 'claim': 'held',
             'fencing': 'none', 'recovery': 'completed',
             'device': 'serving', 'pair': 'held', 'rig': 'restored'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # The queued outcome, the failed cycle it caused, and the
        # in-band answer the driver recorded for it.
        self.assertEqual(first['script']['outcomes'], ['miss'])
        self.assertEqual(first['script_complete']['outcomes'],
                         ['complete'])
        self.assertEqual(first['missed']['answer'], 'in-band')
        self.assertTrue(first['missed']['advanced'])
        self.assertEqual(first['missed']['io']['described'], INBAND)
        self.assertGreater(first['missed']['io']['failed_exchanges'],
                           first['settled']['io']['failed_exchanges'])
        # The claim the connection carries survived, with no fencing
        # journaled and the process never restarted.
        self.assertEqual(first['missed']['view']['claim'], 'held')
        self.assertEqual(first['missed']['view']['role'], 'active')
        self.assertEqual(first['recovered']['view']['claim'], 'held')
        self.assertEqual(first['journal']['losses'], [])
        self.assertEqual(first['journal']['walk'], [])
        self.assertEqual(first['journal']['claimants'], [])
        self.assertEqual(len(first['journal']['boundaries']), 1)
        # The following complete landed on the same link.
        self.assertTrue(first['recovered']['advanced'])
        self.assertEqual(first['recovered']['io']['link'], 'connected')
        self.assertEqual(first['recovered']['io']['consecutive_failures'], 0)
        self.assertGreater(first['recovered']['io']['succeeded'],
                           first['missed']['io']['succeeded'])
        self.assertTrue(first['device']['read'])
        # Both ends of the register protocol read one declaration.
        self.assertEqual(first['field']['mounted'], str(self.model))
        # Every pass ends torn down — the seat and the device server
        # removed, so the legs behind this one find them free.
        self.assertEqual(self.feed.seats, {})
        self.assertIsNone(self.feed.field)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds.count('start_sim_bus_device'), 2)
        self.assertEqual(kinds.count('stop_sim_bus_device'), 2)
        self.assertEqual(kinds.count('start_born_controller'), 2)
        self.assertEqual(kinds.count('stop_born_controller'), 2)
        self.assertEqual(kinds.count('sim_bus_ctl'), 6)

    def test_two_runs_produce_identical_digests(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = self._pass(1)['digest']
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        evidence2 = Path(other.name) / 'evidence'
        evidence2.mkdir()
        feed2 = ScriptedMissFeed(Path(other.name) / 'sim-bus' / 'model.json',
                                 Path(other.name) / 'driven-journal.jsonl')
        ctx2 = self._ctx(feed2)
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        self.assertEqual(first, json.loads(
            (evidence2 / 'sim-bus-scripted-miss-claim-hold-pass-1.json')
            .read_text())['digest'])

    # The doctored negatives — each named defect fails the run by the
    # named diagnostic.

    def test_claim_survived_a_severed_link_fails(self):
        # The issue's own doctored negative: the queued miss answers by
        # dropping the connection, yet the record asserts the claim
        # survived — the assertion the in-band answer must back.
        self.feed.sever_keeps_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-failed',
                      record.get('detail', ''))
        self.assertIn('severed link', record.get('detail', ''))
        self.assertEqual(self._pass(1)['violations'].get('miss-answer'),
                         'sim-bus-scripted-miss-failed')
        report.validate_scenario(record)

    def test_claim_freed_by_the_in_band_miss_fails(self):
        # The recorded defect's consequence on a revision that answers
        # in band: the claim is gone anyway.
        self.feed.claim_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-failed',
                      record.get('detail', ''))
        self.assertEqual(self._pass(1)['digest']['claim'], 'lost')
        report.validate_scenario(record)

    def test_usurped_claim_fails(self):
        self.feed.usurped = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('foreign write-ownership claim',
                      record.get('detail', ''))
        self.assertEqual(self._pass(1)['journal']['claimants'],
                         [ScriptedMissFeed.FOREIGN_TOKEN])
        report.validate_scenario(record)

    def test_fenced_demotion_fails(self):
        self.feed.fenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('fenced demotion', record.get('detail', ''))
        # The durable half: the journaled claim loss the walk carries,
        # read out of the holder's own --journal-file across the window.
        self.assertEqual(self._pass(1)['journal']['losses'],
                         [{'point': 3,
                           'claimant': ScriptedMissFeed.FOREIGN_TOKEN}])
        self.assertEqual(self._pass(1)['digest']['fencing'], 'walked')
        report.validate_scenario(record)

    def test_operator_demotion_fails(self):
        self.feed.demoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('left the field', record.get('detail', ''))
        report.validate_scenario(record)

    def test_restarted_holder_fails(self):
        self.feed.restarted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('run boundaries', record.get('detail', ''))
        report.validate_scenario(record)

    def test_miss_never_consumed_fails(self):
        self.feed.unrecorded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never', record.get('detail', ''))
        self.assertEqual(self._pass(1)['digest']['script'], 'unconsumed')
        report.validate_scenario(record)

    def test_complete_never_answered_fails(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never completed on the holder', record.get('detail',
                                                                 ''))
        self.assertEqual(self._pass(1)['digest']['recovery'], 'stalled')
        report.validate_scenario(record)

    def test_frozen_run_tick_fails(self):
        self.feed.frozen_tick = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('run tick did not advance', record.get('detail', ''))
        report.validate_scenario(record)

    # A staged revision predating the contract, and every absent
    # capability, report inconclusive.

    def test_pre_contract_revision_is_inconclusive(self):
        # The recorded defect signature: the queued miss severed the
        # connection and the claim bound to it went with it.
        self.feed.pre_contract = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('predates', record.get('detail', ''))
        self.assertEqual(self._pass(1)['violations'], {})
        report.validate_scenario(record)

    def test_absent_device_server_is_inconclusive(self):
        ctx = self._ctx()
        ctx['sim_bus_device'] = None
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no sim-bus device server', record.get('detail', ''))
        # The pass's teardown sweep still runs; nothing was staged.
        self.assertNotIn('start_sim_bus_device',
                         [call[0] for call in self.feed.calls])
        report.validate_scenario(record)

    def test_absent_cyclic_model_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['sim_bus_device']['cyclic_model']
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('cyclic_model', record.get('detail', ''))
        report.validate_scenario(record)

    def test_point_wise_field_is_inconclusive(self):
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

    def test_unsettled_holder_is_inconclusive(self):
        self.feed.no_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('claim holder', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreachable_rig_is_inconclusive(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        for key in ('start_sim_bus_device', 'stop_sim_bus_device',
                    'sim_bus_ctl', 'start_born_controller',
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
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_tool_refusal_is_nondeterministic(self):
        self.feed.tool_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_starved_claim_view_is_nondeterministic(self):
        self.feed.starves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unnamed_miss_answer_is_nondeterministic(self):
        self.feed.silent_answer = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_read_failure_is_nondeterministic(self):
        self.feed.tear_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_device_stopped_serving_is_nondeterministic(self):
        self.feed.device_silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_moves_is_nondeterministic(self):
        self.feed.pair_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_pair_wedged_is_nondeterministic(self):
        self.feed.pair_wedged = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        calls = []
        real = scenarios._miss_digest

        def diverging(record, violations):
            calls.append(1)
            digest = dict(real(record, violations))
            if len(calls) > 1:
                digest['claim'] = 'lost'
            return digest

        with patch.object(scenarios, '_miss_digest', diverging):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('diverged', record.get('detail', ''))
        report.validate_scenario(record)

    # The leg's own auditors must catch their planted negatives.

    def test_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_judge_miss',
                          lambda record, note: None):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('sim-bus-scripted-miss-unchecked',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._miss_self_check(), [])


if __name__ == '__main__':
    unittest.main()