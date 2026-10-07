"""The 2494_writable_field_point_settle leg's scenario unit coverage —
the feed fake and TestCase class for
scenario_writable_field_point_settle, split out per the leg-module
convention (#940). The shared fakes and helpers live in
tests/qa_scenario_support.py.


The feed stages the leg's shape: the lane's own scratch field serves a
lane-derived document carrying one channel-bound writable field input,
one born seat stands on that same document with a `--remote`
attachment, and the shipped `dcs-plant-ctl` seam answers the field's
own reads, the fault injection the contended leg stages, and its clear.
Every receipted write settles through the served chain at the
submission index taken before the post, so a driver write that never
reaches the field — an image-only settlement, a phantom write over a
denied write, a re-entry the cleared field still refuses — is visible
in the evidence the leg reads. The fault flags stage each named defect
and each named instability, plus the absent capabilities the leg
declines on."""
import json
import tempfile
import unittest
from pathlib import Path

from qa_scenario_support import *  # noqa: F401,F403 — the shared seam


#: The mounted rig model the leg derives from: a discrete input device
#: whose declared channels are all bound, so the derivation must add a
#: channel of its own for its writable point to ride.
MOUNTED_FIXTURE = {
    'version': 1,
    'devices': [{'id': 2, 'kind': 'sim-di',
                 'channels': {'p101-run': {'direction': 'in',
                                           'value_type': 'bool'},
                              'p102-run': {'direction': 'in',
                                           'value_type': 'bool'}}}],
    'io_points': [{'id': 300, 'direction': 'in', 'value_type': 'bool',
                   'channel': {'device': 2, 'name': 'p101-run'}},
                  {'id': 301, 'direction': 'in', 'value_type': 'bool',
                   'channel': {'device': 2, 'name': 'p102-run'}},
                  {'id': 400, 'direction': 'in', 'value_type': 'bool',
                   'writable': True, 'initial': {'bool': False}}],
    'signals': [{'id': 4000, 'name': 'run-1', 'source': 300, 'unit': '',
                 'group': 'pumps'},
                {'id': 4100, 'name': 'oos', 'source': 400, 'unit': '',
                 'group': 'pumps'}],
    'components': [], 'connections': []}

#: The device the leg derives on, and the channel id the derivation
#: gives its point — one above the mounted fixture's maximum.
DEVICE = 2
DERIVED_POINT = max(point['id'] for point in MOUNTED_FIXTURE['io_points']) + 1
#: The channel name the derivation adds for its point, and the outcome
#: key a driver refusal settles under.
DERIVED_CHANNEL = 'qa-writable-write-back'
REJECTED = 'rejected:driver_rejected'


class CtlAnswer:
    """The shipped field tool's own answer: the exit the leg
    classifies, with the printed response and its stderr."""

    def __init__(self, stdout='', returncode=0, stderr=''):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class WritableFieldFeed:
    """A stubbed rig for the writable-field-point settlement legs.

    The scratch field serves the lane-derived document the seat mounts,
    so both ends read one declaration; the seat's receipted writes
    settle `applied` by way of the field, and settle the named
    `driver_rejected` verdict while the field denies the write. Every
    fault flag doctors one named defect or one named instability, so
    the leg's own judge must catch it."""

    HOSTS = {'ctrl-a:1': 'active', 'ctrl-b:2': 'standby',
             'ctrl-c:3': 'revised', 'ctrl-d:5': 'driven'}

    def __init__(self, model):
        self.model = Path(model)
        self.field = None
        self.seats = {}
        self.calls = []
        self.mounted = None
        # The derived document the lane's own derivation produces.
        self.derived_point = DERIVED_POINT
        self.derived_channel = DERIVED_CHANNEL
        # Staging failures and absent surfaces.
        self.stage_fails = False
        self.launch_fails = False
        self.silent_rig = False
        self.no_mounted_model = False
        self.underivable = False
        self.unreadable_model = False
        # The field's own state: the value it holds for the derived
        # point and the fault standing on it.
        self.held = False
        self.faulted = False
        # The contract defects.
        self.image_only = False      # applied without a driver write
        self.no_serve_back = False   # applied, field written, unread
        self.denied_applied = False  # a denied write settles applied
        self.denied_pending = False  # a denied write never settles
        self.other_reason = False    # a denial naming another verdict
        self.phantom = False         # a denied write that landed anyway
        self.no_reentry = False      # a cleared field still refusing
        self.reentry_image_only = False
        # The instabilities.
        self.tool_refused = False
        self.field_silent = False

    # --- the staged documents ---------------------------------------

    def write_mounted(self):
        self.model.parent.mkdir(parents=True, exist_ok=True)
        document = json.loads(json.dumps(MOUNTED_FIXTURE))
        if self.underivable:
            # A device the derivation cannot mirror: its only channel
            # declares no usable shape, so there is no input value kind
            # for the derived channel to carry.
            document['devices'][0]['channels'] = {'p101-run': 'broken'}
        self.model.write_text('not json' if self.unreadable_model
                              else json.dumps(document))

    # --- the shipped field tool's seam, faked -----------------------

    def ctl(self, *args):
        self.calls.append(('born_field_ctl',) + args)
        command = args[0] if args else ''
        if command == 'read':
            if self.field_silent or self.field is None:
                return CtlAnswer('', 1, 'connection refused')
            return CtlAnswer(json.dumps({'point': self.derived_point,
                                         'sample': {
                                             'value': {'bool': self.held},
                                             'quality': 'good',
                                             'tick': 100}}))
        if command == 'fault':
            if self.tool_refused:
                return CtlAnswer('', 1, 'field is busy')
            self.faulted = True
            return CtlAnswer('{\n  "result": "done"\n}')
        if command == 'clear-fault':
            if self.tool_refused:
                return CtlAnswer('', 1, 'field is busy')
            self.faulted = False
            return CtlAnswer('{\n  "result": "done"\n}')
        return CtlAnswer('', 1, 'unknown command ' + repr(command))

    # --- the runner's born-field and born-seat levers, faked --------

    def start_field(self, mode, document=None):
        self.calls.append(('start_born_field', mode, document))
        if self.stage_fails:
            raise RuntimeError('docker run failed: name in use')
        assert mode == 'serving', 'the leg stages the serving field'
        assert document is not None, \
            'the field serves the leg\'s derived document'
        self.mounted = Path(document)
        self.field = 'serving'
        self.faulted = False
        self.held = False
        return {'container': 'dcs-hw-qa-1-born-plant',
                'remote': 'dcs-hw-qa-1-born-plant:9004',
                'mode': mode, 'model': str(self.mounted)}

    def stop_field(self):
        self.calls.append(('stop_born_field',))
        self.field = None
        self.faulted = False

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None):
        self.calls.append(('start_born_controller', seat, remote,
                           peer, standby, document))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        assert remote is not None, 'the writable field point rides a ' \
            '--remote attachment'
        assert document == str(self.mounted), \
            'the seat mounts the field\'s served document'
        self.seats[seat] = {'seat': seat, 'role': 'active', 'tick': 100,
                            'receipts': [], 'attempts': 0}
        return {'container': 'dcs-hw-qa-1-' + seat, 'seat': seat,
                'address': 'dcs-hw-qa-1-' + seat + ':8082',
                'remote': remote, 'peer': peer, 'standby': standby,
                'model': document,
                'monitor': 'http://ctrl-d:5'}

    def stop_controller(self, seat):
        self.calls.append(('stop_born_controller', seat))
        self.seats.pop(seat, None)

    def state(self, seat):
        return {'container': 'dcs-hw-qa-1-' + seat,
                'running': seat in self.seats, 'exit': None,
                'logs': '', 'absent': seat not in self.seats}

    # --- the receipted command path the served chain answers --------

    def _settle(self, peer):
        """Settle the provisional `accepted` receipt the next scan
        boundary completes, under whichever verdict the fault flags
        stage: a healthy field takes the write and the driver write
        lands on it, a faulted field refuses it and names the refusal.
        `image_only` is the shape this leg exists to exclude — an
        `applied` receipt for a write that reached only the image."""
        peer['tick'] += 1
        index = peer['attempts'] - 1
        if self.faulted:
            if self.denied_applied:
                peer['receipts'][index]['outcome'] = {'applied': {
                    'tick': peer['tick']}}
                return
            if self.denied_pending:
                # The receipt never leaves `accepted`: a silent
                # settlement, not a verdict.
                return
            reason = 'not_writable' if self.other_reason else 'driver_rejected'
            peer['receipts'][index]['outcome'] = {'rejected': {
                'reason': {reason: {'point': self.derived_point}}}}
            if self.phantom:
                # The denied write landed anyway, carrying the value it
                # asked for: the field now reads what the receipt
                # declined to apply.
                self.held = not self.held
            return
        if not self.image_only:
            self.held = True
        peer['receipts'][index]['outcome'] = {'applied': {
            'tick': peer['tick']}}

    def _snapshot(self, seat):
        peer = self.seats.get(seat)
        if peer is None:
            return None
        # The monitor's own read of the point: the written value the
        # same scan read back, unless the flag stages a monitor that
        # kept serving what it read before the substitution.
        value = self.held and not self.no_serve_back
        return {'tick': peer['tick'],
                'points': [{'point': self.derived_point,
                            'sample': {'value': {'bool': value},
                                       'quality': 'good',
                                       'tick': peer['tick']}}]}

    def _role(self, seat):
        peer = self.seats.get(seat)
        if peer is None:
            return None
        return {'role': peer['role'], 'tick': peer['tick'],
                'field_claim': 'held', 'sync': None}

    def _checkpoint(self, seat):
        peer = self.seats.get(seat)
        return {'receipts': peer['receipts'],
                'command_admission': {'attempts': peer['attempts']}}

    # --- the monitor channel — replaces scenarios.http_json ---------

    def http_json(self, method, url, body=None, timeout=10):
        if self.silent_rig:
            raise urllib.error.URLError('connection refused')
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        if host not in self.HOSTS:
            raise urllib.error.URLError('connection refused')
        seat = self.HOSTS[host]
        if seat not in self.seats:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(seat) or {'role': None}
        if (method, route) == ('GET', '/snapshot'):
            served = self._snapshot(seat)
            if served is None:
                raise urllib.error.URLError('connection refused')
            return 200, served
        if (method, route) == ('GET', '/checkpoint'):
            return 200, self._checkpoint(seat)
        if (method, route) == ('GET', '/receipts'):
            return 200, {'receipts': self.seats[seat]['receipts']}
        if (method, route) == ('POST', '/command'):
            peer = self.seats[seat]
            peer['attempts'] += 1
            peer['receipts'].append({
                'index': peer['attempts'] - 1,
                'outcome': {'accepted': {'accepted_tick': peer['tick']}}})
            command = (body or {}).get('command', {})
            if 'write_value' in command:
                self._settle(peer)
            return 202, {'index': peer['attempts'] - 1,
                         'outcome': {'accepted': {}}}
        raise AssertionError('unexpected request %s %s' % (method, url))


class WritableFieldPointTests(unittest.TestCase):
    """The leg's pass, failure, and inconclusive outcomes."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.evidence = self.root / 'evidence'
        self.evidence.mkdir()
        self.model = self.root / 'src' / 'plant_station.json'
        self.feed = WritableFieldFeed(self.model)
        self.feed.write_mounted()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        ctx = {
            'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
            'revised': 'http://ctrl-c:3', 'driven': 'http://ctrl-d:5',
            'evidence_dir': str(self.evidence),
            'born_field_ctl': feed.ctl,
            'start_born_field': feed.start_field,
            'stop_born_field': feed.stop_field,
            'start_born_controller': feed.start_controller,
            'stop_born_controller': feed.stop_controller,
            'born_controller_state': feed.state,
            'mounted_model': None if feed.no_mounted_model
            else str(self.model),
        }
        return ctx

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx(feed)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'WFP_SETTLE_BOUND', 1.0), \
                patch.object(scenarios, 'WFP_SETTLE_POLL', 0.001), \
                patch.object(scenarios, 'WFP_WRITE_BOUND', 1.0), \
                patch.object(scenarios, 'WFP_WRITE_POLL', 0.001):
            return scenarios.scenario_writable_field_point_settle(ctx)

    def _pass(self, number):
        return json.loads(
            (self.evidence / ('writable-field-point-pass-'
                              + str(number) + '.json')).read_text())

    def test_registered(self):
        self.assertIn(scenarios.scenario_writable_field_point_settle,
                      scenarios.SCENARIOS)
        self.assertIs(
            verify.case_function('writable-field-point-settle'),
            scenarios.scenario_writable_field_point_settle)
        order = list(scenarios.SCENARIOS)
        mine = order.index(scenarios.scenario_writable_field_point_settle)
        self.assertLess(
            order.index(scenarios.scenario_claim_skew_bound), mine)
        self.assertLess(
            mine, order.index(
                scenarios.scenario_demote_release_stays_released))

    def test_clean_passes_validate_and_tear_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        refs = sorted(entry['ref'] for entry in record['evidence'])
        self.assertEqual(
            refs,
            ['evidence/writable-field-point-pass-1.json',
             'evidence/writable-field-point-pass-2.json'])
        first = self._pass(1)
        self.assertEqual(first['violations'], {})
        self.assertEqual(
            first['digest'],
            {'apply': 'applied', 'contention': 'rejected',
             'reentry': 'applied', 'field': 'serving', 'rig': 'restored'})
        self.assertEqual(first['digest'], self._pass(2)['digest'])
        # The derived document is the lane's own variant, and both ends
        # read one declaration: the derived channel, the point, and the
        # field and seat that mounted it.
        self.assertEqual(first['field_document']['channel'],
                         DERIVED_CHANNEL)
        self.assertEqual(first['point'], DERIVED_POINT)
        self.assertEqual(first['field']['model'],
                         first['launch']['mounted'])
        self.assertTrue(first['field']['model'].endswith(
            'writable-field-point-model.json'))
        # The three receipted writes, in order, each with the field's
        # own read beside it.
        self.assertEqual(first['applied']['outcome'], 'applied')
        self.assertIs(first['applied']['field'], True)
        self.assertEqual(first['rejected']['outcome'], REJECTED)
        self.assertEqual(first['reentered']['outcome'], 'applied')
        self.assertIs(first['reentered']['field'], True)
        # The rig is swept: the seat and the scratch field are gone.
        self.assertEqual(first['rig'], {'seat': True, 'field_error': None})
        self.assertIn(('stop_born_controller', 'driven'), self.feed.calls)
        self.assertIn(('stop_born_field',), self.feed.calls)
        # The field's fault was injected and cleared, in order.
        # The field-side tool calls, one pass: the leg-1 read, the
        # contention injection, the contended read, the clear, and the
        # re-entry read — and the same sequence again on pass two.
        tool = [call[1] for call in self.feed.calls
                if call[0] == 'born_field_ctl']
        self.assertEqual(tool, ['read', 'fault', 'read', 'clear-fault',
                                'read'] * 2)

    def test_two_runs_produce_identical_digests(self):
        first = self.run_scenario()
        self.assertEqual(first['outcome'], 'passed', first)
        second = self.run_scenario()
        self.assertEqual(second['outcome'], 'passed', second)
        self.assertEqual(self._pass(1)['digest'], self._pass(2)['digest'])

    def test_an_image_only_settlement_fails(self):
        self.feed.image_only = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('writable-field-point-failed: ', record['detail'])
        self.assertIn('field-not-written', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unsettled_receipt_fails(self):
        self.feed.denied_pending = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-rejection', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_write_not_read_back_fails(self):
        self.feed.no_serve_back = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('served-mismatch', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_denied_write_settled_applied_fails(self):
        self.feed.denied_applied = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-rejection', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_denied_write_left_provisional_fails(self):
        self.feed.denied_pending = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-rejection', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unnamed_rejection_fails(self):
        self.feed.other_reason = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-rejection', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_phantom_write_fails(self):
        self.feed.phantom = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('phantom-write', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_refused_reentry_fails(self):
        self.feed.no_reentry = True
        feed = self.feed
        original = feed.ctl

        def ctl(*args):
            answer = original(*args)
            # The clear answers done, but the field keeps refusing the
            # write: the re-entered settlement is a denial.
            if args and args[0] == 'clear-fault':
                feed.faulted = True
            return answer
        feed.ctl = ctl
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-reentry', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_reentry_that_did_not_land_fails(self):
        self.feed.reentry_image_only = True
        feed = self.feed
        original = feed.ctl

        def ctl(*args):
            # The cleared field accepts the write, but the driver write
            # after the contention never reaches it: the third receipt
            # settles applied over an image-only substitution.
            answer = original(*args)
            if args and args[0] == 'clear-fault':
                # The cleared field accepts the re-entered write, but
                # the driver write after the contention reaches only
                # the image: drop the field's held value so the
                # image-only substitution is visible in its read.
                feed.image_only = True
                feed.held = False
            return answer
        feed.ctl = ctl
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reentry-not-written', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unmounted_document_is_inconclusive(self):
        self.feed.no_mounted_model = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('mounted model', record['detail'])
        report.validate_scenario(record)

    def test_an_underivable_document_is_inconclusive(self):
        self.feed.underivable = True
        self.feed.write_mounted()
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no usable value_type', record['detail'])
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        for key in ('start_born_field', 'stop_born_field',
                    'start_born_controller', 'stop_born_controller',
                    'born_controller_state', 'born_field_ctl'):
            ctx = self._ctx()
            ctx[key] = None
            record = self.run_scenario(ctx=ctx)
            self.assertEqual(record['outcome'], 'inconclusive', record)
            self.assertIn(key, record['detail'])
            report.validate_scenario(record)

    def test_an_unsettled_seat_is_nondeterministic(self):
        feed = _Silent(self.model)
        feed.write_mounted()
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'writable-field-point-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_a_refused_fault_injection_is_nondeterministic(self):
        self.feed.tool_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('fault-unanswered', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_a_refused_field_clear_is_nondeterministic(self):
        feed = self.feed
        original = feed.ctl
        state = {'cleared': False}

        def ctl(*args):
            if args and args[0] == 'clear-fault':
                state['cleared'] = True
                return CtlAnswer('', 1, 'field is busy')
            return original(*args)
        feed.ctl = ctl
        record = self.run_scenario(feed=feed)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('clear-unreadable', self._pass(1)['violations'])
        report.validate_scenario(record)

    def test_an_unreachable_rig_is_nondeterministic(self):
        self.feed.silent_rig = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'writable-field-point-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_a_stage_failure_is_nondeterministic(self):
        self.feed.stage_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'writable-field-point-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_a_launch_failure_is_nondeterministic(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'writable-field-point-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_divergent_digests_are_nondeterministic(self):
        with patch.object(scenarios, '_wfp_digest',
                          side_effect=[{'apply': 'applied'},
                                       {'apply': 'image-only'}]):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'writable-field-point-nondeterministic'), record['detail'])
        self.assertIn('digests', record['detail'])
        report.validate_scenario(record)

    def test_an_unchecked_self_check_fails(self):
        with patch.object(scenarios, '_wfp_self_check',
                          return_value=['a planted negative slipped']):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'writable-field-point-unchecked'), record['detail'])
        report.validate_scenario(record)

    def test_self_check_is_complete(self):
        self.assertEqual(scenarios._wfp_self_check(), [])


class _Silent(WritableFieldFeed):
    """A rig whose seat never settles: the staging lands, the monitor
    never answers `active`, so no receipt can settle at a boundary."""

    def start_controller(self, seat, remote, peer=None, standby=None,
                         document=None):
        launch = super().start_controller(seat, remote, peer, standby,
                                          document)
        self.seats[seat]['role'] = None
        return launch

    def _role(self, seat):
        return {'role': None, 'tick': 0, 'field_claim': 'unclaimed',
                'sync': None}
