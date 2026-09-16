"""The consumer-schedule and command-admission scenarios' unit coverage:
a stubbed monitor feed drives each through its pass outcome and the
named failures the issues call out — a stalled reader whose leg's scan
outputs stopped advancing, a lagging seq-cursor read answered with
silently stale data, and a command flood whose submissions meet dropped
receipts, HTTP-layer faults, unsettled admissions, or a bound that never
fills."""
import json
import tempfile
import unittest
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


if __name__ == '__main__':
    unittest.main()
