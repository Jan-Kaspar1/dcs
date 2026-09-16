"""The deterministic scenarios' unit coverage: stubbed monitor feeds
drive scenario_consumer_schedule, scenario_served_interface,
scenario_force_release, and scenario_command_admission through their
pass outcomes and the named failures their issues call out — a stalled
reader whose leg's scan outputs stopped advancing, a lagging seq-cursor
read answered with silently stale data, a served registry missing a
declared kind or collection, a declared command returning no receipt,
an emitted-events view that never reflects the produced event, forced
telemetry missing its Substituted stamp or forces badge, control that
ignores the force, unattributed or never-journaled settlements, a badge
that never clears, recovery that never returns to Good, and a command
flood whose submissions meet dropped receipts, HTTP-layer faults,
unsettled admissions, or a bound that never fills."""
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from qa_lane import report, scenarios, verify


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


class RestartFeed:
    """A stubbed pair for the controller-restart scenario. ctrl-a owns
    the field and persists every scan — the --state-file checkpoint is
    `persisted` — while ctrl-b tracks it and degrades during the
    restart gap. The journal file is a real append-only record the
    feed writes itself: a run_boundary marker per process lifetime and
    one entry per settled command, matching the durable record's
    format. Fault flags stage each named failure the issue calls
    out."""

    def __init__(self, journal_path):
        self.tick = 100      # well past the resume slack
        self.persisted = 100
        self.point = False
        self.up = True           # ctrl-a's monitor answers
        self.serves = True       # False: the monitor never returns
        self.returns = True      # False: the restart action fails
        self.cold = False        # restart resumes nothing
        self.regress = False     # resume lands far behind
        self.loses_state = False  # written point value does not persist
        self.seq_restarts = False  # journal seq numbering restarts
        self.peer_promoted = False
        self.restarts = []
        self.path = Path(journal_path)
        self.next_seq = 1
        self.runs = 1
        self._append({'run_boundary': {'run': 1, 'tick': 0}})

    def _append(self, record):
        with self.path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _journal_entry(self):
        self._append({'entry': {'seq': self.next_seq,
                                'tick': self.tick, 'event': {}}})
        self.next_seq += 1

    def _scan(self):
        # One completed scan per snapshot read; the state file follows
        # at the same end-of-cycle boundary.
        self.tick += 1
        self.persisted = self.tick

    # The runner-owned lifecycle action — replaces
    # ctx['restart_controller'].
    def restart(self, name):
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        self.up = False
        self.down_left = 2  # refused polls before the monitor returns
        resumed = self.persisted
        if self.cold:
            resumed = 0
        if self.regress:
            resumed = max(1, resumed - 100)
        self.tick = resumed
        if self.loses_state:
            self.point = False
        self.runs += 1
        self._append({'run_boundary': {'run': self.runs,
                                       'tick': resumed}})
        if self.seq_restarts:
            self.next_seq = 1

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                if self.peer_promoted:
                    return 200, {'role': 'active', 'tick': self.tick}
                sync = {'tracking': {'aligned': self.tick}} if self.up \
                    else {'degraded': {'detail': 'checkpoint pull '
                                       'failed'}}
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': sync}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.up:
            self.down_left -= 1
            if self.down_left <= 0 and self.serves:
                self.up = True
            else:
                raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 20, 'signal': None, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}]}
        if (method, route) == ('GET', '/snapshot'):
            self._scan()
            return 200, {'tick': self.tick, 'points': [
                {'point': 10, 'sample': {
                    'value': {'bool': self.point},
                    'quality': {'quality': 'good'}}}]}
        if (method, route) == ('POST', '/command'):
            write = body['command']['write_value']
            self.point = write['value']['bool']
            receipt = {'command': body['command'],
                       'outcome': {'applied': {'tick': self.tick}},
                       'actor': body.get('actor')}
            self._journal_entry()
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class ControllerRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'controllers' / 'a'
        self.journal.mkdir(parents=True)
        self.journal = self.journal / 'journal.jsonl'
        self.feed = RestartFeed(self.journal)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, **patches):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'restart_controller': self.feed.restart,
               'journal_files': {'active': str(self.journal),
                                 'standby': str(self.journal)},
               'state_files': {}}
        defaults = {'POLL_INTERVAL': 0.001, 'RESTART_POLL': 0.001,
                    'RESTART_RETURN_DEADLINE': 0.5,
                    'RESTART_SETTLE_DEADLINE': 0.5,
                    'RESTART_JOURNAL_DEADLINE': 0.3}
        defaults.update(patches)
        with patch.object(scenarios, 'http_json', self.feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_controller_restart(ctx)

    def test_clean_restart_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.restarts, ['active'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_cold_start_resume_fails(self):
        self.feed.cold = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('regressed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_stale_resume_fails(self):
        self.feed.regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('regressed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_lost_point_state_fails(self):
        self.feed.loses_state = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('lost its written value',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_seq_restart_fails(self):
        self.feed.seq_restarts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('seqs', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_boundary_fails(self):
        # A restarted lifetime that never marks its boundary: the file
        # holds run 1's records only.
        feed = self.feed

        def restart(name):
            feed.restarts.append(name)
            feed.up, feed.down_left = False, 2
            feed.tick = feed.persisted

        self.feed.restart = restart
        ctx_record = self.run_scenario()
        self.assertEqual(ctx_record['outcome'], 'failed', ctx_record)
        self.assertIn('run-boundary', ctx_record.get('detail', ''))
        report.validate_scenario(ctx_record)

    def test_spurious_peer_promotion_fails(self):
        self.feed.peer_promoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reported active', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unfinished_restart_is_inconclusive(self):
        self.feed.returns = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restart action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_monitor_is_inconclusive(self):
        self.feed.serves = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never returned', record.get('detail', ''))
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


class ForceFeed:
    """A stubbed monitor pair for the force-release scenario: a tiny
    internal-point executor over the rig's writable p101-oos point and
    its inverted p101-oos-ok carrier. Every call on the measurement
    channel is one completed scan — reads observe, commands queue for
    the next scan boundary and journal as they settle — mirroring the
    held-value/force substitution semantics the executor documents for
    an internal `In` point. Fault flags stage each named failure the
    issue calls out."""

    def __init__(self):
        self.tick = 0
        self.held = False       # p101-oos's held operator value
        self.force = None       # the forced value while a force stands
        self.receipts = []
        self.journal = []
        self.next_seq = 1
        # Fault injection for the named-failure cases.
        self.force_unseen = False     # telemetry never shows the force
        self.forces_omitted = False   # the forces list stays empty
        self.control_ignores = False  # oos-ok never follows the force
        self.never_settled = False    # commands apply but never settle
        self.wrong_actor = False      # settled receipts lose attribution
        self.release_sticks = False   # unforce never clears the force
        self.release_refused = False  # unforce is rejected at submission
        self.no_recovery = False      # the point never reads Good again
        self.no_oos = False           # the signal index lacks the target

    # The plant half: one completed scan per measurement call, applying
    # each accepted command whose apply_tick has arrived — the force map
    # substitutes the point's read while it stands, and the held-value
    # rule keeps the last stamp once a release lands.
    def _advance(self):
        self.tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted is None or self.tick < accepted['apply_tick']:
                continue
            command = receipt['command']
            if 'force_point' in command:
                self.force = command['force_point']['value']['bool']
            elif 'unforce_point' in command:
                if not self.release_sticks:
                    self.force = None
            elif 'write_value' in command:
                self.held = command['write_value']['value']['bool']
            if self.never_settled:
                continue
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            settled = dict(receipt)
            if self.wrong_actor:
                settled['actor'] = 'the-plant-server'
            self.journal.append(
                {'seq': self.next_seq, 'tick': self.tick,
                 'event': {'command_settled': {'receipt': settled}}})
            self.next_seq += 1

    # What the scan's input read reports for point 302: the forced value
    # at Substituted while a force stands, else the held value — whose
    # quality this stub can hold at Substituted to model a release that
    # never recovers Good.
    def _oos_sample(self):
        if self.force is not None and not self.force_unseen:
            return {'value': {'bool': self.force},
                    'quality': {'uncertain': 'substituted'},
                    'tick': self.tick}
        quality = {'uncertain': 'substituted'} if self.no_recovery \
            else 'good'
        return {'value': {'bool': self.held}, 'quality': quality,
                'tick': self.tick}

    # digital-input:12's inverted carrier: p101-oos-ok = NOT the
    # observed oos sample, propagating its quality — the control image
    # the scenario watches follow the force.
    def _oos_ok_sample(self, oos):
        observed = oos['value']['bool']
        driven = not self.held if self.control_ignores else not observed
        return {'value': {'bool': driven}, 'quality': oos['quality'],
                'tick': self.tick}

    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 308, 'signal': 10308, 'name': 'p101-oos-ok',
                 'direction': 'out', 'value_type': 'bool',
                 'writable': False}]
            if self.no_oos:
                points = points[1:]
            return 200, {'points': points, 'components': []}
        if (method, route) == ('GET', '/snapshot'):
            oos = self._oos_sample()
            forces = [] if self.force is None or self.forces_omitted \
                else [{'point': 302, 'value': {'bool': self.force}}]
            return 200, {
                'tick': self.tick, 'forces': forces,
                'points': [
                    {'point': 302, 'direction': 'in', 'sample': oos},
                    {'point': 308, 'direction': 'out',
                     'sample': self._oos_ok_sample(oos)}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            command = body['command']
            if 'unforce_point' in command and self.release_refused:
                return 200, {'command': command,
                             'outcome': {'rejected': {'reason': {
                                 'not_writable': {'point': 302}}}},
                             'actor': body.get('actor')}
            receipt = {'command': command,
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class ForceReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = ForceFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'FORCE_DEADLINE', 0.5):
            return scenarios.scenario_force_release(ctx)

    def test_registered_and_replayable(self):
        # The case joins the deterministic set, and the verification
        # lane's case-identity lookup resolves it back to its function.
        self.assertIn(scenarios.scenario_force_release,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('force-release'),
                      scenarios.scenario_force_release)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        self.assertTrue(
            any('Substituted' in note for note in record['observations']))
        self.assertTrue(
            any('journal' in note for note in record['observations']))

    def test_forced_telemetry_never_substitutes_fails(self):
        self.feed.force_unseen = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('forced telemetry never showed',
                      record.get('detail', ''))
        self.assertIn('Substituted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_forces_list_omits_point_fails(self):
        self.feed.forces_omitted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('snapshot.forces', record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_ignoring_the_force_fails(self):
        self.feed.control_ignores = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('control following the force',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_commands_never_settling_fails(self):
        self.feed.never_settled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no settled force receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_receipts_without_attribution_fail(self):
        self.feed.wrong_actor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unattributed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_release_that_never_clears_the_badge_fails(self):
        self.feed.release_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('forces badge never cleared',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_release_refused_fails(self):
        self.feed.release_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('release refused', record.get('detail', ''))
        report.validate_scenario(record)

    def test_recovery_that_never_reads_good_fails(self):
        self.feed.no_recovery = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not recover', record.get('detail', ''))
        self.assertIn('Good quality', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_force_target_is_inconclusive(self):
        self.feed.no_oos = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
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
