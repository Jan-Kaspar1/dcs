"""The deterministic scenarios' unit coverage: stubbed monitor feeds
drive scenario_consumer_schedule, scenario_served_interface, and
scenario_command_admission through their pass outcomes and the named
failures their issues call out — a stalled reader whose leg's scan
outputs stopped advancing, a lagging seq-cursor read answered with
silently stale data, a served registry missing a declared kind or
collection, a declared command returning no receipt, an emitted-events
view that never reflects the produced event, and a command flood whose
submissions meet dropped receipts, HTTP-layer faults, unsettled
admissions, or a bound that never fills — and the managed-alarm
lifecycle against a stubbed monitor and a live plant-protocol peer:
activation with journaled evidence, acknowledge, bounded shelve and
expiry, the named NotWritable refusal, OOS-driven suppression, and the
inconclusive answers a never-reporting status or a claimed field owe."""
import json
import socket
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from qa_lane import report, scenarios


HELD_RESPONSE = (b'HTTP/1.1 200 OK\r\nContent-Length: 26\r\n\r\n'
                 b'{"tick": 12, "points": []}')


def _pipelined_bodies(raw):
    """The JSON bodies out of a pipelined request stream — the test-side
    mirror of the flood channel's framing."""
    bodies = []
    rest = raw
    while rest:
        _line, sep, tail = rest.partition(b'POST /command')
        if not sep:
            break
        header, sep, body = tail.partition(b'\r\n\r\n')
        if not sep:
            break
        length = 0
        for line in header.split(b'\r\n'):
            if line.lower().startswith(b'content-length:'):
                length = int(line.split(b':', 1)[1])
        bodies.append(json.loads(body[:length]))
        rest = body[length:]
    return bodies


class FakeSocket:
    """Just enough of a connected TCP stream for the overlay's held,
    churning, and raw-probe consumers."""

    def __init__(self, response=b''):
        self.response = response
        self.sent = b''
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def recv(self, count):
        chunk, self.response = self.response[:count], self.response[count:]
        return chunk

    def settimeout(self, _seconds):
        pass

    def shutdown(self, _how):
        pass

    def close(self):
        self.closed = True


class Feed:
    """A stubbed monitor pair. Every call on the measurement channel
    (`http_json`) is one completed scan — reads observe, writes queue —
    while the consumer channel (`request_status`, raw sockets) never
    moves the plant: exactly the split the scenario measures."""

    def __init__(self):
        self.tick = 0
        self.window = 4       # small publication window, quick lag
        self.journal_cap = 8  # small ring, quick cursor roll
        self.journal = [{'seq': seq, 'tick': 0, 'event': {}}
                        for seq in range(1, 5)]
        self.next_seq = 5
        self.receipts = []
        self.point = False
        self.sockets = []
        # The bounded pending-command queue the snapshot's
        # command_queue section reports: small so the test flood is tiny.
        self.capacity = 4
        self.bounded = True
        self.full_rejections = 0
        self.high_water = 0
        self.flood_pending = set()  # receipt indices admitted on the flood channel
        # Fault injection for the named-failure cases.
        self.freeze = False           # scans stop advancing
        self.freeze_on_connect = False  # ... once a consumer holds one
        self.stale_cursor = False     # since= reads fabricate evicted seqs
        self.hold_flood = False       # flood-admitted receipts never settle
        self.flood_drop = False       # the last flood answer never arrives
        self.flood_status = None      # flood answers carry this status

    def _depth(self):
        return sum(1 for receipt in self.receipts
                   if 'accepted' in receipt['outcome'])

    # The admission path shared by the measurement channel's POST
    # /command and the flood channel's pipelined submissions: validation
    # precedes admission, and a queue at capacity takes the named
    # queue_full rejection — exactly one receipt per submission.
    def _admit(self, body, flood=False):
        write = body['command']['write_value']
        depth = self._depth()
        if write['point'] == 10:
            if self.bounded and depth >= self.capacity:
                self.full_rejections += 1
                receipt = {'command': body['command'],
                           'outcome': {'rejected': {'reason': {
                               'queue_full': {'point': write['point'],
                                              'capacity': self.capacity}}}},
                           'actor': body.get('actor')}
                self._journal_entry()
            else:
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                if flood:
                    self.flood_pending.add(len(self.receipts))
                self.high_water = max(self.high_water, depth + 1)
        else:
            receipt = {'command': body['command'],
                       'outcome': {'rejected': {'reason': {
                           'not_writable': {'point': write['point']}}}},
                       'actor': body.get('actor')}
            self._journal_entry()
        self.receipts.append(receipt)
        return receipt

    # The plant half: one completed scan per measurement read, applying
    # any accepted write whose apply_tick has arrived.
    def _advance(self):
        if self.freeze:
            return
        self.tick += 1
        for index, receipt in enumerate(self.receipts):
            if self.hold_flood and index in self.flood_pending:
                continue
            accepted = receipt['outcome'].get('accepted')
            if accepted and self.tick >= accepted['apply_tick']:
                write = receipt['command']['write_value']
                self.point = write['value']['bool']
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                self._journal_entry()

    def _journal_entry(self):
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {}})
        self.next_seq += 1

    # The measurement channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 20, 'signal': None, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}], 'components': []}
        if (method, route) == ('GET', '/snapshot'):
            published = self.tick + 1
            return 200, {
                'tick': self.tick,
                'points': [{'point': 10, 'sample': {
                    'value': {'bool': self.point},
                    'quality': {'quality': 'good'}}}],
                'publication': {
                    'published': published,
                    'coalesced': max(0, published - self.window),
                    'depth': min(self.window, published),
                    'window': self.window},
                'command_queue': {
                    'attempts': len(self.receipts),
                    'full_rejections': self.full_rejections,
                    'capacity': self.capacity,
                    'depth': self._depth(),
                    'high_water': self.high_water}}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/history'):
            params = dict(part.split('=', 1) for part in query.split('&'))
            point, since = int(params['point']), int(params['since'])
            first = max(1, self.tick - 1023)
            start = max(since + 1, first)
            samples = [{'seq': seq,
                        'sample': {'value': {'bool': self.point}}}
                       for seq in range(start, self.tick + 1)] \
                if self.tick >= start else []
            return 200, [{'point': point, 'samples': samples}]
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            if self.stale_cursor and since:
                # The named failure: the ring rolled but the read
                # fabricates a seamless continuation from the cursor.
                return 200, [{'seq': seq, 'tick': self.tick, 'event': {}}
                             for seq in range(since + 1, self.next_seq)]
            first = max(1, self.next_seq - self.journal_cap)
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since
                         and entry['seq'] >= first]
        if (method, route) == ('POST', '/command'):
            return 200, self._admit(body)
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The consumer channel — replaces scenarios._request_status: the
    # declared-limit answers the malformed probes assert.
    def request_status(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if method == 'GET':
            if route == '/history':
                bad = 'abc' in query or '-1' in query
                return (400, b'bad query') if bad else (200, b'[]')
            if route == '/journal':
                bad = 'soon' in query
                return (400, b'bad query') if bad else (200, b'[]')
            if route in ('/snapshot', '/receipts', '/checkpoint',
                         '/role', '/signals', '/'):
                return 200, b'{}'
            return 404, b'not found'
        if (method, route) == ('POST', '/command'):
            return 400, b'unparseable'
        if route == '/scan':
            if method != 'POST':
                return 404, b'not found'
            return (409, b'paced') if body == '{"scans":1}' \
                else (400, b'bad request')
        if (method, route) == ('POST', '/promote'):
            return 409, b'already_active'
        return 404, b'not found'

    # The raw transport — replaces scenarios._connect.
    def connect(self, base, timeout=5):
        if self.freeze_on_connect:
            self.freeze = True
        self.sockets.append(FakeSocket(HELD_RESPONSE))
        return self.sockets[-1]

    # The flood channel's other end — replaces scenarios._connect for
    # the command-admission tests: submissions admitted through _admit
    # without advancing the scan, so the queue fills inside one window.
    def flood_connect(self, base, timeout=5):
        self.sockets.append(FloodSocket(self))
        return self.sockets[-1]

    def flood_responses(self, raw):
        replies = []
        for body in _pipelined_bodies(raw):
            receipt = self._admit(body, flood=True)
            status = self.flood_status or 200
            payload = json.dumps(receipt).encode()
            replies.append(
                b'HTTP/1.1 ' + str(status).encode() + b' X\r\n'
                b'Content-Type: application/json\r\nContent-Length: '
                + str(len(payload)).encode() + b'\r\n\r\n' + payload)
        if self.flood_drop and replies:
            replies.pop()
        return b''.join(replies)


class FloodSocket(FakeSocket):
    """The flood channel's socket: the pipelined request stream is
    answered on the first read, one 200 receipt per submission."""

    def __init__(self, feed):
        super().__init__()
        self.feed = feed
        self.served = False

    def recv(self, count):
        if not self.served:
            self.served = True
            self.response = self.feed.flood_responses(self.sent)
        return super().recv(count)


class ConsumerScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = Feed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_request_status',
                             self.feed.request_status), \
                patch.object(scenarios, '_connect', self.feed.connect), \
                patch.object(scenarios, 'LEG_TICKS', 4), \
                patch.object(scenarios, 'LEG_POLL', 0.001), \
                patch.object(scenarios, 'LEG_DEADLINE', 2.0), \
                patch.object(scenarios, 'FLOOD_BATCH', 20):
            return scenarios.scenario_consumer_schedule(ctx)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_stalled_reader_diverged_output_fails(self):
        # The stalled reader's held connection freezes the feed's scan
        # outputs — the leg's named failure, not a silently passed leg.
        self.feed.freeze_on_connect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stalled-reader', record.get('detail', ''))
        self.assertIn('scan outputs stopped advancing',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_silently_stale_cursor_read_fails(self):
        self.feed.stale_cursor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('silently stale', record.get('detail', ''))
        report.validate_scenario(record)


class ServedFeed:
    """A stubbed monitor pair for the served-interface scenario. The rig
    declares two instances whose served interfaces both bind the
    preferred writable bool point — the pump-station model's p101-oos
    fan-out — and the command path settles every submission into a
    journaled receipt `/resources` attributes per instance, mirroring
    the served contract's own rules. Fault flags stage each named
    failure the issue calls out."""

    COMPONENTS = ({'name': 'digital-input:12', 'kind': 'digital-input'},
                  {'name': 'motor:21', 'kind': 'motor'})

    def __init__(self):
        self.tick = 0
        self.journal = []
        self.next_seq = 1
        self.bound = {'digital-input:12': {302}, 'motor:21': {302}}
        # Fault injection for the named-failure cases.
        self.missing_kind = False        # /schema drops an instance
        self.missing_collection = False  # interfaces serve four of five
        self.no_receipt = False          # /command answers an HTTP error
        self.no_events = False           # the produced event never shows

    def _interface(self, kind, port, point):
        interface = {
            'version': 1, 'kind': kind,
            'measurements': [{'name': 'out', 'direction': 'out',
                              'kind': 'bool', 'point': point + 1}],
            'configuration': [{'name': 'invert', 'kind': 'bool',
                               'capability': 'tunable'}],
            'state': [{'name': 'fault', 'direction': 'out',
                       'kind': 'bool', 'persistence': 'bound_point'}],
            'commands': [
                {'name': 'write_value:' + port,
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'bound_point_writable',
                 'adapted': 'write_value', 'point': point},
                {'name': 'set_parameter:invert',
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'always',
                 'adapted': 'set_parameter'}],
            'events': [{'name': 'command_settled', 'payload': [],
                        'retention': 'journal',
                        'emission': 'on_command_settled',
                        'adapted': 'command_settled'}]}
        if self.missing_collection:
            del interface['events']
        return interface

    def _attributed(self, entry, name):
        """The resource view's join: a settled receipt belongs to the
        instance its command names or whose bound points it writes."""
        event = entry.get('event') or {}
        command = ((event.get('command_settled') or {})
                   .get('receipt') or {}).get('command') or {}
        for variant in ('invoke', 'set_parameter'):
            if (command.get(variant) or {}).get('component') == name:
                return True
        for variant in ('write_value', 'force_point', 'unforce_point'):
            point = (command.get(variant) or {}).get('point')
            if point is not None and point in self.bound.get(name, ()):
                return True
        return False

    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self.tick += 1
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 10, 'signal': 10010, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}],
                'components': [dict(c) for c in self.COMPONENTS]}
        if (method, route) == ('GET', '/schema'):
            interfaces = [
                {'name': 'digital-input:12',
                 'interface': self._interface('digital-input', 'in', 302)},
                {'name': 'motor:21',
                 'interface': self._interface('motor', 'oos', 302)}]
            if self.missing_kind:
                interfaces = interfaces[:1]
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': interfaces}
        if (method, route) == ('GET', '/resources'):
            return 200, {
                'publication': self.tick, 'tick': self.tick,
                'components': [
                    {'name': c['name'], 'kind': c['kind'],
                     'measurements': [], 'configuration': [],
                     'state': [], 'commands': [],
                     'events': [] if self.no_events else
                     [entry for entry in self.journal
                      if self._attributed(entry, c['name'])]}
                    for c in self.COMPONENTS]}
        if (method, route) == ('POST', '/command'):
            if self.no_receipt:
                error = urllib.error.HTTPError(
                    url, 500, 'command path broken', {}, None)
                error.close()
                raise error
            receipt = {'command': body['command'],
                       'outcome': {'applied': {'tick': self.tick}},
                       'actor': body.get('actor')}
            self.journal.append(
                {'seq': self.next_seq, 'tick': self.tick,
                 'event': {'command_settled': {'receipt': receipt}}})
            self.next_seq += 1
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class ServedInterfaceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = ServedFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CONTRACT_DEADLINE', 0.05):
            return scenarios.scenario_served_interface(ctx)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_missing_declared_kind_fails(self):
        # The served registry drops an instance the model declares —
        # the coverage check must name the uncovered kind.
        self.feed.missing_kind = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('misses declared kinds', record.get('detail', ''))
        self.assertIn('motor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_collection_fails(self):
        self.feed.missing_collection = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('miss collections', record.get('detail', ''))
        report.validate_scenario(record)

    def test_command_without_receipt_fails(self):
        self.feed.no_receipt = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unobserved_event_fails(self):
        self.feed.no_events = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reflected a produced event',
                      record.get('detail', ''))
        report.validate_scenario(record)


class CommandAdmissionTests(unittest.TestCase):
    """scenario_command_admission against the stubbed feed: the flood
    channel's pipelined submissions fill the fake's bounded queue inside
    one scan window, so the over-limit leg shows the mixed
    settle/reject shape and the under-limit leg all-admitted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = Feed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_connect',
                             self.feed.flood_connect), \
                patch.object(scenarios, 'LEG_TICKS', 4), \
                patch.object(scenarios, 'LEG_POLL', 0.001), \
                patch.object(scenarios, 'LEG_DEADLINE', 2.0), \
                patch.object(scenarios, 'ADMISSION_TRICKLE', 3):
            return scenarios.scenario_command_admission(ctx)

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_command_admission,
                      scenarios.SCENARIOS)

    def test_over_limit_mixed_settle_passes(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The over-limit leg: admitted receipts settled applied at the
        # boundary and the named queue_full rejection appeared.
        self.assertTrue(self.feed.flood_pending)
        self.assertGreater(self.feed.full_rejections, 0)
        flood = json.loads(
            (self.evidence / 'command-admission-flood.json').read_text())
        self.assertEqual(flood['capacity'], self.feed.capacity)
        self.assertGreater(flood['queue_full'], 0)

    def test_under_limit_all_admitted_fails_named_rejection(self):
        # The under-limit leg: every submission is admitted and settles
        # applied — and the scenario names the missing queue_full.
        self.feed.bounded = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('queue_full rejection never appeared',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_dropped_receipt_fails(self):
        self.feed.flood_drop = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_http_layer_error_fails(self):
        self.feed.flood_status = 500
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('HTTP-layer', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_admission_fails(self):
        self.feed.hold_flood = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled applied', record.get('detail', ''))
        report.validate_scenario(record)


class ManagedPlant(threading.Thread):
    """A simulated plant-protocol peer: the newline-delimited JSON
    surface the scenario's field stimulus writes through. One request
    per connection — exactly as `_plant_request` speaks it — and
    `fence` answers writes the field-ownership verdict a promoted
    controller's claim would produce."""

    def __init__(self):
        super().__init__(daemon=True)
        self.tick = 0
        self.points = {120: {'direction': 'in',
                             'value': {'bool': False}}}
        self.fence = False
        self.open = True
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET,
                                 socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.listener.settimeout(0.2)
        self.address = '127.0.0.1:' \
            + str(self.listener.getsockname()[1])
        self.start()

    def _dispatch(self, request):
        op = request.get('op')
        point = request.get('point')
        state = self.points.get(point)
        if op == 'list_points':
            return {'result': 'points', 'points': [
                {'point': p, 'direction': d['direction'],
                 'sample': {'value': d['value'], 'quality': 'good',
                            'tick': self.tick}, 'fault': None}
                for p, d in sorted(self.points.items())]}
        if state is None:
            return {'result': 'error', 'error': {'kind': 'io',
                    'error': {'unknown_point': point}}}
        if op == 'read':
            return {'result': 'sample',
                    'sample': {'value': state['value'],
                               'quality': 'good', 'tick': self.tick}}
        if op == 'write':
            if self.fence:
                return {'result': 'error', 'error': {'kind': 'io',
                        'error': {'fenced': {'point': point}}}}
            state['value'] = request['value']
            self.tick += 1
            return {'result': 'done'}
        return {'result': 'error',
                'error': {'kind': 'invalid_request',
                          'detail': 'unsupported op ' + str(op)}}

    def run(self):
        while self.open:
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            try:
                data = b''
                while not data.endswith(b'\n'):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                if data:
                    conn.sendall(json.dumps(
                        self._dispatch(json.loads(data))).encode()
                        + b'\n')
            except Exception:
                pass
            finally:
                conn.close()

    def close(self):
        self.open = False
        self.listener.close()
        self.join(timeout=2)


class ManagedFeed:
    """A stubbed monitor for the managed-alarm scenario, mirroring the
    pump-station fixture's managed layout: the Bool alarm comp10 reads
    its condition off the plant-held field point 120; comp5's `shelve`
    binds a non-writable point (the never-shelvable refusal); comp6
    shelves under max_shelve_ticks=8; comp21/comp35 carry the per-pump
    OOS-driven suppression wiring (oos 302/334 -> suppress 331/363).

    Every measurement-channel call is one completed scan: accepted
    writes apply at their boundary, the image reads the field point,
    the machines evaluate — latch on the Bool alarm's rising edge,
    bounded shelve counting the request's scans, the OOS copy feeding
    suppress — and each transition lands in the journal the real
    monitor serves. Fault flags stage each named failure."""

    WRITABLE = {1050, 1010, 1011, 302, 334}
    # comp10's alarm/unacknowledged — the points the `status_absent`
    # fault omits from the served snapshot.
    ABSENT_UNDER_FAULT = {1053, 1054}

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.next_seq = 1
        self.journal = []
        self.receipts = []
        self.values = {point: False for point in (
            120, 202, 203, 302, 314, 331, 334, 346, 363,
            1000, 1001, 1010, 1011, 1050, 1060, 1090,
            1003, 1004, 1005, 1006, 1007, 1013, 1014, 1015, 1016, 1017,
            1053, 1054, 1055, 1056, 1057,
            1063, 1064, 1065, 1066, 1067,
            1093, 1094, 1095, 1096, 1097)}
        self.latched = {'10': False, '21': False, '35': False}
        self.prev_alarm = dict(self.latched)
        self.shelve_elapsed = 0
        # Fault injection for the named-failure cases.
        self.alarm_never = False    # the driven alarm never asserts
        self.no_journal = False     # transitions never reach the journal
        self.no_settle = False      # accepted commands never settle
        self.no_release = False     # shelved never auto-releases
        self.no_refusal = False     # the never-shelvable write applies
        self.no_oos_status = False  # OOS-driven statuses never report
        self.status_absent = False  # snapshot omits monitored statuses
        self.no_managed = False     # /schema serves no managed alarms

    def _set(self, point, want):
        want = bool(want)
        if self.values[point] == want:
            return
        self.values[point] = want
        if self.no_journal:
            return
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {'point_changed': {
                                 'point': point,
                                 'from': {'bool': not want},
                                 'to': {'bool': want}}}})
        self.next_seq += 1

    def _journal_receipt(self, receipt):
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {'command_settled': {
                                 'receipt': receipt}}})
        self.next_seq += 1

    def _admit(self, body):
        write = body['command']['write_value']
        if write['point'] in self.WRITABLE or self.no_refusal:
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
        else:
            receipt = {'command': body['command'],
                       'outcome': {'rejected': {'reason': {
                           'not_writable': {'point': write['point']}}}},
                       'actor': body.get('actor')}
            self._journal_receipt(receipt)
        self.receipts.append(receipt)
        return receipt

    def _advance(self):
        """One completed scan: apply the writes whose boundary has
        arrived, read the field, evaluate the machines."""
        self.tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted and self.tick >= accepted['apply_tick']:
                write = receipt['command']['write_value']
                self._set(write['point'], write['value']['bool'])
                if self.no_settle:
                    continue
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                self._journal_receipt(receipt)
        # The field read: the image tracks the plant-held input.
        state = self.plant.points.get(120)
        self._set(120, state['value'].get('bool') if state else False)
        # The OOS copy chain: the suppress inputs follow the OOS point.
        self._set(331, self.values[302])
        self._set(363, self.values[334])
        # The Bool latching alarms: a rising edge latches
        # unacknowledged; the level-observed ack clears it.
        for key, in_pt, ack_pt, alarm_pt, unack_pt in (
                ('10', 120, 1050, 1053, 1054),
                ('21', 314, 1060, 1063, 1064),
                ('35', 346, 1090, 1093, 1094)):
            new = self.values[in_pt] and not self.alarm_never
            if self.values[ack_pt]:
                self.latched[key] = False
            elif new and not self.prev_alarm[key]:
                self.latched[key] = True
            self._set(alarm_pt, new)
            self._set(unack_pt, self.latched[key])
            self.prev_alarm[key] = new
        # comp6's bounded shelve: asserts on the request's first scan,
        # releases at the declared bound while the request still stands
        # — the `no_release` fault holds it asserted past the bound.
        self.shelve_elapsed = self.shelve_elapsed + 1 \
            if self.values[1011] else 0
        self._set(1015, self.values[1011]
                  and (self.no_release or self.shelve_elapsed <= 8))
        # The OOS-driven statuses follow the declared wiring.
        self._set(1067, self.values[302] and not self.no_oos_status)
        self._set(1066, self.values[331] and not self.no_oos_status)
        self._set(1097, self.values[334] and not self.no_oos_status)
        self._set(1096, self.values[363] and not self.no_oos_status)

    def _interface(self, kind, ports):
        """One managed-alarm interface entry — port name to bound point
        split across the measurements and state collections the way
        the served registry lays them out."""
        return {'name': ports['name'],
                'interface': {'kind': kind,
                              'measurements': [
                                  {'name': name, 'direction': 'in',
                                   'kind': 'bool', 'point': point}
                                  for name, point in ports['m']],
                              'state': [
                                  {'name': name, 'direction': 'in',
                                   'kind': 'bool', 'point': point}
                                  for name, point in ports['s']]}}

    def _interfaces(self):
        entries = [
            self._interface('managed-latching-alarm', {
                'name': 'managed-latching-alarm:5',
                'm': [('in', 202), ('ack', 1000), ('alarm', 1003),
                      ('unacknowledged', 1004)],
                's': [('shelve', 1001), ('shelved', 1005),
                      ('suppressed', 1006), ('out_of_service', 1007)]}),
            self._interface('managed-latching-alarm', {
                'name': 'managed-latching-alarm:6',
                'm': [('in', 203), ('ack', 1010), ('alarm', 1013),
                      ('unacknowledged', 1014)],
                's': [('shelve', 1011), ('shelved', 1015),
                      ('suppressed', 1016), ('out_of_service', 1017)]}),
            self._interface('managed-bool-latching-alarm', {
                'name': 'managed-bool-latching-alarm:10',
                'm': [('in', 120), ('ack', 1050), ('alarm', 1053),
                      ('unacknowledged', 1054)],
                's': [('shelved', 1055), ('suppressed', 1056),
                      ('out_of_service', 1057)]}),
            self._interface('managed-bool-latching-alarm', {
                'name': 'managed-bool-latching-alarm:21',
                'm': [('in', 314), ('ack', 1060), ('alarm', 1063),
                      ('unacknowledged', 1064)],
                's': [('oos', 302), ('suppress', 331),
                      ('shelved', 1065), ('suppressed', 1066),
                      ('out_of_service', 1067)]}),
            self._interface('managed-bool-latching-alarm', {
                'name': 'managed-bool-latching-alarm:35',
                'm': [('in', 346), ('ack', 1090), ('alarm', 1093),
                      ('unacknowledged', 1094)],
                's': [('oos', 334), ('suppress', 363),
                      ('shelved', 1095), ('suppressed', 1096),
                      ('out_of_service', 1097)]})]
        if self.no_managed:
            return []
        return entries

    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': point, 'signal': None,
                 'name': 'point-' + str(point), 'direction': 'in',
                 'value_type': 'bool',
                 'writable': point in self.WRITABLE}
                for point in sorted(self.values)], 'components': []}
        if (method, route) == ('GET', '/schema'):
            return 200, {'tick': self.tick,
                         'interfaces': self._interfaces()}
        if (method, route) == ('GET', '/snapshot'):
            points = [{'point': point,
                       'sample': {'value': {'bool': value},
                                  'quality': 'good'}}
                      for point, value in sorted(self.values.items())]
            if self.status_absent:
                points = [entry for entry in points
                          if entry['point']
                          not in self.ABSENT_UNDER_FAULT]
            return 200, {'tick': self.tick, 'points': points,
                         'parameters': [
                             {'name': 'managed-latching-alarm:5',
                              'values': {'max_shelve_ticks':
                                         {'int': 0}}},
                             {'name': 'managed-latching-alarm:6',
                              'values': {'max_shelve_ticks':
                                         {'int': 8}}}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            return 200, self._admit(body)
        raise AssertionError('unexpected request %s %s' % (method, url))


class ManagedAlarmLifecycleTests(unittest.TestCase):
    """scenario_managed_alarm_lifecycle against the stubbed monitor and
    a live plant-protocol peer: each leg's pass shape plus the named
    failures — an unasserted driven condition, missing journaled
    evidence, an unsettled receipt, a shelve that never auto-releases,
    a never-shelvable write that applies, OOS statuses that never
    report — and the inconclusive answers the lane owes when a
    monitored status never reports or the field is already claimed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = ManagedPlant()
        self.feed = ManagedFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'ALARM_POLL', 0.001), \
                patch.object(scenarios, 'ALARM_DEADLINE', 3.0):
            return scenarios.scenario_managed_alarm_lifecycle(ctx)

    def test_registered_before_failover(self):
        # The plant write rides an unclaimed field; a promotion takes
        # the single-writer claim, so the case runs ahead of failover.
        order = list(scenarios.SCENARIOS)
        self.assertLess(order.index(
            scenarios.scenario_managed_alarm_lifecycle),
            order.index(scenarios.scenario_failover))

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The lifecycle evidence: assert the shelve leg recorded the
        # declared bound and the refusal leg the named rejection.
        shelve = json.loads(
            (self.evidence / 'managed-alarm-shelve.json').read_text())
        self.assertEqual(shelve['max_shelve_ticks'], 8)
        self.assertEqual(shelve['request_standing'], True)
        refusal = json.loads(
            (self.evidence / 'managed-alarm-refusal.json').read_text())
        self.assertEqual(refusal['settled'], 'rejected:not_writable')
        oos = json.loads(
            (self.evidence / 'managed-alarm-oos.json').read_text())
        self.assertEqual(oos['component'],
                         'managed-bool-latching-alarm:21')

    def test_driven_condition_unasserted_fails(self):
        self.feed.alarm_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unasserted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_activation_without_journal_evidence_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no journaled point_changed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unsettled_receipt_fails(self):
        self.feed.no_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled applied',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_shelve_never_releases_fails(self):
        self.feed.no_release = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('auto-released', record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_shelvable_applies_fails(self):
        self.feed.no_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not refuse NotWritable',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_oos_statuses_never_report_fails(self):
        self.feed.no_oos_status = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('out_of_service/suppressed statuses',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_status_never_reports_is_inconclusive(self):
        # A monitored status that never reports is an inconclusive
        # answer, not the product failure the lane names.
        self.feed.status_absent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never report', record.get('detail', ''))
        report.validate_scenario(record)

    def test_claimed_field_is_inconclusive(self):
        # The plant's write-ownership already claimed — the fencing
        # verdict — is an environment answer, not a managed-alarm
        # failure.
        self.plant.fence = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('field-write ownership is already claimed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_oos_already_standing_uses_the_other_pump(self):
        # With comp21's OOS point already standing — the shape an
        # earlier suite leg leaves behind — the leg drives comp35's.
        self.feed.values[302] = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        oos = json.loads(
            (self.evidence / 'managed-alarm-oos.json').read_text())
        self.assertEqual(oos['component'],
                         'managed-bool-latching-alarm:35')
        report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
