"""The deterministic scenarios' unit coverage: stubbed monitor feeds
drive scenario_consumer_schedule, scenario_served_interface,
scenario_force_release, scenario_command_admission,
scenario_controller_restart, scenario_plant_link_loss,
scenario_field_fault, and scenario_model_revision through their
pass outcomes and the named
failures their issues call out — a stalled reader whose leg's scan
outputs stopped advancing, a lagging seq-cursor read answered with
silently stale data, a served registry missing a declared kind or
collection, a declared command returning no receipt, an
emitted-events view that never reflects the produced event, forced
telemetry missing its Substituted stamp or forces badge, control that
ignores the force, unattributed or never-journaled settlements, a
badge that never clears, recovery that never returns to Good, a
command flood whose submissions meet dropped receipts, HTTP-layer
faults, unsettled admissions, or a bound that never fills, a restarted
controller that resumes cold, a plant outage whose telemetry stays
fresh, whose standby promotes, whose writer claim never re-arms, or
whose io_health forgets the failures it counted, an injected quality
fault the served snapshot keeps reporting Good, an error fault that
never surfaces on io_health, a role that moves under a field fault,
a clear that never restores the field value, a model revision
whose revised peer never converges, converges to the wrong sync
state, loses receipts across the boundary, regresses the field, or
ends on the wrong fingerprint, a foreign-fingerprint standby
that never reports the named negotiation refusal, converges anyway,
accepts promotion, or leaves the active peer disturbed, and an
incompatible revision whose peer silently crosses, degrades on the
wrong detail, promotes anyway, answers the wrong refusal, or disturbs
the active's receipts, journal, or field — plus a control half that
refuses to converge."""
import io
import json
import socket
import tempfile
import threading
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
    restart gap. Commands queue at admission and apply at the next
    scan's boundary, so a stop inside the admission-to-application
    window leaves the Accepted receipt riding the checkpoint — the
    resumed run re-queues it. Each peer's --journal-file is a real
    append-only record the feed writes itself: a run_boundary marker
    per process lifetime, the restart's served run_boundary entry, one
    entry per settled command, and the declared-journaled point's
    value transitions — matching the durable record's format. Fault
    flags stage each named failure the issue calls out."""

    def __init__(self, journal_path, peer_journal_path):
        self.tick = 100      # well past the resume slack
        self.persisted = 100
        self.point = False
        self.pending = []        # accepted receipts awaiting a scan
        self.settled = []        # journaled receipts (the replay fold)
        self.served = []         # the monitor's served journal ring
        self.up = True           # ctrl-a's monitor answers
        self.serves = True       # False: the monitor never returns
        self.returns = True      # False: the restart action fails
        self.cold = False        # restart resumes nothing
        self.regress = False     # resume lands far behind
        self.seq_restarts = False  # journal seq numbering restarts
        self.peer_promoted = False
        # The extended scenario's named failures.
        self.applies_early = False    # the second write lands pre-stop
        self.drops_pending = False    # resume loses the carried receipt
        self.replays_receipts = False  # resume re-journals settlements
        self.phantom_census = False   # resume re-journals the census
        self.peer_disturbed = False   # the peer's journal gains a run
        self.skips_served_boundary = False  # the served marker never seeds
        self.restarts = []
        self.path = Path(journal_path)
        self.peer_path = Path(peer_journal_path)
        self.next_seq = 1
        self.runs = 1
        self._append(self.path, {'run_boundary': {'run': 1, 'tick': 0}})
        # Run 1's observation record — the baseline the resumed run's
        # replayed fold diffs its standing points against.
        self._entry({'quality_changed': {'point': 10, 'from': None,
                                         'to': 'good'}})
        self._entry({'point_changed': {'point': 10, 'from': None,
                                       'to': {'bool': False}}})
        self._append(self.peer_path,
                     {'run_boundary': {'run': 1, 'tick': 0}})

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, event):
        entry = {'seq': self.next_seq, 'tick': self.tick,
                 'event': event}
        self._append(self.path, {'entry': entry})
        self.served.append(entry)
        self.next_seq += 1

    def _apply_pending(self):
        # The scan boundary's command phase: queued receipts settle,
        # the journaled point's transition records, and the receipt
        # log carries the settled outcome.
        for receipt in self.pending:
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            new = write['value']['bool']
            if new != self.point:
                self._entry({'point_changed': {
                    'point': write['point'],
                    'from': {'bool': self.point},
                    'to': {'bool': new}}})
                self.point = new
            self._entry({'command_settled': {'receipt': receipt}})
            self.settled.append(receipt)
        self.pending = []

    def _scan(self):
        # One completed scan per snapshot read; queued commands apply
        # at its boundary and the state file follows at end of cycle.
        self.tick += 1
        self._apply_pending()
        self.persisted = self.tick

    # The runner-owned lifecycle action — replaces
    # ctx['restart_controller'].
    def restart(self, name):
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        if self.applies_early:
            # The stop landed late: the queued write's applying scan
            # ran before the process died — the settlement journals
            # ahead of the run boundary.
            self.tick += 1
            self._apply_pending()
            self.persisted = self.tick
        self.up = False
        self.down_left = 2  # refused polls before the monitor returns
        resumed = self.persisted
        if self.cold:
            resumed = 0
        if self.regress:
            resumed = max(1, resumed - 100)
        self.tick = resumed
        if self.drops_pending:
            # The checkpoint never carried the Accepted receipt — the
            # command is lost unaudited, the failure the admission-time
            # persist closed. The pre-application value stands.
            self.pending = []
        self.runs += 1
        self._append(self.path, {'run_boundary': {'run': self.runs,
                                                  'tick': resumed}})
        # The served form of the marker: journaled once on replay,
        # taking the next seq like any event.
        if not self.skips_served_boundary:
            self._entry({'run_boundary': {'run': self.runs}})
        if self.seq_restarts:
            self.next_seq = 1
        if self.replays_receipts:
            # The resumed run re-journals the settled receipts its
            # checkpoint still carries — the restart-integrity
            # regression the finding closed.
            for receipt in self.settled:
                self._entry({'command_settled': {'receipt': receipt}})
        if self.phantom_census:
            # The resumed run diffs its standing points against
            # nothing and re-journals the whole census as first
            # observations.
            self._entry({'quality_changed': {'point': 10, 'from': None,
                                             'to': 'good'}})
            self._entry({'point_changed': {'point': 10, 'from': None,
                                           'to': {'bool': self.point}}})
        if self.peer_disturbed:
            self._append(self.peer_path,
                         {'run_boundary': {'run': 2, 'tick': resumed}})

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
        if (method, route) == ('GET', '/journal'):
            return 200, list(self.served)
        if (method, route) == ('POST', '/command'):
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.pending.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class ControllerRestartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal.parent.mkdir(parents=True)
        self.peer_journal = Path(self.tmp.name) / 'controllers' / 'b' \
            / 'journal.jsonl'
        self.peer_journal.parent.mkdir(parents=True)
        self.feed = RestartFeed(self.journal, self.peer_journal)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, **patches):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'restart_controller': feed.restart,
               'journal_files': {'active': str(self.journal),
                                 'standby': str(self.peer_journal)},
               'state_files': {}}
        defaults = {'POLL_INTERVAL': 0.001, 'RESTART_POLL': 0.001,
                    'RESTART_RETURN_DEADLINE': 0.5,
                    'RESTART_SETTLE_DEADLINE': 0.5,
                    'RESTART_JOURNAL_DEADLINE': 0.3,
                    'RESTART_COMMAND_DEADLINE': 0.3}
        defaults.update(patches)
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_controller_restart(ctx)

    def test_clean_restart_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.restarts, ['active'])
        # The restart-window command rode the checkpoint: its
        # settlement landed past the resumed run's boundary.
        self.assertTrue(any('post-boundary' in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_applied_before_stop_passes(self):
        # The other admissible answer: the queued write's applying
        # scan ran before the container died — the settlement journals
        # ahead of the run boundary and still satisfies the contract.
        self.feed.applies_early = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(any('pre-boundary' in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)

    def test_dropped_pending_command_fails(self):
        # The failure the admission-time persist closed: the carried
        # Accepted receipt never re-queues — the point keeps its
        # pre-application value and the journal stays silent on the
        # settlement.
        self.feed.drops_pending = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('silently lost', record.get('detail', ''))
        report.validate_scenario(record)

    def test_replayed_receipts_fail(self):
        # The resumed run re-journals the settled receipts its
        # checkpoint still carries past the run boundary — the
        # restart-integrity regression.
        self.feed.replays_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('re-journaled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_phantom_census_fails(self):
        self.feed.phantom_census = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('phantom', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_served_boundary_fails(self):
        self.feed.skips_served_boundary = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('served journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_disturbed_peer_journal_fails(self):
        self.feed.peer_disturbed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('peer', record.get('detail', ''))
        report.validate_scenario(record)

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

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs of the scenario
        # against the same rig layout record the same report and the
        # same evidence files — the feed's transitions are call-count
        # keyed, never wall-clock.
        runs = []
        for _index in range(2):
            for path in (self.journal, self.peer_journal):
                if path.exists():
                    path.unlink()
            for stale in self.evidence.iterdir():
                stale.unlink()
            feed = RestartFeed(self.journal, self.peer_journal)
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_bytes()
                                  for p in self.evidence.iterdir()}))
        self.assertEqual(runs[0][0]['outcome'], 'passed', runs[0][0])
        self.assertEqual(runs[0], runs[1])


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


class FakePlantPeer:
    """A plant-protocol peer on 127.0.0.1: a real listener speaking the
    documented newline-JSON request/response surface — list_points,
    read, inject_fault, clear_fault — over a fixed table of stable
    in-points with per-point fault state, mirroring the plant server's
    semantics: a quality fault substitutes the served sample's quality,
    an error fault answers the point's IoError."""

    def __init__(self):
        self.samples = {
            20: {'value': {'float': 1.5}, 'quality': 'good', 'tick': 0},
            40: {'value': {'bool': False}, 'quality': 'good',
                 'tick': 0},
        }
        self.faults = {}
        self.requests = []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR,
                                 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(4)
        self.listener.settimeout(30)
        self.address = '127.0.0.1:' \
            + str(self.listener.getsockname()[1])
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while True:
                try:
                    conn, _ = self.listener.accept()
                except OSError:
                    return
                threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True).start()
        finally:
            self.listener.close()

    def _handle(self, conn):
        try:
            buffer = b''
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    if line:
                        response = self.dispatch(json.loads(line))
                        conn.sendall(json.dumps(response).encode()
                                     + b'\n')
        except OSError:
            pass
        finally:
            conn.close()

    def served(self, point):
        """The sample a reader observes: stored value, injected
        quality applied — error faults live at the read boundary."""
        sample = dict(self.samples[point])
        fault = self.faults.get(point)
        if isinstance(fault, dict) and 'quality' in fault:
            sample['quality'] = fault['quality']
        return sample

    def dispatch(self, request):
        self.requests.append(request)
        op, point = request.get('op'), request.get('point')
        if op == 'list_points':
            return {'result': 'points', 'points': [
                {'point': p, 'direction': 'in', 'sample': self.served(p),
                 'fault': self.faults.get(p)}
                for p in sorted(self.samples)]}
        if op == 'read':
            fault = self.faults.get(point)
            if fault in ('disconnected', 'timeout'):
                return {'result': 'error',
                        'error': {'kind': 'io', 'error': {fault: point}}}
            return {'result': 'sample', 'sample': self.served(point)}
        if op == 'inject_fault':
            self.faults[point] = request.get('fault')
            return {'result': 'done'}
        if op == 'clear_fault':
            self.faults.pop(point, None)
            return {'result': 'done'}
        return {'result': 'error',
                'error': {'kind': 'invalid_request',
                          'detail': 'unknown op'}}

    def close(self):
        self.listener.close()
        self.thread.join(timeout=5)


class FieldFaultFeed:
    """A stubbed monitor pair for the field-fault scenario. Each
    `http_json` call is one completed scan: the snapshot serves every
    plant point with the quality its fault state implies — substituted
    quality under a quality fault, bad:communication_fault plus the
    io_health counters under an error fault — and the role stays
    active. Fault flags stage each named failure the issue calls out."""

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.failed_reads = 0
        self.last_error = None
        self.ever_faulted = set()
        # Fault injection for the named-failure cases.
        self.ignore_quality_fault = False  # snapshot keeps serving Good
        self.hide_io_fault = False         # io_health never counts
        self.demote_on_fault = False       # a field fault moves the role
        self.stuck_recovery = False        # a cleared point stays bad

    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self.tick += 1
        if (method, route) == ('GET', '/role'):
            role = 'active'
            if self.demote_on_fault and self.plant.faults:
                role = 'standby'
            return 200, {'role': role, 'tick': self.tick}
        if (method, route) == ('GET', '/snapshot'):
            points = []
            for point in sorted(self.plant.samples):
                fault = self.plant.faults.get(point)
                if isinstance(fault, dict) and 'quality' in fault:
                    self.ever_faulted.add(point)
                quality = 'good'
                if isinstance(fault, dict) and 'quality' in fault \
                        and not self.ignore_quality_fault:
                    quality = fault['quality']
                elif fault in ('disconnected', 'timeout'):
                    quality = {'bad': 'communication_fault'}
                    if not self.hide_io_fault:
                        self.failed_reads += 1
                        self.last_error = {
                            'tick': self.tick, 'point': point,
                            'direction': 'in',
                            'error': {fault: point}}
                if self.stuck_recovery and point in self.ever_faulted:
                    quality = {'bad': 'device_fault'}
                points.append({'point': point, 'direction': 'in',
                               'sample': dict(
                                   self.plant.samples[point],
                                   quality=quality,
                                   tick=self.tick)})
            return 200, {
                'tick': self.tick, 'points': points,
                'io_health': {
                    'failed_reads': self.failed_reads,
                    'failed_writes': 0,
                    'consecutive_failures': 0,
                    'last_error': self.last_error,
                    'scan_overruns': 0,
                    'driver': {'link': 'connected',
                               'last_error': None,
                               'exchange': None}}}
        raise AssertionError('unexpected request %s %s' % (method, url))


class FieldFaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = FakePlantPeer()
        self.feed = FieldFaultFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': self.plant.address,
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'FAULT_PROBE', 0.01), \
                patch.object(scenarios, 'FAULT_DEADLINE', 2.0):
            return scenarios.scenario_field_fault(ctx)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The conversation stayed on the documented request surface and
        # the run left no fault behind.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertEqual(ops.count('inject_fault'), 2)
        self.assertGreaterEqual(ops.count('clear_fault'), 2)
        self.assertIn('list_points', ops)
        self.assertEqual(self.plant.faults, {})

    def test_quality_fault_kept_good_fails(self):
        # The named bad-data clause: the injected quality never reaches
        # the served sample — the point keeps reading Good.
        self.feed.ignore_quality_fault = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never surfaced', record.get('detail', ''))
        report.validate_scenario(record)

    def test_error_fault_hidden_from_io_health_fails(self):
        self.feed.hide_io_fault = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never surfaced on io_health',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_role_change_under_field_fault_fails(self):
        # A field fault that reads as peer loss — the scenario's
        # role-stability check must catch it.
        self.feed.demote_on_fault = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved the active role', record.get('detail', ''))
        report.validate_scenario(record)

    def test_clear_without_recovery_fails(self):
        self.feed.stuck_recovery = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never restored', record.get('detail', ''))
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


class CarryoverPeer:
    """One endpoint of the carryover pair: role, tracking state, the
    force the adopted state holds, and its own receipt/journal log."""

    def __init__(self, name):
        self.name = name
        self.tick = 0
        self.role = 'standby'      # active | standby | demoting | promoting
        self.tracking = False
        self.force = None          # the forced value while a force stands
        self.last_value = False    # the held/stamped sample value
        self.released_once = False
        self.receipts = []
        self.journal = []
        self.next_seq = 1


class CarryoverPair:
    """A stubbed redundant pair for the force-carryover scenario: two
    monitor endpoints over one checkpoint line. The active's force
    applies at a scan boundary and lands on the line, the tracking
    peer's per-pull scans adopt it — badge and substituted stamp
    included — and demote/promote move the writer role, the demoted
    peer following its successor the way the announced-peer contract
    describes. Every call on the measurement channel is one completed
    scan. Fault flags stage each named failure the issue calls out."""

    def __init__(self):
        self.a = CarryoverPeer('a')
        self.b = CarryoverPeer('b')
        self.a.role = 'active'
        self.b.role = 'standby'
        self.b.tracking = True
        self.line_force = None   # the checkpoint-carried force set
        self.held = False        # p101-oos's held operator value
        # Fault injection for the named-failure cases.
        self.badge_omitted_on = set()       # snapshot.forces stays empty
        self.force_unseen_on = set()        # the sample never substitutes
        self.promoted_drops_on = set()      # the promoted peer loses the force
        self.release_never_applies = False  # the release stays accepted
        self.resubstitutes_on = set()       # the released point re-substitutes
        self.never_activates_on = set()     # the restore leg never re-activates

    def _advance(self, peer):
        """One completed scan on `peer`: pending role transitions
        settle, the tracking peer pulls the line's force set, and each
        accepted command whose apply_tick has arrived applies and
        journals its settlement."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.tracking = True    # the demoted peer follows its successor
        elif peer.role == 'promoting':
            peer.role = 'standby' \
                if peer.name in self.never_activates_on else 'active'
            if peer.name in self.promoted_drops_on:
                peer.force = None
                peer.last_value = self.held
        if peer.role != 'active' and peer.tracking:
            peer.force = self.line_force
            if peer.force is not None:
                peer.last_value = peer.force
        for receipt in peer.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted is None or peer.tick < accepted['apply_tick']:
                continue
            command = receipt['command']
            if 'force_point' in command:
                value = command['force_point']['value']['bool']
                self.line_force = value
                peer.force = value
                peer.last_value = value
            elif 'unforce_point' in command:
                self.line_force = None
                peer.force = None
                peer.released_once = True
            elif 'write_value' in command:
                self.held = command['write_value']['value']['bool']
                peer.last_value = self.held
            if 'unforce_point' in command and self.release_never_applies:
                continue
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            peer.journal.append(
                {'seq': peer.next_seq, 'tick': peer.tick,
                 'event': {'command_settled':
                           {'receipt': dict(receipt)}}})
            peer.next_seq += 1

    def _oos_sample(self, peer):
        """Point 302's scan read: the forced value at Substituted while
        the adopted force stands, else the last stamp re-stamped Good
        — the held-value rule; the resubstitution fault holds the
        stamp at Substituted past release."""
        if peer.force is not None \
                and peer.name not in self.force_unseen_on:
            return {'value': {'bool': peer.force},
                    'quality': {'uncertain': 'substituted'},
                    'tick': peer.tick}
        quality = {'uncertain': 'substituted'} \
            if peer.name in self.resubstitutes_on \
            and peer.released_once else 'good'
        return {'value': {'bool': peer.last_value}, 'quality': quality,
                'tick': peer.tick}

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self.a if host == 'ctrl-a' else self.b
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance(peer)
        if (method, route) == ('GET', '/role'):
            report = {'role': peer.role, 'tick': peer.tick}
            if peer.role == 'standby':
                report['sync'] = {'tracking': {'aligned': peer.tick}} \
                    if peer.tracking else {'unsynchronized': {}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {
                'points': [
                    {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                     'direction': 'in', 'value_type': 'bool',
                     'writable': True}],
                'components': []}
        if (method, route) == ('GET', '/snapshot'):
            forces = [] if peer.force is None \
                or peer.name in self.badge_omitted_on \
                else [{'point': 302, 'value': {'bool': peer.force}}]
            return 200, {
                'tick': peer.tick, 'forces': forces,
                'points': [{'point': 302, 'direction': 'in',
                            'sample': self._oos_sample(peer)}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [entry for entry in peer.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}},
                       'actor': body.get('actor')}
            peer.receipts.append(receipt)
            return 200, receipt
        if (method, route) == ('POST', '/demote'):
            if peer.role != 'active':
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            return 200, {'role': 'demoting'}
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if not peer.tracking:
                self._raise(409, {'not_converged':
                                  {'sync': {'unsynchronized': {}}}})
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        raise AssertionError('unexpected request %s %s' % (method, url))


class ForceCarryoverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.pair = CarryoverPair()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.pair.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'FORCE_DEADLINE', 0.5):
            return scenarios.scenario_force_carryover(ctx)

    def test_registered_and_replayable(self):
        self.assertIn(scenarios.scenario_force_carryover,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('force-carryover'),
                      scenarios.scenario_force_carryover)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        self.assertTrue(
            any('tracking standby' in note
                for note in record['observations']))
        self.assertTrue(
            any('restored' in note for note in record['observations']))

    def test_two_runs_produce_identical_records(self):
        first = self.run_scenario()
        pair, self.pair = self.pair, CarryoverPair()
        try:
            second = self.run_scenario()
        finally:
            self.pair = pair
        self.assertEqual(first, second)

    def test_mirrored_layout_passes(self):
        # The post-failover layout: ctrl-b owns the field, ctrl-a
        # follows through the announced-peer pull — the case runs the
        # same legs mirrored and restores ctrl-b active.
        self.pair.a.role = 'standby'
        self.pair.a.tracking = True
        self.pair.b.role = 'active'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.pair.b.role, 'active')
        report.validate_scenario(record)

    def test_standby_never_reporting_the_force_fails(self):
        self.pair.badge_omitted_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking standby never reported the force',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_sample_never_substituting_fails(self):
        self.pair.force_unseen_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking standby never reported the force',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promoted_peer_dropping_the_force_fails(self):
        self.pair.promoted_drops_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the promoted peer lost',
                      record.get('detail', ''))
        self.assertIn('did not ride the checkpoint',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_release_never_settling_applied_fails(self):
        self.pair.release_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not settle applied',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_released_point_resubstituting_fails(self):
        self.pair.resubstitutes_on.add('b')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not recover', record.get('detail', ''))
        self.assertIn('re-substituted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unrestored_pair_fails(self):
        self.pair.never_activates_on.add('a')
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not restored', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_never_converging_is_inconclusive(self):
        self.pair.b.tracking = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking convergence',
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


class FreshnessFeed:
    """A stubbed pair for the stale-freshness scenario. ctrl-a is the
    field writer: while it is up the shared plant steps and the
    dynamics-driven stamp advances with it. ctrl-b is the tracking
    standby — its own scan tick advances per snapshot read, its sync
    reports `degraded` while the writer's checkpoint pulls miss, and the
    budgeted point's served quality follows the declared five-tick lag
    rule over the frozen stamp while the undeclared comparison keeps
    Good. The writer's restart realigns the standby's tick — the resumed
    checkpoint stream's rewind — and /history keeps the recorded
    interval. Fault flags stage each named outcome the issue calls
    out."""

    BUDGET = 5
    B_POINT = 13   # net-flow — the model's declared stale_after_ticks
    C_POINT = 10   # level-primary — the undeclared comparison
    PROMOTE_MISSES = 12  # the armed failover budget, past first stale

    def __init__(self):
        self.plant = 100     # the dynamics-driven driver stamp
        self.b_tick = 100    # the standby's own scan tick
        self.writer_up = True
        self.misses = 0
        self.promoted = False
        self.stale_seen = False
        self.relapsed = False
        self.hist = {self.B_POINT: [], self.C_POINT: []}
        self.seq = 1
        self.stops = []
        self.starts = []
        # Fault flags for the named outcomes.
        self.never_stale = False   # the budgeted point never presents
        self.leak = False          # the undeclared point presents stale
        self.relapse = False       # stale flips back to good mid-freeze
        self.no_recover = False    # the restart never realigns the peer
        self.freeze_takes = True   # False: the stop never freezes stamps
        self.promote = False       # the armed failover budget fires
        self.start_fails = False   # the restart action never completes

    # The runner-owned lifecycle actions — replace ctx's
    # stop_controller/start_controller.
    def stop(self, name):
        self.stops.append(name)
        self.writer_up = False

    def start(self, name):
        self.starts.append(name)
        if self.start_fails:
            raise RuntimeError('docker start failed: no such container')
        self.writer_up = True
        if not self.promoted and not self.no_recover:
            # The resumed checkpoint stream rewinds the tracking peer's
            # tick domain to the plant's — the documented realign.
            self.b_tick = self.plant

    def _frozen(self):
        # Stamps freeze while no peer steps the plant. A promoted
        # standby steps it itself; a stop that never took leaves the
        # writer effectively running.
        return not self.writer_up and not self.promoted \
            and self.freeze_takes

    def _b_scan(self):
        """One standby scan: its own tick advances, the plant's stamp
        advances only while a writer steps it, and a downed writer's
        pulls miss — the armed budget promoting at the configured
        count."""
        self.b_tick += 1
        if not self._frozen():
            self.plant += 1
        if self.writer_up or not self.freeze_takes:
            self.misses = 0
        else:
            self.misses += 1
            if self.promote and self.misses >= self.PROMOTE_MISSES:
                self.promoted = True

    def _b_quality(self):
        lag = max(0, self.b_tick - self.plant)
        if lag > self.BUDGET and not self.never_stale:
            if self.relapse and self.stale_seen and not self.relapsed:
                self.relapsed = True
                return 'good'
            self.stale_seen = True
            return {'uncertain': 'stale'}
        return 'good'

    def _c_quality(self):
        lag = max(0, self.b_tick - self.plant)
        if self.leak and lag > self.BUDGET:
            return {'uncertain': 'stale'}
        return 'good'

    def _record(self, point, quality):
        self.hist[point].append({'seq': self.seq, 'sample': {
            'value': {'float': 1.0}, 'quality': quality,
            'tick': self.plant}})
        self.seq += 1

    def _standby(self, method, route, query):
        if (method, route) == ('GET', '/role'):
            if self.promoted:
                return 200, {'role': 'active', 'tick': self.b_tick}
            sync = {'degraded': {'misses': self.misses}} if self.misses \
                else {'tracking': {'aligned': self.plant}}
            return 200, {'role': 'standby', 'tick': self.b_tick,
                         'sync': sync}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': self.B_POINT, 'signal': None,
                 'name': 'net-flow', 'direction': 'in',
                 'value_type': 'float', 'writable': False},
                {'point': self.C_POINT, 'signal': None,
                 'name': 'level-primary', 'direction': 'in',
                 'value_type': 'float', 'writable': False}]}
        if (method, route) == ('GET', '/snapshot'):
            self._b_scan()
            qb, qc = self._b_quality(), self._c_quality()
            self._record(self.B_POINT, qb)
            self._record(self.C_POINT, qc)
            return 200, {'tick': self.b_tick, 'points': [
                {'point': self.B_POINT, 'sample': {
                    'value': {'float': 1.0}, 'quality': qb,
                    'tick': self.plant}},
                {'point': self.C_POINT, 'sample': {
                    'value': {'float': 1.0}, 'quality': qc,
                    'tick': self.plant}}]}
        if (method, route) == ('GET', '/history'):
            params = [part.split('=', 1) for part in query.split('&')]
            wanted = [int(v) for k, v in params if k == 'point']
            since = next((int(v) for k, v in params if k == 'since'), 0)
            return 200, [{'point': point,
                          'samples': [s for s in self.hist[point]
                                      if s['seq'] > since]}
                         for point in wanted]
        raise AssertionError('unexpected request %s ctrl-b%s'
                             % (method, route))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if host == 'ctrl-b:2':
            return self._standby(method, route, query)
        if not self.writer_up:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.plant}
        raise AssertionError('unexpected request %s %s' % (method, url))


class StaleFreshnessTests(unittest.TestCase):
    """scenario_stale_freshness against the stubbed pair: stopping the
    writer freezes the plant's stamps, the tracking standby's scans
    outrun them, and the declared budget presents stale per-point while
    the undeclared comparison keeps Good; the writer's restart realigns
    the peer inside the failover bound and /history preserves the
    interval."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = FreshnessFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, ctx=None, feed=None, evidence=None):
        feed = feed if feed is not None else self.feed
        evidence = evidence if evidence is not None else self.evidence
        base = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'evidence_dir': str(evidence),
                'stop_controller': feed.stop,
                'start_controller': feed.start,
                'failover_misses': 120}
        if ctx is not None:
            base.update(ctx)
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'STALE_FRESHNESS_POLL', 0.001), \
                patch.object(scenarios, 'STALE_WALL_DEADLINE', 5.0), \
                patch.object(scenarios, 'STALE_RECOVER_DEADLINE', 1.0), \
                patch.object(scenarios, 'STALE_RETURN_DEADLINE', 1.0):
            return scenarios.scenario_stale_freshness(base)

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_stale_freshness,
                      scenarios.SCENARIOS)

    def test_freeze_stale_recovery_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.stops, ['active'])
        self.assertEqual(self.feed.starts, ['active'])
        self.assertTrue(self.feed.stale_seen)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        interval = json.loads(
            (self.evidence / 'stale-freshness-history.json').read_text())
        self.assertIsNotNone(interval['budgeted']['interval'])
        self.assertFalse(interval['budgeted']['interval']['good_inside'])

    def test_stale_never_presenting_fails(self):
        self.feed.never_stale = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never presented Uncertain(Stale)',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_comparison_presenting_stale_fails(self):
        self.feed.leak = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('leaked past its declaration',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_stale_reverting_to_healthy_fails(self):
        self.feed.relapse = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('healthy last-known', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_recovery_after_restart_fails(self):
        self.feed.no_recover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not return Good', record.get('detail', ''))
        report.validate_scenario(record)

    def test_promoted_peer_never_recovering_fails(self):
        # The armed failover bound firing mid-freeze: the promoted peer
        # reclaims the writer and resumes stepping, but its scan ticks
        # lead the frozen stamps by the outage — the lag never closes.
        self.feed.promote = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('self-promotion', record.get('detail', ''))
        self.assertTrue(self.feed.promoted)
        report.validate_scenario(record)

    def test_unfrozen_induction_is_inconclusive(self):
        # The writer-stop never took: the plant's stamps keep advancing
        # and the peer keeps tracking, so a missing stale presentation
        # cannot be attributed to the induction.
        self.feed.freeze_takes = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never took effect', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_restart_action_is_inconclusive(self):
        self.feed.start_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('restart never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_lifecycle_actions_are_inconclusive(self):
        record = self.run_scenario(ctx={'stop_controller': None,
                                        'start_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no documented', record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs of the scenario
        # against the same rig state record the same report and the
        # same evidence files.
        runs = []
        for index in range(2):
            evidence = Path(self.tmp.name) / ('evidence-' + str(index))
            evidence.mkdir()
            record = self.run_scenario(feed=FreshnessFeed(),
                                       evidence=evidence)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


class RevisionFeed:
    """A stubbed rig for the model-revision scenario. ctrl-b owns the
    field — the post-failover layout the suite reaches this case in —
    ctrl-a is its demoted partner, and ctrl-c is the third controller
    the runner action launches on the revised document. Every monitor
    read on a peer is one completed scan: an active peer's scan lands
    its staged field write on the faked sim-net plant. Each peer's
    --journal-file is a real append-only record the feed writes itself.
    Every transition is call-count keyed — never wall-clock — so two
    scenario runs emit identical evidence. Fault flags stage each named
    failure the issue calls out."""

    V1, V2 = 111, 222  # the mounted and revised model fingerprints
    POINT = 302        # the writable bool the carryover must name
    WATCH = 100        # the field `out` point the roll watches

    def __init__(self, journals, document):
        self.journals = {name: Path(p) for name, p in journals.items()}
        self.document = document
        self.ticks = {'a': 0, 'b': 40, 'c': 0}
        self.roles = {'a': 'standby', 'b': 'active', 'c': 'standby'}
        self.launched = False
        self.c_role_polls = 0
        self.converge_after = 3
        self.reinit_journaled = False
        self.point = False
        self.receipts = {'a': [], 'b': [], 'c': []}
        self.staged = {'a': True, 'b': True, 'c': True}
        self.field = {'value': {'bool': True}, 'quality': 'good',
                      'tick': 7}
        self.health = {'b': {'failed_writes': 0,
                             'consecutive_failures': 0,
                             'last_error': None}}
        self.seqs = {'a': 1, 'b': 1, 'c': 1}
        self.calls = []
        self.gap_open = False
        self.gap_reads = 0
        # Fault injection for the named-failure cases.
        self.action_fails = False      # the runner action raises
        self.never_converge = False    # c stays unsynchronized
        self.bad_convergence = None    # 'degraded'|'diverged'|'tracking'
        self.lose_receipts = False     # the adopted log drops the tail
        self.empty_carry = False       # the report names no carried point
        self.regress_field = False     # the field moves in the gap
        self.field_lag = False         # post-roll writes never land
        self.demoted_writes = False    # b keeps attempting writes
        self.wrong_fp = False          # c's checkpoint keeps v1's fp
        self.no_reinit_entry = False   # c's journal lacks the crossing
        self.demoted_restart = False   # b's journal gains a lifetime
        for name in ('a', 'b'):
            self.journals[name].parent.mkdir(parents=True,
                                             exist_ok=True)
            self.journals[name].write_text(
                json.dumps({'run_boundary': {'run': 1, 'tick': 0}})
                + '\n')

    def _journal(self, peer, record):
        with self.journals[peer].open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, peer, event):
        self._journal(peer, {'entry': {'seq': self.seqs[peer],
                                       'tick': self.ticks[peer],
                                       'event': event}})
        self.seqs[peer] += 1

    def _report(self):
        carried = [] if self.empty_carry else [
            {'point': self.POINT, 'value': {'bool': self.point}}]
        return {'from': self.V1, 'to': self.V2,
                'resumed_at': self.ticks['b'],
                'carried': carried,
                'carried_outputs': [{'point': self.WATCH,
                                     'value': {'bool': True}}],
                'carried_forces': [], 'dropped': [],
                'reinitialized': ['motor:20', 'digital-input:12'],
                'initialized': [900]}

    # The runner-owned action — replaces ctx['start_revised'].
    def start(self, name):
        self.calls.append(('start_revised', name))
        if self.action_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        self.journals['c'].parent.mkdir(parents=True, exist_ok=True)
        self._journal('c', {'run_boundary': {'run': 1, 'tick': 0}})
        return {'container': 'dcs-hw-qa-1-c',
                'document': str(self.document),
                'added_points': [900], 'added_signals': [10900]}

    def _role(self, peer):
        if peer != 'c':
            return {'role': self.roles[peer], 'tick': self.ticks[peer],
                    'sync': 'unsynchronized'
                    if self.roles[peer] == 'standby' else None}
        self.c_role_polls += 1
        if self.never_converge:
            sync = 'unsynchronized'
        elif self.bad_convergence:
            sync = {self.bad_convergence: {'detail': 'staged'}}
        elif self.c_role_polls >= self.converge_after:
            sync = {'reinitialized': {'report': self._report()}}
            if not self.reinit_journaled:
                self.reinit_journaled = True
                # The crossing journals its carryover report and the
                # adopted receipt log arrives with the checkpoint.
                if not self.no_reinit_entry:
                    self._entry('c', {'reinitialized':
                                      {'report': self._report()}})
                receipts = list(self.receipts['b'])
                if self.lose_receipts:
                    receipts = receipts[:-1]
                self.receipts['c'] = receipts
        else:
            sync = 'unsynchronized'
        return {'role': self.roles['c'], 'tick': self.ticks['c'],
                'sync': sync}

    def _snapshot(self, peer):
        self.ticks[peer] += 1
        if self.roles[peer] == 'active' and not (
                peer == 'c' and self.field_lag):
            self.field = {'value': {'bool': self.staged[peer]},
                          'quality': 'good',
                          'tick': self.ticks[peer]}
        if peer == 'b' and self.demoted_writes \
                and self.roles['b'] == 'standby':
            # A demoted peer still attempting writes meets the plant's
            # fence, which counts each rejection in its io_health.
            self.health['b']['failed_writes'] += 1
            self.health['b']['consecutive_failures'] += 1
            self.health['b']['last_error'] = {
                'tick': self.ticks['b'], 'point': self.WATCH,
                'direction': 'out', 'error': {'fenced': None}}
        health = dict(self.health.get(
            peer, {'failed_writes': 0, 'consecutive_failures': 0,
                   'last_error': None}))
        return {'tick': self.ticks[peer],
                'points': [
                    {'point': self.WATCH,
                     'sample': {'value': {'bool': self.staged[peer]},
                                'quality': 'good'}},
                    {'point': self.POINT,
                     'sample': {'value': {'bool': self.point},
                                'quality': 'good'}}],
                'io_health': health}

    def _command(self, peer, body):
        write = body['command']['write_value']
        if write['point'] == self.POINT:
            self.point = write['value']['bool']
        receipt = {'command': body['command'],
                   'outcome': {'applied': {'tick': self.ticks[peer]}},
                   'actor': body.get('actor')}
        self.receipts[peer].append(receipt)
        self._entry(peer, {'command_settled': {'receipt': receipt}})
        return 200, receipt

    def _demote(self, peer):
        self.calls.append(('demote', peer))
        self.gap_open = True
        self.roles[peer] = 'standby'
        self._entry(peer, {'role_changed': {'from': 'active',
                                            'to': 'demoting'}})
        self._entry(peer, {'role_changed': {'from': 'demoting',
                                            'to': 'standby'}})
        if self.demoted_restart:
            self._journal(peer, {'run_boundary': {'run': 2,
                                                  'tick': 0}})
        return 200, {'role': 'standby', 'tick': self.ticks[peer]}

    def _promote(self, peer):
        self.calls.append(('promote', peer))
        self.gap_open = False
        self.roles[peer] = 'active'
        self._entry(peer, {'role_changed': {'from': 'standby',
                                            'to': 'promoting'}})
        self._entry(peer, {'role_changed': {'from': 'promoting',
                                            'to': 'active'}})
        return 200, {'role': 'active', 'tick': self.ticks[peer]}

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b',
                'ctrl-c:3': 'c'}[host]
        if peer == 'c' and not self.launched:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 10, 'signal': 10010, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}]}
        if (method, route) == ('GET', '/checkpoint'):
            fp = self.V1 if peer != 'c' or self.wrong_fp else self.V2
            return 200, {'model_fingerprint': fp,
                         'tick': self.ticks[peer]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot(peer)
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts[peer])
        if (method, route) == ('POST', '/command'):
            return self._command(peer, body)
        if (method, route) == ('POST', '/demote'):
            return self._demote(peer)
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The plant's sim-net service — replaces scenarios._field_request.
    def field_request(self, ctx, request):
        if request['op'] == 'list_points':
            return {'result': 'points', 'points': [
                {'point': self.WATCH, 'direction': 'out',
                 'sample': self.field, 'fault': None},
                {'point': 10, 'direction': 'in',
                 'sample': {'value': {'float': 1.5},
                            'quality': 'good', 'tick': 7},
                 'fault': None}]}
        if request['op'] == 'read':
            if self.gap_open:
                self.gap_reads += 1
            if self.regress_field and self.gap_open \
                    and self.gap_reads > 1:
                # The named failure: the field moves inside the
                # writer-less window — after the scenario's held read.
                self.regress_field = False
                self.field = {'value': {'bool': not self.field
                                        ['value']['bool']},
                              'quality': 'good', 'tick': 9}
            return {'result': 'sample', 'sample': self.field}
        raise AssertionError('unexpected plant request %s' % request)


class ModelRevisionTests(unittest.TestCase):
    """scenario_model_revision against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the revised peer that never converges, the wrong terminal sync,
    receipts or journal seqs lost across the boundary, a field
    regression, a demoted peer still serving writes, and a run that
    ends off the revised fingerprint."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        root = Path(self.tmp.name) / 'controllers'
        self.document = Path(self.tmp.name) / 'model-revised.json'
        self.document.write_text(json.dumps({'revised': True}))
        self.journals = {name: root / name / 'journal.jsonl'
                         for name in ('a', 'b', 'c')}
        self.feed = RevisionFeed(self.journals, self.document)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': 'plant:9',
                'evidence_dir': str(self.evidence),
                'start_revised': self.feed.start,
                'journal_files': {
                    'active': str(self.journals['a']),
                    'standby': str(self.journals['b']),
                    'revised': str(self.journals['c'])}}

    def run_scenario(self):
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_field_request',
                             self.feed.field_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'REVISION_POLL', 0.001), \
                patch.object(scenarios, 'REVISION_CONVERGE_DEADLINE',
                             2.0), \
                patch.object(scenarios, 'REVISION_SETTLE_DEADLINE',
                             0.5), \
                patch.object(scenarios, 'REVISION_FIELD_ROUNDS', 4), \
                patch.object(scenarios, 'RESTART_JOURNAL_DEADLINE',
                             0.5):
            return scenarios.scenario_model_revision(self._ctx())

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_model_revision,
                      scenarios.SCENARIOS)

    def test_clean_roll_passes_validates_and_orders_the_roll(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented order: the third controller launches first,
        # the old active demotes, and only then the revised peer
        # promotes.
        kinds = [kind for kind, _ in self.feed.calls]
        self.assertEqual(kinds,
                         ['start_revised', 'demote', 'promote'])
        self.assertEqual(dict(self.feed.calls)['demote'], 'b')
        self.assertEqual(dict(self.feed.calls)['promote'], 'c')
        self.assertEqual(self.feed.roles['b'], 'standby')
        self.assertEqual(self.feed.roles['c'], 'active')

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        journals2 = {name: Path(second_tmp.name) / 'controllers'
                     / name / 'journal.jsonl'
                     for name in ('a', 'b', 'c')}
        feed2 = RevisionFeed(journals2, self.document)
        ctx2 = self._ctx()
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['start_revised'] = feed2.start
        ctx2['journal_files'] = {
            'active': str(journals2['a']),
            'standby': str(journals2['b']),
            'revised': str(journals2['c'])}
        with patch.object(scenarios, 'http_json', feed2.http_json), \
                patch.object(scenarios, '_field_request',
                             feed2.field_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'REVISION_POLL', 0.001), \
                patch.object(scenarios, 'REVISION_CONVERGE_DEADLINE',
                             2.0), \
                patch.object(scenarios, 'REVISION_SETTLE_DEADLINE',
                             0.5), \
                patch.object(scenarios, 'REVISION_FIELD_ROUNDS', 4), \
                patch.object(scenarios, 'RESTART_JOURNAL_DEADLINE',
                             0.5):
            record2 = scenarios.scenario_model_revision(ctx2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_never_converging_revised_peer_is_inconclusive(self):
        self.feed.never_converge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never converged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_wrong_convergence_state_fails(self):
        self.feed.bad_convergence = 'tracking'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reinitialized', record.get('detail', ''))
        report.validate_scenario(record)

    def test_persistent_degraded_pulls_fail(self):
        # A degraded pull that never clears is the revised peer's
        # rejected crossing — terminal at the convergence deadline.
        self.feed.bad_convergence = 'degraded'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('degraded', record.get('detail', ''))
        report.validate_scenario(record)

    def test_carryover_missing_the_operator_write_fails(self):
        self.feed.empty_carry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('carryover report', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipt_loss_across_the_roll_fails(self):
        self.feed.lose_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_in_the_gap_fails(self):
        self.feed.regress_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('writer-less window', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_after_promotion_fails(self):
        self.feed.staged['c'] = False
        self.feed.field_lag = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('regressed across the roll',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_demoted_peer_serving_writes_fails(self):
        self.feed.demoted_writes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('serving writes', record.get('detail', ''))
        report.validate_scenario(record)

    def test_run_ending_off_the_revised_fingerprint_fails(self):
        self.feed.wrong_fp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('fingerprint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_reinitialized_journal_entry_fails(self):
        self.feed.no_reinit_entry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_demoted_journal_lifetime_restart_fails(self):
        self.feed.demoted_restart = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('lifetimes', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_action_is_inconclusive(self):
        self.feed.action_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('model-revision action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)


class IncompatibleFeed:
    """A stubbed rig for the incompatible-revision scenario. ctrl-b
    owns the field — the post-failover layout the suite reaches this
    case in — and ctrl-c is the third controller the runner action
    launches twice: first on the incompatible document (its carryover
    crossing refuses, so every role poll reports the named degraded
    detail), then on the control document (the same revision minus the
    retype), which converges reinitialized carrying point 302 as its
    declared bool kind. Every transition is call-count keyed — never
    wall-clock — so two scenario runs emit identical evidence. Fault
    flags stage each named failure the issue calls out."""

    POINT = 302     # the carried internal point the spec retypes
    WATCH = 100     # the field `out` point the window watches
    DETAIL = ('checkpoint internal point 302 carries Bool but the '
             'revision declares Int — a retype must rename the point')
    FINGERPRINT_DETAIL = ('checkpoint model fingerprint 7f2a does not '
                          'match this run\'s 9c1e')

    def __init__(self, journals, incompatible_doc, control_doc):
        self.journals = {name: Path(p) for name, p in journals.items()}
        self.incompatible_doc = incompatible_doc
        self.control_doc = control_doc
        self.ticks = {'a': 0, 'b': 40, 'c': 0}
        self.roles = {'a': 'standby', 'b': 'active', 'c': 'standby'}
        self.launched = False
        self.incompatible = None
        self.c_role_polls = 0
        self.refuse_after = 3    # polls before the crossing reports
        self.receipts = {'a': [], 'b': [], 'c': []}
        self.staged_b = True
        self.field = {'value': {'bool': True}, 'quality': 'good',
                      'tick': 7}
        self.seqs = {'a': 1, 'b': 1, 'c': 1}
        self.calls = []
        self.reads = 0
        # Fault injection for the named-failure cases.
        self.action_fails = False       # the runner action raises
        self.never_cross = False        # c stays unsynchronized
        self.fingerprint_degrade = False  # the wrong degrade detail
        self.silent_cross = False       # the incompatible doc converges
        self.promote_succeeds = False   # /promote answers 200
        self.refusal_plain = False      # 409 without the named detail
        self.receipts_grow = False      # b's receipt log moves mid-window
        self.journal_grows = False      # b's journal file moves mid-window
        self.field_regress = False      # the field stops following b
        self.control_degrades = False   # the control relaunch refuses
        for name in ('a', 'b'):
            self.journals[name].parent.mkdir(parents=True,
                                             exist_ok=True)
            self.journals[name].write_text(
                json.dumps({'run_boundary': {'run': 1, 'tick': 0}})
                + '\n')

    def _journal(self, peer, record):
        with self.journals[peer].open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry(self, peer, event):
        self._journal(peer, {'entry': {'seq': self.seqs[peer],
                                       'tick': self.ticks[peer],
                                       'event': event}})
        self.seqs[peer] += 1

    def _report(self):
        return {'from': 111, 'to': 222, 'resumed_at': self.ticks['b'],
                'carried': [{'point': self.POINT,
                             'value': {'bool': True}}],
                'carried_outputs': [{'point': self.WATCH,
                                     'value': {'bool': True}}],
                'carried_forces': [], 'dropped': [],
                'reinitialized': ['motor:20'], 'initialized': [900]}

    # The runner-owned action — replaces ctx['start_revised'].
    def start(self, name, incompatible=False):
        self.calls.append(('start_revised', name, incompatible))
        if self.action_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        self.incompatible = incompatible
        self.c_role_polls = 0
        self.journals['c'].parent.mkdir(parents=True, exist_ok=True)
        self._journal('c', {'run_boundary': {'run': 1, 'tick': 0}})
        info = {'container': 'dcs-hw-qa-1-c',
                'document': str(self.incompatible_doc
                                if incompatible else self.control_doc),
                'added_points': [900], 'added_signals': [10900]}
        if incompatible:
            info['retyped_point'] = self.POINT
            info['rewired_connections'] = 3
        return info

    def _role(self, peer):
        if peer != 'c':
            return {'role': self.roles[peer], 'tick': self.ticks[peer],
                    'sync': 'unsynchronized'
                    if self.roles[peer] == 'standby' else None}
        self.c_role_polls += 1
        if self.c_role_polls < self.refuse_after or self.never_cross:
            sync = 'unsynchronized'
        elif self.incompatible:
            if self.silent_cross:
                sync = {'reinitialized': {'report': self._report()}}
            elif self.fingerprint_degrade:
                sync = {'degraded':
                        {'detail': self.FINGERPRINT_DETAIL}}
            else:
                sync = {'degraded': {'detail': self.DETAIL}}
        elif self.control_degrades:
            sync = {'degraded': {'detail': self.DETAIL}}
        else:
            sync = {'reinitialized': {'report': self._report()}}
        return {'role': self.roles['c'], 'tick': self.ticks['c'],
                'sync': sync}

    def _snapshot(self, peer):
        self.ticks[peer] += 1
        return {'tick': self.ticks[peer],
                'points': [
                    {'point': self.WATCH,
                     'sample': {'value': {'bool': self.staged_b},
                                'quality': 'good'}}],
                'io_health': {'failed_writes': 0,
                              'consecutive_failures': 0,
                              'last_error': None}}

    def _promote(self, peer):
        self.calls.append(('promote', peer))
        if self.promote_succeeds:
            self.roles[peer] = 'active'
            return 200, {'role': 'active', 'tick': self.ticks[peer]}
        detail = 'checkpoint refused' if self.refusal_plain \
            else self.DETAIL
        body = {'not_converged': {'sync': {'degraded':
                                           {'detail': detail}}}}
        raise urllib.error.HTTPError(
            'http://ctrl-c:3/promote', 409, 'Conflict', {},
            io.BytesIO(json.dumps(body).encode()))

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b',
                'ctrl-c:3': 'c'}[host]
        if peer == 'c' and not self.launched:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot(peer)
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts[peer])
        if (method, route) == ('POST', '/promote'):
            return self._promote(peer)
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The plant's sim-net service — replaces scenarios._field_request.
    def field_request(self, ctx, request):
        if request['op'] == 'list_points':
            return {'result': 'points', 'points': [
                {'point': self.WATCH, 'direction': 'out',
                 'sample': self.field, 'fault': None}]}
        if request['op'] == 'read':
            self.reads += 1
            if self.reads == 2:
                # Mid-window mutations stage the audit-trail faults:
                # the active's receipts or journal moving under the
                # refusal, or the field leaving the active's image.
                if self.receipts_grow:
                    self.receipts['b'].append(
                        {'command': {'write_value': {'point': 302}},
                         'actor': 'intruder'})
                if self.journal_grows:
                    self._entry('b', {'command_settled':
                                      {'receipt': {'actor':
                                                   'intruder'}}})
                if self.field_regress:
                    self.field = {'value': {'bool': not self.staged_b},
                                  'quality': 'good', 'tick': 9}
            return {'result': 'sample', 'sample': self.field}
        raise AssertionError('unexpected plant request %s' % request)


class IncompatibleRevisionTests(unittest.TestCase):
    """scenario_incompatible_revision against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named acceptance
    failure — the incompatible document silently crossing, the degrade
    landing on the foreign-fingerprint detail instead of the carryover
    refusal, a promotion that answers anything but the named 409
    payload, an active whose receipts, journal, or field move during
    the observation window, and a control half that never converges."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        root = Path(self.tmp.name) / 'controllers'
        self.incompatible_doc = Path(self.tmp.name) \
            / 'model-revised-incompatible.json'
        self.incompatible_doc.write_text(json.dumps(
            {'revised': True, 'retyped': 302}))
        self.control_doc = Path(self.tmp.name) / 'model-revised.json'
        self.control_doc.write_text(json.dumps({'revised': True}))
        self.journals = {name: root / name / 'journal.jsonl'
                         for name in ('a', 'b', 'c')}
        self.feed = IncompatibleFeed(self.journals,
                                     self.incompatible_doc,
                                     self.control_doc)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, feed=None):
        feed = feed or self.feed
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': 'plant:9',
                'evidence_dir': str(self.evidence),
                'start_revised': feed.start,
                'journal_files': {
                    'active': str(feed.journals['a']),
                    'standby': str(feed.journals['b']),
                    'revised': str(feed.journals['c'])}}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_field_request',
                             feed.field_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'REVISION_POLL', 0.001), \
                patch.object(scenarios, 'REVISION_CONVERGE_DEADLINE',
                             2.0), \
                patch.object(scenarios, 'REVISION_FIELD_ROUNDS', 4):
            return scenarios.scenario_incompatible_revision(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_incompatible_revision,
                      scenarios.SCENARIOS)
        # Immediately ahead of the compatible case: the control's
        # promote leg belongs to scenario_model_revision in the same
        # run.
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(scenarios.scenario_incompatible_revision) + 1,
            order.index(scenarios.scenario_model_revision))

    def test_clean_refusal_passes_and_orders_the_legs(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The incompatible launch, the refused promotion, then the
        # control relaunch — and no demote/promote of the field owner.
        kinds = [kind for kind, *_ in self.feed.calls]
        self.assertEqual(kinds, ['start_revised', 'promote',
                                 'start_revised'])
        self.assertEqual(self.feed.calls[0],
                         ('start_revised', 'standby', True))
        self.assertEqual(self.feed.calls[2],
                         ('start_revised', 'standby', False))
        self.assertEqual(self.feed.roles['b'], 'active')
        self.assertEqual(self.feed.roles['c'], 'standby')

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        journals2 = {name: Path(second_tmp.name) / 'controllers'
                     / name / 'journal.jsonl'
                     for name in ('a', 'b', 'c')}
        feed2 = IncompatibleFeed(journals2, self.incompatible_doc,
                                 self.control_doc)
        ctx2 = {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': 'plant:9',
                'evidence_dir': str(evidence2),
                'start_revised': feed2.start,
                'journal_files': {
                    'active': str(journals2['a']),
                    'standby': str(journals2['b']),
                    'revised': str(journals2['c'])}}
        record2 = self.run_scenario(feed=feed2, ctx=ctx2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_silent_crossing_fails(self):
        # The contract break this case exists to catch: the
        # incompatible document converged reinitialized.
        self.feed.silent_cross = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reinitialized', record.get('detail', ''))
        report.validate_scenario(record)

    def test_fingerprint_degrade_is_the_wrong_refusal(self):
        # The foreign-fingerprint degrade #483 exercises must not
        # satisfy this case — the detail lacks the carryover naming.
        self.feed.fingerprint_degrade = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('carryover', record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_crossing_peer_is_inconclusive(self):
        self.feed.never_cross = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported a checkpoint crossing',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_succeeding_fails(self):
        self.feed.promote_succeeds = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('409', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_without_the_named_detail_fails(self):
        self.feed.refusal_plain = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('carryover', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipt_drift_fails(self):
        self.feed.receipts_grow = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_drift_fails(self):
        self.feed.journal_grows = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_fails(self):
        self.feed.field_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('field', record.get('detail', ''))
        report.validate_scenario(record)

    def test_control_degrading_fails(self):
        # The same document minus the retype must converge — a second
        # refusal names a rig defect, not the carryover violation.
        self.feed.control_degrades = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('control', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_action_is_inconclusive(self):
        self.feed.action_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never completed', record.get('detail', ''))
        report.validate_scenario(record)


class NegotiationFeed:
    """A stubbed rig for the checkpoint-negotiation scenario. ctrl-b
    owns the field — the post-failover layout the suite reaches this
    case in — ctrl-a is its unsynchronized demoted partner, and ctrl-f
    is the foreign peer the runner action launches on the derived
    document without --revised: every pull produces a checkpoint the
    peer refuses, so once its monitor answers it reports the named
    degraded negotiation failure and never converges, and POST
    /promote on it answers the 409 not_converged refusal. Each ctrl-b
    snapshot read is one completed scan landing its staged write on
    the faked sim-net field. Every transition is call-count keyed —
    never wall-clock — so two scenario runs emit identical evidence.
    Fault flags stage each named failure the issue calls out."""

    V1, V2 = 111, 222  # the pair's and the foreign model fingerprints
    WATCH = 100        # the field `out` point the window watches

    def __init__(self, document):
        self.document = document
        self.ticks = {'a': 0, 'b': 40, 'f': 0}
        self.launched = False   # f's monitor answers
        self.f_polls = 0
        self.degrade_after = 2  # f's first polls stay unsynchronized
        self.promote_seen = False
        self.staged = True      # the value ctrl-b's scans write
        self.field = {'value': {'bool': True}, 'quality': 'good',
                      'tick': 7}
        self.receipts = {'b': [
            {'command': {'write_value': {'point': 302, 'kind': 'bool',
                                         'value': {'bool': True}}},
             'outcome': {'applied': {'tick': 30}},
             'actor': 'qa-lane'}]}
        self.calls = []
        # Fault injection for the named-failure cases.
        self.action_fails = False   # the launch action raises
        self.stop_fails = False     # the teardown action raises
        self.never_refuse = False   # f stays unsynchronized
        self.converges = False      # f reports tracking — violation
        self.late_converge = False  # f leaves degraded mid-window
        self.wrong_detail = False   # degraded names another rejection
        self.wrong_fp = False       # the detail omits the pair's fp
        self.promote_ok = False     # promote answers 200
        self.promote_other = False  # 409 without not_converged
        self.refusal_bare = False   # not_converged without the sync
        self.stall_active = False   # the active's tick stops advancing
        self.field_regress = False  # the field stops following writes
        self.receipt_drift = False  # a receipt appears after promote

    def _sync(self):
        if self.wrong_detail:
            return {'degraded': {
                'detail': 'checkpoint carries output point 9 the '
                          'point map does not serve as out'}}
        found = self.V1 + 1 if self.wrong_fp else self.V1
        return {'degraded': {
            'detail': 'checkpoint model fingerprint %016x does not '
                      "match this run's %016x" % (found, self.V2)}}

    # The runner-owned actions — replace ctx['start_foreign'] and
    # ctx['stop_foreign'].
    def start(self, name):
        self.calls.append(('start_foreign', name))
        if self.action_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        return {'container': 'dcs-hw-qa-1-foreign',
                'document': str(self.document),
                'added_points': [900], 'added_signals': [10900]}

    def stop(self):
        self.calls.append(('stop_foreign',))
        if self.stop_fails:
            raise RuntimeError('docker rm failed: no such container')
        self.launched = False  # the foreign endpoint stops answering

    def _role(self, peer):
        if peer == 'a':
            return {'role': 'standby', 'tick': self.ticks['a'],
                    'sync': 'unsynchronized'}
        if peer == 'b':
            return {'role': 'active', 'tick': self.ticks['b']}
        self.f_polls += 1
        self.ticks['f'] += 1
        if self.never_refuse or self.f_polls < self.degrade_after:
            sync = 'unsynchronized'
        elif self.converges or (
                self.late_converge
                and self.f_polls >= self.degrade_after + 3):
            sync = {'tracking': {'aligned': self.ticks['b']}}
        else:
            sync = self._sync()
        return {'role': 'standby', 'tick': self.ticks['f'],
                'sync': sync}

    def _snapshot(self, peer):
        if peer == 'b':
            if not self.stall_active:
                self.ticks['b'] += 1
                if not self.field_regress:
                    self.field = {'value': {'bool': self.staged},
                                  'quality': 'good',
                                  'tick': self.ticks['b']}
        else:
            self.ticks[peer] += 1
        return {'tick': self.ticks[peer],
                'points': [{'point': self.WATCH,
                            'sample': {'value': {'bool': self.staged},
                                       'quality': 'good'}}]}

    def _promote(self, url, peer):
        self.calls.append(('promote', peer))
        self.promote_seen = True
        if self.promote_ok:
            return 200, {'role': 'active', 'tick': self.ticks[peer]}
        if self.promote_other:
            payload = {'already_active': None}
        elif self.refusal_bare:
            payload = {'not_converged': {'sync': 'unsynchronized'}}
        else:
            payload = {'not_converged': {'sync': self._sync()}}
        raise urllib.error.HTTPError(
            url, 409, 'conflict', {},
            io.BytesIO(json.dumps(payload).encode()))

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b',
                'ctrl-f:4': 'f'}.get(host)
        if peer is None or (peer == 'f' and not self.launched):
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if (method, route) == ('GET', '/checkpoint'):
            fp = self.V1 if peer != 'f' else self.V2
            return 200, {'model_fingerprint': fp,
                         'tick': self.ticks[peer]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot(peer)
        if (method, route) == ('GET', '/receipts'):
            receipts = list(self.receipts.get(peer, []))
            if peer == 'b' and self.receipt_drift \
                    and self.promote_seen:
                receipts.append(
                    {'command': {'write_value': {
                        'point': 302, 'kind': 'bool',
                        'value': {'bool': False}}},
                     'outcome': {'applied': {'tick': self.ticks['b']}},
                     'actor': 'qa-lane'})
            return 200, receipts
        if (method, route) == ('POST', '/promote'):
            return self._promote(url, peer)
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The plant's sim-net service — replaces scenarios._field_request.
    def field_request(self, ctx, request):
        if request['op'] == 'list_points':
            return {'result': 'points', 'points': [
                {'point': self.WATCH, 'direction': 'out',
                 'sample': self.field, 'fault': None}]}
        if request['op'] == 'read':
            return {'result': 'sample', 'sample': self.field}
        raise AssertionError('unexpected plant request %s' % request)


class CheckpointNegotiationTests(unittest.TestCase):
    """scenario_checkpoint_negotiation against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the foreign peer that never reports the refusal, converges anyway,
    reports a different rejection, accepts promotion, or leaves the
    active peer's ticks, field writes, or receipt log disturbed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.document = Path(self.tmp.name) / 'model-foreign.json'
        self.document.write_text(json.dumps({'revised': True}))
        self.feed = NegotiationFeed(self.document)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'plant': 'plant:9',
                'evidence_dir': str(self.evidence),
                'start_foreign': self.feed.start,
                'stop_foreign': self.feed.stop}

    def run_scenario(self, ctx=None):
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_field_request',
                             self.feed.field_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_POLL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_DEADLINE', 2.0), \
                patch.object(scenarios, 'NEGOTIATION_ROUNDS', 6):
            return scenarios.scenario_checkpoint_negotiation(
                ctx or self._ctx())

    def test_registered_ahead_of_model_revision(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_checkpoint_negotiation),
            order.index(scenarios.scenario_model_revision))

    def test_clean_refusal_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds,
                         ['start_foreign', 'promote', 'stop_foreign'])
        self.assertEqual(dict(
            call for call in self.feed.calls if len(call) == 2
        )['start_foreign'], 'standby')
        self.assertFalse(self.feed.launched)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = NegotiationFeed(self.document)
        ctx2 = self._ctx()
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['start_foreign'] = feed2.start
        ctx2['stop_foreign'] = feed2.stop
        with patch.object(scenarios, 'http_json', feed2.http_json), \
                patch.object(scenarios, '_field_request',
                             feed2.field_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_POLL', 0.001), \
                patch.object(scenarios, 'NEGOTIATION_DEADLINE', 2.0), \
                patch.object(scenarios, 'NEGOTIATION_ROUNDS', 6):
            record2 = scenarios.scenario_checkpoint_negotiation(ctx2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_never_refusing_peer_is_inconclusive(self):
        self.feed.never_refuse = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reached a negotiation verdict',
                      record.get('detail', ''))
        report.validate_scenario(record)
        # Teardown still ran — the rig stays clean either way.
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_converged_foreign_peer_fails(self):
        self.feed.converges = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('converged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_other_rejection_detail_fails(self):
        self.feed.wrong_detail = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never named the fingerprint',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_detail_omitting_the_pair_fingerprint_fails(self):
        self.feed.wrong_fp = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('does not name the pair', record.get('detail', ''))
        report.validate_scenario(record)

    def test_convergence_inside_the_window_fails(self):
        self.feed.late_converge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('left the refused state',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_accepted_fails(self):
        self.feed.promote_ok = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record.get('detail', ''))
        report.validate_scenario(record)
        # Teardown still ran even on the failed leg.
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_other_409_refusal_fails(self):
        self.feed.promote_other = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_without_the_sync_state_fails(self):
        self.feed.refusal_bare = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('does not carry the degraded',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_active_tick_stall_fails(self):
        self.feed.stall_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tick stalled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_regression_during_the_window_fails(self):
        self.feed.field_regress = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped receiving', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipt_drift_fails(self):
        self.feed.receipt_drift = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt log changed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_launch_action_is_inconclusive(self):
        self.feed.action_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_teardown_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never removed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_actions_are_inconclusive(self):
        ctx = self._ctx()
        del ctx['start_foreign']
        del ctx['stop_foreign']
        del ctx['foreign']
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)


class TuneFeed:
    """A stubbed monitor pair for the parameter-tune carryover
    scenario. ctrl-a owns the field and holds the parameter's live
    tune; ctrl-b tracks it, its served parameter report mirroring the
    last adopted checkpoint. POST /command validates the declared range
    at submission — an out-of-range tune takes the named out_of_range
    rejection receipt on the spot — while an accepted tune applies at
    the next scan boundary and journals its settled receipt. The
    demote/promote leg switches the pair: the promoted peer's report
    serves what the checkpoint carried. Fault flags stage each named
    failure the issue calls out."""

    COMPONENT = 'alarm-1'
    PARAM = 'hysteresis'
    DEFAULT = 0.1
    RANGE = {'min': {'float': 0.0}, 'max': {'float': 10.0}}

    def __init__(self):
        self.tick = 0
        self.tuned = self.DEFAULT    # ctrl-a's live parameter value
        self.carried = self.DEFAULT  # what ctrl-b's checkpoint adopted
        self.role_a = 'active'
        self.promoted = False
        self.receipts = []
        self.journal = []
        self.next_seq = 1
        # Fault injection for the named-failure cases.
        self.hold_settle = False      # the receipt never leaves accepted
        self.never_reflect = False    # the served report never shows it
        self.apply_outside = False    # the out-of-range tune applies
        self.promote_reverts = False  # the promoted peer reverts
        self.drop_journal = False     # the settlement is never journaled

    def _scan(self):
        # One completed scan per request on either endpoint: both peers
        # pace their own loop, and the active's boundary applies an
        # accepted tune into the same fields its checkpoint carries.
        self.tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted and not self.hold_settle \
                    and self.tick >= accepted['apply_tick']:
                tune = receipt['command']['set_parameter']
                self.tuned = tune['value']['float']
                self.carried = self.tuned
                receipt['outcome'] = {'applied': {'tick': self.tick}}
                if not self.drop_journal:
                    self._journal(receipt)

    def _journal(self, receipt):
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {'command_settled': {
                                 'receipt': json.loads(
                                     json.dumps(receipt))}}})
        self.next_seq += 1

    def _active(self, method, route, body):
        if (method, route) == ('GET', '/role'):
            return 200, {'role': self.role_a, 'tick': self.tick}
        if (method, route) == ('GET', '/schema'):
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': [
                             {'name': self.COMPONENT,
                              'interface': {
                                  'version': 1,
                                  'kind': 'managed-latching-alarm',
                                  'measurements': [],
                                  'configuration': [
                                      {'name': self.PARAM,
                                       'kind': 'float',
                                       'range': self.RANGE,
                                       'capability': 'tunable'}],
                                  'state': [], 'commands': [],
                                  'events': []}}]}
        if (method, route) == ('GET', '/snapshot'):
            served = self.DEFAULT if self.never_reflect else self.tuned
            return 200, {'tick': self.tick, 'parameters': [
                {'name': self.COMPONENT,
                 'values': {self.PARAM: {'float': served}}}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/journal'):
            return 200, list(self.journal)
        if (method, route) == ('POST', '/demote'):
            self.role_a = 'standby'
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/command'):
            tune = body['command']['set_parameter']
            value = tune['value']['float']
            if (value < 0.0 or value > 10.0) and not self.apply_outside:
                receipt = {'command': body['command'],
                           'outcome': {'rejected': {'reason': {
                               'out_of_range': {
                                   'component': tune['component'],
                                   'parameter': tune['name'],
                                   'value': tune['value'],
                                   'range': self.RANGE}}}},
                           'actor': body.get('actor')}
                self.receipts.append(receipt)
                if not self.drop_journal:
                    self._journal(receipt)
                return 200, receipt
            # With apply_outside the declared range is never consulted —
            # the defect — and the out-of-range tune queues and applies
            # like an admitted one.
            receipt = {'command': body['command'],
                       'outcome': {'accepted': {
                           'apply_tick': self.tick + 1}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))

    def _peer(self, method, route):
        if (method, route) == ('GET', '/role'):
            if self.promoted:
                return 200, {'role': 'active', 'tick': self.tick}
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': {'tracking': {'aligned': self.tick}}}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'parameters': [
                {'name': self.COMPONENT,
                 'values': {self.PARAM: {'float': self.carried}}}]}
        if (method, route) == ('POST', '/promote'):
            self.promoted = True
            # The promoted peer's report serves what its adopted
            # checkpoint carried — or the declared default when the
            # tune never crossed.
            self.carried = self.DEFAULT if self.promote_reverts \
                else self.tuned
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route = path.partition('?')[0]
        self._scan()
        if host == 'ctrl-b:2':
            return self._peer(method, route)
        return self._active(method, route, body)


class ParameterTuneCarryoverTests(unittest.TestCase):
    """scenario_parameter_tune_carryover against the stubbed pair: the
    tune/receipt/report legs, the named out-of-range rejection, the
    cross-promotion continuity assertion, and the inconclusive case
    when the served report never reflects the tune."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = TuneFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'TUNE_DEADLINE', 0.5):
            return scenarios.scenario_parameter_tune_carryover(ctx)

    def test_registered_ahead_of_failover(self):
        self.assertIn(scenarios.scenario_parameter_tune_carryover,
                      scenarios.SCENARIOS)
        # The tune can only cross a checkpoint from ctrl-a to the
        # tracking ctrl-b, so the case's promotion is the run's one
        # a->b switch — it must run ahead of the failover leg.
        self.assertLess(
            scenarios.SCENARIOS.index(
                scenarios.scenario_parameter_tune_carryover),
            scenarios.SCENARIOS.index(scenarios.scenario_failover))

    def test_clean_tune_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_unsettled_tune_fails(self):
        # The in-range submission's receipt never leaves accepted.
        self.feed.hold_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('settled receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreported_tune_is_inconclusive(self):
        # The receipt settles applied but the served parameter report
        # never reflects the tune — the lane cannot prove the carryover
        # legs, so the case is inconclusive rather than a false pass.
        self.feed.never_reflect = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reflected', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_journal_fails(self):
        # The tune's settlement never lands in the transition journal.
        self.feed.drop_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_out_of_range_applying_fails(self):
        # The out-of-range submission applies instead of meeting the
        # named out_of_range rejection.
        self.feed.apply_outside = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not rejected by name',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_reverted_tune_fails(self):
        # The promoted peer's report reverts to the model-declared
        # default — tuning loss across promotion is a product defect,
        # not an inconclusive run.
        self.feed.promote_reverts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('default', record.get('detail', ''))
        report.validate_scenario(record)


class FailoverFeed:
    """A stubbed pair for the failover leg in either role layout.
    `switched` False models a fresh rig — ctrl-a active, ctrl-b
    tracking — while True models the pair after the parameter-tune
    case already ran the a->b switch: ctrl-b is the settled active,
    and the leg must demote it and let it reconverge before its
    promote-back succeeds."""

    def __init__(self, switched=False):
        self.tick = 0
        self.role = {'a': 'standby' if switched else 'active',
                     'b': 'active' if switched else 'standby'}
        self.reconverge_left = 0

    def _refuse(self, url):
        error = urllib.error.HTTPError(url, 409, 'conflict', {}, None)
        error.close()
        raise error

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route = path.partition('?')[0]
        self.tick += 1
        peer = 'b' if host == 'ctrl-b:2' else 'a'
        if (method, route) == ('GET', '/role'):
            body = {'role': self.role[peer], 'tick': self.tick}
            if self.role[peer] == 'standby' and peer == 'b' \
                    and not self.reconverge_left:
                body['sync'] = {'tracking': {'aligned': self.tick}}
            return 200, body
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'points': []}
        if (method, route) == ('POST', '/demote'):
            if self.role[peer] != 'active':
                self._refuse(url)
            self.role[peer] = 'standby'
            # The demoted peer needs a scan boundary behind it before
            # its reconverged state lets a promote through.
            self.reconverge_left = 1
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if peer != 'b' or self.role['b'] != 'standby' \
                    or self.reconverge_left:
                if self.reconverge_left:
                    self.reconverge_left -= 1
                self._refuse(url)
            self.role['b'] = 'active'
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))


class FailoverTests(unittest.TestCase):
    """scenario_failover demotes whichever peer reports settled active:
    ctrl-a on a fresh rig, or ctrl-b when the parameter-tune case
    already ran the a->b switch — the converged ctrl-b takes the
    promote either way."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, switched=False):
        self.feed = FailoverFeed(switched)
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001):
            return scenarios.scenario_failover(ctx)

    def test_fresh_pair_switches(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.role['b'], 'active')
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_switched_pair_recycles_the_active(self):
        record = self.run_scenario(switched=True)
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.role['b'], 'active')
        report.validate_scenario(record)


class PlantLinkFeed:
    """A stubbed pair plus shared plant for the plant-link-loss
    scenario. ctrl-a owns the field; the plant's wire protocol answers
    the scenario's census and fencing probes through `plant_request`.
    `stop`/`start` replace ctx['stop_plant']/ctx['start_plant'] — the
    runner-owned lifecycle actions — and flip `plant_up`; every
    snapshot read advances one scan whose point qualities and
    io_health reflect the link state. Fault flags stage each named
    failure the issue calls out."""

    def __init__(self):
        self.tick = 0
        self.plant_tick = 0
        self.plant_up = True
        self.active_up = True
        self.cycled = False       # the plant went through stop+start
        self.claimed = True       # a writer claim stands on the plant
        self.failed_reads = 0
        self.failed_writes = 0
        self.consecutive = 0
        self.last_error = None
        self.calls = []           # the lifecycle actions run
        # Fault injection for the named-failure cases.
        self.fresh_through = False    # telemetry never degrades
        self.clean_health = False     # io_health never counts the loss
        self.promoted = False         # the standby reports active
        self.aborts = False           # the active's monitor dies on loss
        self.never_returns = False    # start leaves the plant dead
        self.start_raises = False     # the start action itself fails
        self.never_recovers = False   # plant back, reads stay bad
        self.never_reclaims = False   # the claim is never re-taken
        self.resets_health = False    # io_health zeroes on recovery

    # The runner-owned lifecycle actions — replace
    # ctx['stop_plant']/ctx['start_plant'].
    def stop(self):
        self.calls.append('stop')
        self.plant_up = False
        self.claimed = False        # the claim dies with the server
        if self.aborts:
            self.active_up = False

    def start(self):
        self.calls.append('start')
        if self.start_raises:
            raise RuntimeError('docker start failed: no such container')
        if not self.never_returns:
            self.plant_up = True
            self.cycled = True
        if not self.never_reclaims:
            self.claimed = True     # the field owner re-claimed

    # One completed scan per snapshot read: reads fail at the dead
    # link, writes keep landing while the plant is up.
    def _scan(self):
        self.tick += 1
        if self._down():
            if not self.clean_health:
                self.failed_reads += 5
                self.failed_writes += 2
                self.consecutive += 7
                self.last_error = {
                    'tick': self.tick, 'point': 10, 'direction': 'in',
                    'error': {'disconnected': 10}}
        else:
            self.consecutive = 0
            if self.resets_health:
                self.failed_reads = 0
                self.failed_writes = 0
                self.last_error = None

    def _down(self):
        # The link is severed while the plant is stopped; a feed whose
        # never_recovers flag is set keeps it severed past the restart.
        return not self.plant_up \
            or (self.never_recovers and self.cycled)

    def _link(self):
        return 'disconnected' if self._down() else 'connected'

    def _quality(self):
        if self._down():
            return {'bad': 'communication_fault'}
        return 'good'

    # The plant wire protocol — replaces scenarios._plant_probe.
    def plant_request(self, ctx, request, timeout=5):
        if not self.plant_up:
            raise urllib.error.URLError('connection refused')
        if request['op'] == 'list_points':
            sample = {'value': {'float': 1.5}, 'quality': 'good',
                      'tick': self.plant_tick}
            return {'result': 'points', 'points': [
                {'point': 10, 'direction': 'in', 'sample': sample,
                 'fault': None},
                {'point': 11, 'direction': 'in', 'sample': sample,
                 'fault': None},
                {'point': 100, 'direction': 'out', 'sample': sample,
                 'fault': None}]}
        if request['op'] == 'step':
            if self.claimed:
                return {'result': 'error',
                        'error': {'kind': 'fenced',
                                  'detail': 'another attachment owns '
                                            'field writes'}}
            # The field fails closed while unclaimed: a restarted
            # plant's claim-less window is a named refusal, not an
            # open one — so the probe can tell "waiting for the owner's
            # re-arm" from "fenced by a standing owner".
            return {'result': 'error',
                    'error': {'kind': 'unclaimed',
                              'detail': 'no attachment holds '
                                        'field writes'}}
        raise AssertionError('unexpected plant request %s' % request)

    # The monitor surface — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = url.split('/', 3)[3].partition('?')[0]
        route = '/' + route
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                role = 'active' if self.promoted else 'standby'
                report = {'role': role, 'tick': self.tick}
                if role == 'standby':
                    report['sync'] = {'tracking': {'aligned': self.tick}}
                return 200, report
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.active_up:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'level-primary',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False},
                {'point': 11, 'signal': None, 'name': 'level-backup',
                 'direction': 'in', 'value_type': 'float',
                 'writable': False}]}
        if (method, route) == ('GET', '/snapshot'):
            self._scan()
            quality = 'good' if self.fresh_through else self._quality()
            return 200, {'tick': self.tick, 'points': [
                {'point': 10, 'sample': {'value': {'float': 1.5},
                                         'quality': quality}},
                {'point': 11, 'sample': {'value': {'float': 2.5},
                                         'quality': quality}},
                {'point': 100, 'sample': {'value': {'bool': False},
                                          'quality': 'good'}}],
                'io_health': {
                    'failed_reads': self.failed_reads,
                    'failed_writes': self.failed_writes,
                    'consecutive_failures': self.consecutive,
                    'last_error': self.last_error,
                    'scan_overruns': 0,
                    'driver': {'link': self._link(),
                               'last_error': self.last_error
                               and 'connection reset'}}}
        raise AssertionError('unexpected request %s %s' % (method, url))


class PlantLinkLossTests(unittest.TestCase):
    """scenario_plant_link_loss against the stubbed feed: the lifecycle
    actions cycle the shared plant while the monitor surface and the
    wire protocol carry the degradation and recovery evidence."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = PlantLinkFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, ctx_extra=None):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': '127.0.0.1:9',
               'evidence_dir': str(self.evidence),
               'stop_plant': self.feed.stop,
               'start_plant': self.feed.start}
        ctx.update(ctx_extra or {})
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_plant_probe',
                             self.feed.plant_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'LINK_POLL', 0.001), \
                patch.object(scenarios, 'LINK_DEGRADE_DEADLINE', 0.05), \
                patch.object(scenarios, 'LINK_SETTLE', 0.005), \
                patch.object(scenarios, 'LINK_RECOVERY_DEADLINE', 0.05):
            return scenarios.scenario_plant_link_loss(ctx)

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_plant_link_loss,
                      scenarios.SCENARIOS)

    def test_clean_loss_recovery_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.calls, ['stop', 'start'])
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        outage = json.loads(
            (self.evidence / 'plant-link-loss-outage.json').read_text())
        self.assertGreater(
            outage['health']['failed_reads'], 0)
        self.assertEqual(outage['health']['driver']['link'],
                         'disconnected')

    def test_fresh_telemetry_through_outage_fails(self):
        self.feed.fresh_through = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('kept reading good', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unchecked_link_failure_fails(self):
        self.feed.clean_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('io_health', record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_during_outage_fails(self):
        self.feed.promoted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role', record.get('detail', ''))
        report.validate_scenario(record)

    def test_aborted_run_fails(self):
        # The run that dies on field loss: the active's monitor never
        # answers after the stop — the named 'scans continuing rather
        # than aborting' violation.
        self.feed.aborts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('aborted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_plant_is_inconclusive(self):
        self.feed.never_returns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never served again', record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_start_action_is_inconclusive(self):
        self.feed.start_raises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('start', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreclaimed_writer_claim_fails(self):
        self.feed.never_reclaims = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('single-writer claim',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unrecovered_reads_fail(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('Good', record.get('detail', ''))
        report.validate_scenario(record)

    def test_reset_io_health_fails(self):
        self.feed.resets_health = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reset', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_actions_is_inconclusive(self):
        record = self.run_scenario({'stop_plant': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)


class CtlResult:
    """A faked CompletedProcess for the _run_ctl seam."""

    def __init__(self, stdout='', stderr='', returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


class CtlFeed:
    """The faked-subprocess side of the dcs-ctl scenario: run_ctl
    answers each argv the way the real binary would against the
    post-failover rig — reads print the served payloads and exit 0,
    receipted subcommands print the receipt and exit nonzero on a
    named rejection — while http_json covers the raw liveness gate and
    the resources read the unavailable-command probe selects through.
    Fault flags stage each named failure the issue calls out."""

    COMPONENTS = ({'name': 'digital-input:12', 'kind': 'digital-input'},
                  {'name': 'motor:21', 'kind': 'motor'})
    BOUND = {'digital-input:12': {302}, 'motor:21': {302}}
    KINDS = {302: 'bool', 10: 'float'}  # the point kinds /signals declares
    WRITABLE = {302}

    def __init__(self):
        self.tick = 0
        self.next_seq = 1
        self.journal = []
        self.calls = []      # the recorded (addr, argv) transcript
        self.binaries = []   # the binary path each invocation ran
        # Fault injection for the named-failure cases.
        self.role_mismatch = False     # the CLI's role reads both standby
        self.missing_kind = False      # the schema read drops an instance
        self.command_fails = False     # receipted submissions die transport-side
        self.no_journal_entry = False  # the settlement never journals
        self.wrong_actor = False       # the journaled receipt loses the actor
        self.no_events = False         # the events read attributes nothing
        self.silent_accept = False     # the undeclared invoke applies
        self.carryover = None          # a receipt an adopted checkpoint
                                       # re-journals above the cursor

    @staticmethod
    def _ok(payload):
        return json.dumps(payload), 0, ''

    # The subprocess seam — replaces scenarios._run_ctl: one argv
    # answered the way the real binary would against this fake rig.
    def run_ctl(self, binary, addr, args):
        self.binaries.append(binary)
        self.calls.append((addr, list(args)))
        stdout, rc, stderr = self._dispatch(addr, list(args))
        return CtlResult(stdout=stdout, stderr=stderr, returncode=rc)

    def _dispatch(self, addr, args):
        self.tick += 1
        if args == ['role']:
            role = 'standby' if self.role_mismatch else \
                ('active' if addr == 'ctrl-b:2' else 'standby')
            return self._ok({'role': role, 'tick': self.tick})
        if args == ['signals']:
            return self._ok(self._signals())
        if args == ['schema']:
            return self._ok({'publication': self.tick,
                             'tick': self.tick,
                             'interfaces': self._interfaces()})
        if args[0] == 'journal':
            since = int(args[args.index('--since') + 1]) \
                if '--since' in args else 0
            return self._ok([entry for entry in self.journal
                             if entry['seq'] > since])
        if args[0] == 'events':
            component = args[1]
            if self.no_events:
                return self._ok([])
            return self._ok([entry for entry in self.journal
                             if self._attributed(entry, component)])
        # Receipted subcommands — `write`, `set-parameter`, `invoke`:
        # the receipt prints to stdout; a named rejection exits 1.
        actor = None
        if '--actor' in args:
            index = args.index('--actor')
            actor = args[index + 1]
            del args[index:index + 2]
        if self.command_fails:
            return '', 1, 'dcs-ctl: ' + addr + ': connection refused'
        command = self._command_from_args(args)
        refused = self._refusal(command)
        if refused is None:
            receipt = {'command': command,
                       'outcome': {'accepted':
                                   {'apply_tick': self.tick + 1}},
                       'actor': actor}
            rc, stderr = 0, ''
        else:
            receipt = {'command': command,
                       'outcome': {'rejected': {'reason': refused}},
                       'actor': actor}
            rc = 1
            stderr = 'dcs-ctl: ' + addr + ': command rejected: ' \
                + next(iter(refused))
        # An accepted command journals its applied echo at the promised
        # tick; a rejected receipt is already final and echoes verbatim —
        # the real executor's durable record. `carryover` injects the
        # entry a checkpoint-adopted receipt re-journals on this peer —
        # an earlier leg's identical command under its own actor —
        # landing above the consumer's pre-submission cursor, ahead of
        # this submission's own settlement.
        if not self.no_journal_entry:
            if self.carryover is not None:
                self.journal.append({'seq': self.next_seq,
                                     'tick': self.tick,
                                     'event': {'command_settled': {
                                         'receipt': self.carryover}}})
                self.next_seq += 1
                self.carryover = None
            settled = dict(receipt)
            if refused is None:
                settled['outcome'] = {'applied': {'tick': self.tick + 1}}
            if self.wrong_actor:
                settled['actor'] = 'qa-lane'
            self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                                 'event': {'command_settled': {
                                     'receipt': settled}}})
            self.next_seq += 1
        return json.dumps(receipt), rc, stderr

    @staticmethod
    def _literal(text):
        if text == 'true':
            return {'bool': True}
        if text == 'false':
            return {'bool': False}
        try:
            return {'int': int(text)}
        except ValueError:
            return {'float': float(text)}

    def _command_from_args(self, args):
        """The Command a receipted argv denotes — write/set-parameter/
        invoke with the declared point kind the real binary reads out
        of the signal index."""
        if args[0] == 'write':
            point = int(args[1])
            value = self._literal(args[2])
            kind = self.KINDS.get(point) or next(iter(value))
            return {'write_value': {'point': point, 'kind': kind,
                                    'value': value}}
        if args[0] == 'set-parameter':
            return {'set_parameter': {'component': args[1],
                                      'name': args[2],
                                      'value': self._literal(args[3])}}
        if args[0] == 'invoke':
            arguments = {}
            for pair in args[3:]:
                name, _, text = pair.partition('=')
                arguments[name] = self._literal(text)
            return {'invoke': {'component': args[1],
                               'command': args[2],
                               'arguments': arguments}}
        raise AssertionError('unexpected dcs-ctl argv %s' % args)

    def _refusal(self, command):
        """The named rejection a submission meets — mirroring the
        executor's validation order — or None when it is admitted."""
        write = command.get('write_value')
        if write is not None and write['point'] not in self.WRITABLE:
            return {'not_writable': {'point': write['point']}}
        invoke = command.get('invoke')
        if invoke is not None:
            interface = self._served().get(invoke['component'])
            if interface is None:
                return {'unknown_component':
                        {'component': invoke['component']}}
            names = {spec['name'] for spec in interface['commands']}
            if invoke['command'] not in names \
                    and not self.silent_accept:
                return {'unknown_command': {
                    'component': invoke['component'],
                    'command': invoke['command']}}
        return None

    def _interface(self, kind):
        """One kind's declared interface — a writable-bool write, an
        unavailable non-writable write (the unavailable probe's
        target), a parameter tune, and the emitted settled-event
        entry."""
        return {'version': 1, 'kind': kind,
                'measurements': [{'name': 'in', 'kind': 'bool'}],
                'configuration': [], 'state': [],
                'commands': [
                    {'name': 'write_value:in', 'point': 302,
                     'request': [{'name': 'value', 'kind': 'bool'}],
                     'availability': 'bound_point_writable',
                     'adapted': 'write_value'},
                    {'name': 'write_value:raw', 'point': 10,
                     'request': [{'name': 'value', 'kind': 'float'}],
                     'availability': 'bound_point_writable',
                     'adapted': 'write_value'},
                    {'name': 'set_parameter:invert',
                     'request': [{'name': 'value', 'kind': 'bool'}],
                     'availability': 'always',
                     'adapted': 'set_parameter'}],
                'events': [{'name': 'command_settled', 'payload': [],
                            'retention': 'journal',
                            'emission': 'on_command_settled',
                            'adapted': 'command_settled'}]}

    def _interfaces(self):
        interfaces = [{'name': record['name'],
                       'interface': self._interface(record['kind'])}
                      for record in self.COMPONENTS]
        return interfaces[:1] if self.missing_kind else interfaces

    def _served(self):
        return {entry['name']: entry['interface']
                for entry in self._interfaces()}

    def _signals(self):
        return {'points': [
            {'point': 302, 'signal': 10302, 'name': 'p101-oos',
             'direction': 'in', 'value_type': 'bool', 'writable': True},
            {'point': 10, 'signal': 10010, 'name': 'level-primary',
             'direction': 'in', 'value_type': 'float',
             'writable': False}],
            'components': [dict(record) for record in self.COMPONENTS]}

    def _resources(self, record):
        commands = []
        for spec in self._interface(record['kind'])['commands']:
            available = spec.get('adapted') != 'write_value' \
                or spec.get('point') in self.WRITABLE
            commands.append({'name': spec['name'], 'available': available,
                             'refusal': None if available else
                             'point ' + str(spec.get('point'))
                             + ' is not writable'})
        return {'name': record['name'], 'kind': record['kind'],
                'measurements': [], 'configuration': [], 'state': [],
                'commands': commands, 'events': []}

    def _attributed(self, entry, name):
        """The per-component events attribution, mirroring the served
        view's rule: commands settle against their component or their
        bound point; emitted events name their producer."""
        event = entry.get('event') or {}
        bound = self.BOUND.get(name, set())
        emitted = event.get('event_emitted') or {}
        if emitted.get('component') == name:
            return True
        receipt = (event.get('command_settled') or {}) \
            .get('receipt') or {}
        command = receipt.get('command') or {}
        component = (command.get('invoke') or {}).get('component') \
            or (command.get('set_parameter') or {}).get('component') \
            or (command.get('force') or {}).get('component')
        point = (command.get('write_value') or {}).get('point')
        return component == name or point in bound

    # The raw channel the scenario still crosses — the pair's liveness
    # gate and the resources read the unavailable probe selects
    # through.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active' if host == 'ctrl-b:2'
                         else 'standby', 'tick': self.tick}
        if (method, route) == ('GET', '/resources'):
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'components': [self._resources(record)
                                        for record in self.COMPONENTS]}
        raise AssertionError('unexpected request %s %s' % (method, url))


class DcsCtlTests(unittest.TestCase):
    """scenario_dcs_ctl behind the faked-subprocess seam: CtlFeed
    models the binary's argv contract so every pass/fail leg the issue
    calls out — role layout, schema coverage, the journaled and
    actor-attributed settlement, event attribution, the named refusals,
    and an unavailable binary — runs through the real scenario path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.binary = Path(self.tmp.name) / 'dcs-ctl'
        self.binary.write_text('#!/bin/sh\nexit 0\n')
        self.binary.chmod(0o755)
        self.feed = CtlFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'dcs_ctl': str(self.binary)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, '_run_ctl', self.feed.run_ctl), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'CTL_DEADLINE', 0.5):
            return scenarios.scenario_dcs_ctl(ctx)

    def test_registered_last_and_replayable(self):
        self.assertIs(scenarios.SCENARIOS[-1],
                      scenarios.scenario_dcs_ctl)
        self.assertIs(verify.case_function('dcs-ctl'),
                      scenarios.scenario_dcs_ctl)

    def test_clean_rig_passes_with_full_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        for name in ('dcs-ctl-roles', 'dcs-ctl-schema', 'dcs-ctl-invoke',
                     'dcs-ctl-journal', 'dcs-ctl-events',
                     'dcs-ctl-refusals', 'dcs-ctl-transcript'):
            self.assertTrue((self.evidence / (name + '.json')).is_file(),
                            name)
        transcript = json.loads(
            (self.evidence / 'dcs-ctl-transcript.json').read_text())
        argvs = [entry['argv'] for entry in transcript]
        # The consumer contract end to end: role on both endpoints, the
        # picked command with the declared actor, the journal and
        # events reads, and both refusal probes.
        self.assertIn(['ctrl-a:1', 'role'], argvs)
        self.assertIn(['ctrl-b:2', 'role'], argvs)
        self.assertIn(['ctrl-b:2', 'write', '302', 'true',
                       '--actor', scenarios.CTL_ACTOR], argvs)
        self.assertIn(['ctrl-b:2', 'events', 'digital-input:12'], argvs)
        self.assertIn(['ctrl-b:2', 'invoke', 'digital-input:12',
                       'dcs-ctl-undeclared', '--actor',
                       scenarios.CTL_ACTOR], argvs)
        self.assertIn(['ctrl-b:2', 'write', '10', '1.0',
                       '--actor', scenarios.CTL_ACTOR], argvs)
        self.assertTrue(self.feed.binaries)
        self.assertTrue(all(binary == str(self.binary)
                            for binary in self.feed.binaries))
        refusals = json.loads(
            (self.evidence / 'dcs-ctl-refusals.json').read_text())
        self.assertEqual(refusals['undeclared']['exit'], 1)
        self.assertEqual(refusals['unavailable']['exit'], 1)

    def test_two_runs_produce_identical_transcript(self):
        record = self.run_scenario()
        first = (self.evidence / 'dcs-ctl-transcript.json').read_text()
        self.evidence = self.evidence.parent / 'evidence-2'
        self.evidence.mkdir()
        self.feed = CtlFeed()
        again = self.run_scenario()
        second = (self.evidence / 'dcs-ctl-transcript.json').read_text()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(again['outcome'], 'passed', again)
        self.assertEqual(first, second)

    def test_role_mismatch_fails(self):
        self.feed.role_mismatch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('one standby', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_schema_kind_fails(self):
        self.feed.missing_kind = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('misses declared kinds', record.get('detail', ''))
        self.assertIn('motor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_receipt_fails(self):
        self.feed.command_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no settled receipt', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_settlement_fails(self):
        self.feed.no_journal_entry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never recorded', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unattributed_actor_fails(self):
        self.feed.wrong_actor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unattributed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_stale_identical_settlement_is_not_this_legs(self):
        """The qa-20260916-065 defect: the served-interface case
        submits the same picked command under actor 'qa-lane' ahead of
        this leg, so the journal already holds an identical settled
        receipt — the settlement read must start above the
        pre-submission seq cursor, not match the stale entry."""
        stale = {'seq': self.feed.next_seq, 'tick': 0,
                 'event': {'command_settled': {'receipt': {
                     'command': {'write_value': {
                         'point': 302, 'kind': 'bool',
                         'value': {'bool': True}}},
                     'outcome': {'applied': {'tick': 0}},
                     'actor': 'qa-lane'}}}}
        self.feed.journal.append(stale)
        self.feed.next_seq += 1
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        journal = json.loads(
            (self.evidence / 'dcs-ctl-journal.json').read_text())
        self.assertEqual(
            journal['entry']['event']['command_settled']['receipt']
            ['actor'], scenarios.CTL_ACTOR)

    def test_carried_over_settlement_is_not_this_legs(self):
        """The qa-20260917-002 defect: an earlier leg submits the same
        picked command under actor 'qa-lane'; the receipt crosses peers
        inside a checkpoint and re-journals on the serving peer only
        when the adopting scan records it — landing above this leg's
        pre-submission seq cursor, ahead of this submission's own
        settlement. The settlement read must identify this leg's
        receipt by the actor only it declares, not take the first
        same-command entry past the floor."""
        self.feed.carryover = {
            'command': {'write_value': {'point': 302, 'kind': 'bool',
                                        'value': {'bool': True}}},
            'outcome': {'applied': {'tick': 40}},
            'actor': 'qa-lane'}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        journal = json.loads(
            (self.evidence / 'dcs-ctl-journal.json').read_text())
        entry = journal['entry']['event']['command_settled']['receipt']
        self.assertEqual(entry['actor'], scenarios.CTL_ACTOR)
        self.assertEqual(
            journal['foreign']['event']['command_settled']['receipt']
            ['actor'], 'qa-lane')

    def test_unattributed_event_fails(self):
        self.feed.no_events = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('attributes no produced event',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_silently_accepted_undeclared_invoke_fails(self):
        self.feed.silent_accept = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not refused by name', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unavailable_binary_is_inconclusive(self):
        stub = Path(self.tmp.name) / 'stub-ctl'
        stub.write_text('')   # present but not executable
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        for value in (None, str(Path(self.tmp.name) / 'no-such-ctl'),
                      str(stub)):
            if value is None:
                ctx.pop('dcs_ctl', None)
            else:
                ctx['dcs_ctl'] = value
            record = scenarios.scenario_dcs_ctl(ctx)
            self.assertEqual(record['outcome'], 'inconclusive',
                             (value, record))
            report.validate_scenario(record)


if __name__ == '__main__':
    unittest.main()
