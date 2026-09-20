"""The deterministic scenarios' unit coverage: stubbed monitor feeds
drive scenario_consumer_schedule, scenario_served_interface,
scenario_force_release, scenario_command_admission,
scenario_controller_restart, scenario_plant_link_loss,
scenario_field_fault, scenario_field_claim,
scenario_unavailable_fallback, and
scenario_model_revision through their
pass outcomes and the named
failures their issues call out — a stalled reader whose leg's scan
outputs stopped advancing, a lagging seq-cursor read answered with
silently stale data, a served registry missing a declared kind or
collection, a declared command returning no receipt, an
emitted-events view that never reflects the produced event, forced
telemetry missing its Substituted stamp or forces badge, control that
ignores the force, unattributed or never-journaled settlements, a
badge that never clears, a released point that never returns to Good
or whose cone stays tainted, a tracking standby whose adopted
snapshot never shows the recovered state, a pair that never
converges, a command flood whose submissions meet dropped receipts,
HTTP-layer faults, unsettled admissions, or a bound that never fills,
a restarted controller that resumes cold, a plant outage whose
telemetry stays fresh, whose standby promotes, whose writer claim
never re-arms, or whose io_health forgets the failures it counted, an
injected quality fault the served snapshot keeps reporting Good, an
error fault that never surfaces on io_health, a role that moves under
a field fault, a clear that never restores the field value, a model
revision whose revised peer never converges, converges to the wrong
sync state, loses receipts across the boundary, regresses the field, or
ends on the wrong fingerprint, a foreign-fingerprint standby
that never reports the named negotiation refusal, converges anyway,
accepts promotion, or leaves the active peer disturbed, an
incompatible revision whose peer silently crosses, degrades on the
wrong detail, promotes anyway, answers the wrong refusal, or disturbs
the active's receipts, journal, or field — plus a control half that
refuses to converge — a cold-restarted source whose tracking peer
rewinds its run tick adopting the regressed checkpoint stream, never
journals or double-journals the resync, misreports its fields,
promotes spuriously, re-takes the field after its fenced demotion,
keeps its cleared alignment, or leaves the writer claim unheld — and
a doomed foreign startup whose corrupt journal file must never strand
a claim fencing the incumbent: the pre-fix shape that preempts on its
way out, the field that goes unclaimed or silently writable, the
doomed peer that serves or journaled past its failed replay, and
every incumbent disturbance — demotion, stalled ticks, stalled
writes, refused or lost receipts — the ordering fix exists to
prevent — plus the served per-command availability verdicts: an
inconsistent `available`/`refusal` row, a served-unavailable command
that applies anyway or settles a different reason than the row
served, a served-available command settling refused, a tracking
peer's diverged verdicts, the empty-row inconclusive path — and the
demote-raced settlement audit: an admission journaled both applied
and superseded, a raced admission that vanishes unsettled, a
superseded value minted on the fenced image, an adopted log
disagreeing with its journal, refused switch steps, and the
unconverged or unreachable peer — and an all-level-sources-bad leg
whose backup-degraded annunciation stays silent or moves the
selection, whose all-bad state never engages the declared fallback,
whose served output holds its last Good stamp or keeps controlling
on untrusted data, whose recovered primary never re-selects, whose
annunciation never clears, or whose rig never presents the leg's
contract."""
import copy
import io
import json
import math
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from qa_lane import report, runner, scenarios, verify


HELD_RESPONSE = (b'HTTP/1.1 200 OK\r\nContent-Length: 26\r\n\r\n'
                 b'{"tick": 12, "points": []}')


def _ctl_request(args):
    """The one wire request the shipped dcs-plant-ctl sends for an
    argv covering `list`, `read`, `fault`, and `clear-fault` — the
    subcommands the migrated scenarios drive. `write`/`step` are the
    tool's claimed mutations and the claim ops are the ones it does
    not expose; the scenarios keep those on the raw client, so an argv
    carrying them means a leg drifted off the covered seam."""
    op = args[0] if args else None
    if op == 'list':
        return {'op': 'list_points'}
    if op == 'read':
        return {'op': 'read', 'point': int(args[1])}
    if op == 'fault':
        return {'op': 'inject_fault', 'point': int(args[1]),
                'fault': _ctl_fault(args[2])}
    if op == 'clear-fault':
        return {'op': 'clear_fault', 'point': int(args[1])}
    raise AssertionError('plant_ctl argv outside the covered '
                         'subcommands: %s' % (args,))


def _ctl_fault(arg):
    """The Fault value the tool's `fault` argument parses to:
    disconnected/timeout error faults, uncertain[:reason]/bad[:reason]
    quality faults (defaulting to unspecified)."""
    kind, _, reason = arg.partition(':')
    if kind in ('disconnected', 'timeout') and not reason:
        return kind
    if kind in ('uncertain', 'bad'):
        return {'quality': {kind: reason or 'unspecified'}}
    raise AssertionError('unparseable fault argument %s' % arg)


def _ctl_process(body=None, stderr='', returncode=0):
    """A CompletedProcess-shaped answer for a faked ctx['plant_ctl']
    seam — the shape runner.plant_ctl's `docker exec` returns: the
    response JSON on stdout at exit 0, the refusal on stderr at exit
    1."""
    return subprocess.CompletedProcess(
        args=('dcs-plant-ctl',), returncode=returncode,
        stdout=json.dumps(body) if returncode == 0 else '',
        stderr=stderr)


def _ctl_wrap(dispatch, *args):
    """Run argv through a stubbed wire dispatch as the shipped tool
    would: the response JSON at exit 0, or the refusal's detail at
    exit 1 — a transport failure raising is the tool's nonzero exit
    too."""
    try:
        body = dispatch(_ctl_request(args))
    except AssertionError:
        raise
    except Exception as exc:
        return _ctl_process(stderr=str(exc), returncode=1)
    if (body or {}).get('result') == 'error':
        return _ctl_process(stderr=json.dumps(body.get('error')),
                            returncode=1)
    return _ctl_process(body)


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


class SourceRestartFeed:
    """A stubbed pair for the source-restart scenario. ctrl-a owns the
    field — each cold restart drops its tick to the resume mark and
    opens a new run on its --journal-file — while ctrl-b tracks its
    checkpoint stream: every ctrl-b request is one completed tracking
    cycle, the pull applying under the generation-offset rule —
    regressed streams journal one source_restarted and adopt at the
    run tick, never rewinding it — unless a fault flag stages the
    finding's defect. ctrl-b's --journal-file is a real append-only
    file the feed writes; the plant's single-writer claim the fencing
    probes answer is a token the feed's claim/demote/cold-start
    transitions move — a stopped owner's token still fences."""

    def __init__(self, journal_a, journal_b):
        self.a_tick = 400
        self.b_tick = 400
        self.aligned = 400        # ctrl-b's applied stream alignment
        self.offset = 0           # ctrl-b's generation tick_offset
        self.a_up = True
        self.a_role = 'active'
        self.b_role = 'standby'
        self.claimed = 'a'        # the plant's standing writer token
        self.journal_a = Path(journal_a)
        self.journal_b = Path(journal_b)
        self.seq_a = 1
        self.seq_b = 1
        self.runs_a = 1
        self.served_b = []        # ctrl-b's served journal ring
        self.history = []         # ctrl-b's served /history rows
        self.restarts = []
        # Fault flags staging the named failures.
        self.returns = True        # False: the cold-restart action fails
        self.serves = True         # False: the monitor never returns
        self.warm = False          # the state file survives the restart
        self.rewinds = False       # the regressed apply drops the run tick
        self.skips_journal = False  # the resync never journals
        self.double_restart = False  # two source_restarted per regression
        self.peer_promotes = False   # ctrl-b reports promoting mid-gap
        self.claim_lapses = False    # the plant drops the claim mid-gap
        self.never_tracks = False    # ctrl-b never reports tracking
        self.wrong_fields = False    # the journaled resync misreports
        self.keeps_alignment = False  # the demotion forgets no alignment
        self.repromotes = False      # the fenced peer re-takes the field
        self._append(self.journal_a,
                     {'run_boundary': {'run': 1, 'tick': 0}})
        self._append(self.journal_b,
                     {'run_boundary': {'run': 1, 'tick': 0}})

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _a_entry(self, event):
        entry = {'seq': self.seq_a, 'tick': self.a_tick,
                 'event': event}
        self._append(self.journal_a, {'entry': entry})
        self.seq_a += 1

    def _b_entry(self, event, tick=None):
        entry = {'seq': self.seq_b,
                 'tick': self.b_tick if tick is None else tick,
                 'event': event}
        self._append(self.journal_b, {'entry': entry})
        self.served_b.append(entry)
        self.seq_b += 1

    def _fence_demote(self):
        # The fenced-write demotion: the claim loss journals once, the
        # role walks demoting -> standby, and the alignment clears —
        # the demoted peer's next pull is the no-prior-alignment form.
        self._b_entry({'field_claim_lost': {'point': 10}})
        self._b_entry({'role_changed': {'from': 'active',
                                        'to': 'demoting'}})
        self._b_entry({'role_changed': {'from': 'demoting',
                                        'to': 'standby'}})
        self.b_role = 'standby'
        if not self.keeps_alignment:
            self.aligned = None
            self.offset = 0
        self.b_tracking = False

    def _b_scan(self):
        """One tracking cycle on ctrl-b: while it owns the field the
        scan writes — a preempted claim fences it into demotion — and
        while it stands by, the pull applies under the
        generation-offset rule before the scan's tick advance."""
        if self.b_role == 'promoting':
            self.b_role = 'active'
        if self.repromotes and self.b_role == 'standby' \
                and self.aligned is None and self.a_up:
            # The defect: a demoted peer asserting the field again.
            self.b_role = 'active'
            self.claimed = 'b'
        if self.b_role == 'active':
            if self.claimed != 'b':
                self._fence_demote()
            self.b_tick += 1
            self._history_row()
            return
        if self.a_up:
            ckpt = self.a_tick
            regressed = ckpt < self.aligned if self.aligned is not None \
                else ckpt < self.b_tick
            if regressed:
                was_aligned = self.aligned
                # The generation offset: the adopt lands at the run
                # tick and later checkpoints land under it — the
                # defect form lands at the stream's own tick instead.
                self.offset = 0 if self.rewinds \
                    else self.b_tick - ckpt
                if self.rewinds:
                    self.b_tick = ckpt
                if not self.skips_journal:
                    fields = {'was_aligned': None if self.wrong_fields
                              else was_aligned,
                              'resumed_at': ckpt}
                    self._b_entry({'source_restarted': fields})
                    if self.double_restart:
                        self._b_entry({'source_restarted':
                                       dict(fields)})
                self.aligned = ckpt
                self.b_tracking = True
            else:
                # An ordinary apply lands under the standing offset.
                self.aligned = ckpt
                self.b_tick = ckpt + self.offset
                self.b_tracking = True
        else:
            # The pull missed — the heartbeat degrades until the
            # stream serves again.
            self.b_tracking = False
        self.b_tick += 1
        self._history_row()

    def _history_row(self):
        self.history.append({'seq': len(self.history) + 1,
                             'sample': {'tick': self.b_tick,
                                        'value': {'bool': True},
                                        'quality': 'good'}})

    def _b_sync(self):
        if self.b_role != 'standby':
            return None
        if self.never_tracks:
            return 'unsynchronized'
        if self.b_tracking:
            return {'tracking': {'aligned': self.aligned}}
        if not self.a_up:
            return {'degraded': {'detail': 'checkpoint pull failed'}}
        return 'unsynchronized'

    # The runner-owned action's simulated half: the container cycles,
    # the state file is gone (the real action's unlink is exercised in
    # the test's ctx wiring), the journal opens a new lifetime at the
    # resume tick, and the fresh process claims the field.
    def cold_restart(self, name):
        self.restarts.append(name)
        if not self.returns:
            raise RuntimeError('docker start failed: no such container')
        assert name == 'active'
        persisted = self.a_tick
        self.a_up = False
        self.down_left = 2
        self.a_tick = persisted if self.warm else 0
        self.a_role = 'active'
        self.claimed = None if self.claim_lapses else 'a'
        self.runs_a += 1
        self._append(self.journal_a, {'run_boundary': {
            'run': self.runs_a, 'tick': self.a_tick}})

    def try_plant(self, ctx, request):
        """The plant-protocol fencing probe: `step` answers fenced
        while any attachment holds the single-writer claim — a stopped
        owner's token still fences."""
        if request.get('op') == 'step':
            if self.claimed is None:
                return {'error': {'kind': 'unclaimed'}}
            return {'error': {'kind': 'fenced',
                              'detail': 'the writer claim is held'}}
        raise AssertionError('unexpected plant request %s' % (request,))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if host == 'ctrl-b:2':
            self._b_scan()
            if (method, route) == ('GET', '/role'):
                role = self.b_role
                if self.peer_promotes and not self.a_up:
                    role = 'promoting'
                return 200, {'role': role, 'tick': self.b_tick,
                             'sync': self._b_sync()}
            if (method, route) == ('GET', '/snapshot'):
                return 200, {'tick': self.b_tick, 'points': [
                    {'point': 10, 'sample': {
                        'value': {'bool': True}, 'quality': 'good'}}]}
            if (method, route) == ('GET', '/history'):
                return 200, [{'point': 10, 'samples': list(
                    self.history)}]
            if (method, route) == ('GET', '/journal'):
                return 200, list(self.served_b)
            if (method, route) == ('POST', '/promote'):
                if not self.b_tracking:
                    return 409, {'refused': {'not_converged': {
                        'sync': self._b_sync()}}}
                self._b_entry({'role_changed': {'from': 'standby',
                                                'to': 'promoting'}})
                self._b_entry({'role_changed': {'from': 'promoting',
                                                'to': 'active'}})
                self.b_role = 'promoting'
                self.claimed = 'b'
                return 200, {'role': 'promoting', 'tick': self.b_tick}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if not self.a_up:
            self.down_left -= 1
            if self.down_left <= 0 and self.serves:
                self.a_up = True
            else:
                raise urllib.error.URLError('connection refused')
        self.a_tick += 1
        if (method, route) == ('GET', '/role'):
            return 200, {'role': self.a_role, 'tick': self.a_tick,
                         'sync': None}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.a_tick, 'points': []}
        if (method, route) == ('POST', '/demote'):
            if self.a_role != 'active':
                return 409, {'refused': 'not_active'}
            self._a_entry({'role_changed': {'from': 'active',
                                            'to': 'demoting'}})
            self._a_entry({'role_changed': {'from': 'demoting',
                                            'to': 'standby'}})
            self.a_role = 'standby'
            # The demote path's release hook drops this peer's claim.
            if self.claimed == 'a':
                self.claimed = None
            return 200, {'role': 'demoting', 'tick': self.a_tick}
        if (method, route) == ('POST', '/promote'):
            return 409, {'refused': {'not_converged': {
                'sync': 'unsynchronized'}}}
        raise AssertionError('unexpected request %s %s' % (method, url))


class SourceRestartTests(unittest.TestCase):
    """scenario_source_restart against the stubbed pair: the tracked
    peer adopts each regressed checkpoint stream at its own run tick
    — monotonic served ticks, newest-last /history, exactly one
    journaled source_restarted per regression carrying the recorded
    fields, the demoted-peer leg's no-prior-alignment form, no
    spurious promotion, and the field's claim fenced throughout."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.run_dir = Path(self.tmp.name) / 'runs' / 'qa-1'
        self.dirs = {}
        for name in ('a', 'b'):
            directory = self.run_dir / 'controllers' / name
            directory.mkdir(parents=True)
            (directory / 'state.json').write_text('{"tick": 400}')
            self.dirs[name] = directory
        self.journal_a = self.dirs['a'] / 'journal.jsonl'
        self.journal_b = self.dirs['b'] / 'journal.jsonl'
        self.feed = SourceRestartFeed(self.journal_a, self.journal_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, evidence=None, ctx=None):
        feed = feed if feed is not None else self.feed
        evidence = evidence if evidence is not None else self.evidence
        calls, events = [], []

        def fake_docker(*args, timeout=120, check=True):
            calls.append(args)
            return None

        def cold_restart(name):
            # The real runner action — the state.json drop under the
            # bounded run dir — then the feed's simulated resume.
            runner.cold_restart_controller(
                'qa-1', self.run_dir, name,
                lambda event, detail=None: events.append(event))
            feed.cold_restart(name)

        base = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
                'plant': '127.0.0.1:9999',
                'evidence_dir': str(evidence),
                'cold_restart_controller': cold_restart,
                'journal_files': {'active': str(self.journal_a),
                                  'standby': str(self.journal_b)},
                'state_files': {'active': str(self.dirs['a']
                                              / 'state.json'),
                                'standby': str(self.dirs['b']
                                               / 'state.json')}}
        if ctx is not None:
            base.update(ctx)
        patches = {'POLL_INTERVAL': 0.001, 'SOURCE_RESTART_POLL': 0.001,
                   'SOURCE_RESTART_RETURN_DEADLINE': 0.5,
                   'SOURCE_RESTART_SETTLE_DEADLINE': 0.5,
                   'SOURCE_RESTART_JOURNAL_DEADLINE': 0.3,
                   'SOURCE_RESTART_BASELINE_DEADLINE': 0.3}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_try_plant', feed.try_plant), \
                patch.object(runner, 'docker', fake_docker):
            for key, value in patches.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            record = scenarios.scenario_source_restart(base)
        return record, calls, events

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_source_restart,
                      scenarios.SCENARIOS)

    def test_clean_rig_passes_and_validates(self):
        record, calls, events = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.restarts, ['active', 'active'])
        # The real runner action ran each leg: both cold-restart halves
        # on the timeline and both containers' state files dropped.
        self.assertIn('controller-cold-restart', events)
        self.assertIn('controller-cold-restarted', events)
        self.assertEqual(calls.count(('start', 'dcs-hw-qa-1-a')), 2)
        self.assertFalse((self.dirs['a'] / 'state.json').exists())
        self.assertTrue((self.dirs['a'] / 'journal.jsonl').exists())
        self.assertTrue((self.dirs['b'] / 'state.json').exists())
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Both journaled forms landed: the tracked leg's prior
        # alignment and the demoted leg's no-prior-alignment form.
        restarts = scenarios._source_restarts(
            scenarios._journal_entries(self.journal_b))
        self.assertEqual(len(restarts), 2)
        self.assertIsInstance(restarts[0]['was_aligned'], int)
        self.assertIsNone(restarts[1]['was_aligned'])
        self.assertLess(restarts[0]['resumed_at'],
                        restarts[0]['was_aligned'])
        # The restarted peer's journal kept both lifetimes' cold
        # markers beside the launch's.
        bounds = [item['run_boundary'] for item in
                  scenarios._journal_entries(self.journal_a)
                  if 'run_boundary' in item]
        self.assertEqual([b['tick'] for b in bounds], [0, 0, 0])

    def test_rewinding_peer_fails(self):
        # The finding's defect: the regressed apply lands at the
        # stream's own tick — the served run tick rewinds.
        self.feed.rewinds = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rewound', record.get('detail', ''))
        report.validate_scenario(record)

    def test_spurious_promotion_fails(self):
        self.feed.peer_promotes = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('spurious', record.get('detail', ''))
        report.validate_scenario(record)

    def test_repromoted_peer_fails(self):
        # Leg two's defect: the fenced peer re-asserts the field claim
        # after its demotion instead of tracking the new stream.
        self.feed.repromotes = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('re-took', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unjournaled_resync_fails(self):
        self.feed.skips_journal = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('source_restarted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_doubled_resync_entry_fails(self):
        self.feed.double_restart = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('exactly one', record.get('detail', ''))
        report.validate_scenario(record)

    def test_misreported_alignment_fails(self):
        self.feed.wrong_fields = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('prior alignment', record.get('detail', ''))
        report.validate_scenario(record)

    def test_lapsed_claim_fails(self):
        self.feed.claim_lapses = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('claim', record.get('detail', ''))
        report.validate_scenario(record)

    def test_kept_alignment_fails_the_demoted_leg(self):
        # The demotion never cleared the alignment: leg two's entry
        # carries a prior alignment instead of the no-alignment form.
        self.feed.keeps_alignment = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no-prior-alignment', record.get('detail', ''))
        report.validate_scenario(record)

    def test_warm_resume_is_inconclusive(self):
        # The state file survived: the resumed stream never regressed,
        # so the adoption contract was never exercised.
        self.feed.warm = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never produced a regressed stream',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_failed_action_is_inconclusive(self):
        self.feed.returns = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never completed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unreturned_monitor_is_inconclusive(self):
        self.feed.serves = False
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never returned', record.get('detail', ''))
        report.validate_scenario(record)

    def test_untracked_peer_is_inconclusive(self):
        self.feed.never_tracks = True
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no adopter', record.get('detail', ''))
        report.validate_scenario(record)

    def test_peer_owned_field_is_inconclusive(self):
        # The post-failover layout: the field owner is the rig's only
        # tracking peer — no tracked endpoint can adopt.
        self.feed.a_role = 'standby'
        self.feed.b_role = 'active'
        self.feed.claimed = 'b'
        record, _, _ = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('checkpoint-tracking peer',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_action_is_inconclusive(self):
        record, _, _ = self.run_scenario(
            ctx={'cold_restart_controller': None})
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no cold-restart action',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        # The deterministic-rerun contract: two runs over the same rig
        # transitions record the same report and evidence bytes — the
        # feed's behavior is call-count keyed, never wall-clock. The
        # evidence embeds the journal paths, so both runs reuse them.
        runs = []
        for _index in range(2):
            for path in (self.journal_a, self.journal_b):
                if path.exists():
                    path.unlink()
            for stale in self.evidence.iterdir():
                stale.unlink()
            for directory in self.dirs.values():
                (directory / 'state.json').write_text('{"tick": 400}')
            feed = SourceRestartFeed(self.journal_a, self.journal_b)
            record, _, _ = self.run_scenario(feed=feed)
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


class RetentionFeed:
    """A stubbed pair for the event-retention scenario. ctrl-a is the
    active and ctrl-b its tracking standby; the served model declares
    one component whose interface carries a journal-, a history-, and
    a latest-retained kind-emitted event beside a kind-declared
    command whose every accepted submission emits all three — the
    emit-identical record the standby mirrors, so its journal carries
    the journal-retained `fired` too while the routed `shift`/`beat`
    only ever land in their own stores. Fault flags stage each named
    failure the issue calls out."""

    COMPONENTS = ({'name': 'seq:1', 'kind': 'sequencer'},)

    def __init__(self):
        self.tick = 0
        self.journal = []        # the active's journal entries
        self.peer_journal = []   # the tracking standby's — emit-identical
        self.history = []        # the routed event-history ring's records
        self.latest = {}         # (component, event) -> standing record
        self.latest_extra = []   # spurious second standing records
        self.seq = 1             # the active journal's next seq
        self.peer_seq = 1        # the standby journal's next seq
        self.event_seq = 1       # the routed stream's next seq
        self.emissions = 0
        # Fault injection for the named-failure cases.
        self.history_to_journal = False  # `shift` lands in the journal
        self.history_freeze = False      # repeats add no `shift` records
        self.latest_accumulates = False  # `beat` stands twice
        self.latest_stale = False        # repeats leave `beat` unchanged
        self.standby_journals = False    # standby journals `shift`
        self.standby_admits = False      # standby accepts the drive
        self.peer_undocumented = False   # standby serves no descriptor
        self.no_qualifier = False        # no routed retentions declared
        self.unserved = False            # /resources omits the component
        self.no_path = False             # no commands to drive through
        self.no_emissions = False        # accepted drives emit nothing

    def _interface(self):
        events = [{'name': 'fired', 'payload': [],
                   'retention': 'journal', 'emission': 'kind_emitted',
                   'adapted': 'declared'}]
        if not self.no_qualifier:
            events += [
                {'name': 'shift', 'payload': [],
                 'retention': 'history', 'emission': 'kind_emitted',
                 'adapted': 'declared'},
                {'name': 'beat', 'payload': [],
                 'retention': 'latest', 'emission': 'kind_emitted',
                 'adapted': 'declared'}]
        commands = [] if self.no_path else [
            {'name': 'advance',
             'request': [{'name': 'count', 'kind': 'int'}],
             'availability': 'kind_declared', 'adapted': 'declared'}]
        return {'version': 1, 'kind': 'sequencer',
                'measurements': [], 'configuration': [], 'state': [],
                'commands': commands, 'events': events}

    def _record(self, name, retention, n):
        record = {'seq': self.event_seq, 'tick': self.tick,
                  'retention': retention,
                  'event': {'event_emitted': {'event': {
                      'event': name, 'component': 'seq:1',
                      'fields': {'n': {'int': n}}}}}}
        self.event_seq += 1
        return record

    # One emission batch per applied drive: `fired` journals on both
    # peers — the emit-identical durable record — `shift` routes to the
    # event-history ring, `beat` overwrites the standing record.
    def _emit(self):
        if self.no_emissions:
            return
        self.emissions += 1
        n = self.emissions
        for journal, key in ((self.journal, 'seq'),
                             (self.peer_journal, 'peer_seq')):
            journal.append({'seq': getattr(self, key),
                            'tick': self.tick,
                            'event': {'event_emitted': {'event': {
                                'event': 'fired', 'component': 'seq:1',
                                'fields': {'n': {'int': n}}}}}})
            setattr(self, key, getattr(self, key) + 1)
        if self.history_to_journal:
            self.journal.append(
                {'seq': self.seq, 'tick': self.tick,
                 'event': {'event_emitted': {'event': {
                     'event': 'shift', 'component': 'seq:1',
                     'fields': {'n': {'int': n}}}}}})
            self.seq += 1
        elif not (self.history_freeze and n > 1):
            self.history.append(self._record('shift', 'history', n))
        if self.standby_journals:
            self.peer_journal.append(
                {'seq': self.peer_seq, 'tick': self.tick,
                 'event': {'event_emitted': {'event': {
                     'event': 'shift', 'component': 'seq:1',
                     'fields': {'n': {'int': n}}}}}})
            self.peer_seq += 1
        if self.latest_accumulates:
            self.latest_extra.append(
                self._record('beat', 'latest', n))
        elif not (self.latest_stale and n > 1):
            self.latest[('seq:1', 'beat')] = \
                self._record('beat', 'latest', n)

    def _events(self, journal):
        # The resource view's join: the attributed journal tail beside
        # the routed history ring and the standing latest records.
        attributed = [entry for entry in journal
                      if ((entry.get('event') or {})
                          .get('event_emitted', {}).get('event') or {})
                      .get('component') == 'seq:1'
                      or (((entry.get('event') or {})
                           .get('command_settled', {})
                           .get('receipt') or {})
                          .get('command') or {})
                      .get('invoke', {}).get('component') == 'seq:1']
        return [dict(entry, retention='journal')
                for entry in attributed] \
            + list(self.history) + list(self.latest.values()) \
            + self.latest_extra

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self.tick += 1
        standby = host == 'ctrl-b:2'
        if (method, route) == ('GET', '/role'):
            if standby:
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': {'tracking': {
                                 'aligned': self.tick}}}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True}],
                'components': [dict(c) for c in self.COMPONENTS]}
        if (method, route) == ('GET', '/schema'):
            if standby and self.peer_undocumented:
                return 200, {'publication': self.tick,
                             'tick': self.tick, 'interfaces': []}
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': [
                             {'name': 'seq:1',
                              'interface': self._interface()}]}
        if (method, route) == ('GET', '/resources'):
            journal = self.peer_journal if standby else self.journal
            components = [] if (not standby and self.unserved) else [
                {'name': 'seq:1', 'kind': 'sequencer',
                 'measurements': [], 'configuration': [],
                 'state': [], 'commands': [],
                 'events': self._events(journal)}]
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'components': components}
        if (method, route) == ('GET', '/journal'):
            journal = self.peer_journal if standby else self.journal
            return 200, list(journal)
        if (method, route) == ('GET', '/history'):
            return 200, []
        if (method, route) == ('POST', '/command'):
            command = body['command']
            if standby and not self.standby_admits:
                return 200, {'command': command,
                             'outcome': {'rejected': {'reason': {
                                 'not_active': {'point': None,
                                                'role': 'standby'}}}},
                             'actor': body.get('actor')}
            receipt = {'command': command,
                       'outcome': {'applied': {'tick': self.tick}},
                       'actor': body.get('actor')}
            journal = self.peer_journal if standby else self.journal
            key = 'peer_seq' if standby else 'seq'
            journal.append({'seq': getattr(self, key),
                            'tick': self.tick,
                            'event': {'command_settled': {
                                'receipt': receipt}}})
            setattr(self, key, getattr(self, key) + 1)
            if 'invoke' in command:
                self._emit()
            return 200, receipt
        raise AssertionError('unexpected request %s %s' % (method, url))


class EventRetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RetentionFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'RETENTION_DEADLINE', 0.05):
            return scenarios.scenario_event_retention(ctx)

    def test_registered_and_replayable(self):
        self.assertIn(scenarios.scenario_event_retention,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('event-retention'),
                      scenarios.scenario_event_retention)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_two_runs_produce_identical_evidence(self):
        first = self.run_scenario()
        names = sorted(entry['ref'] for entry in first['evidence'])
        contents = {name: (self.evidence.parent / name).read_text()
                    for name in names}
        self.tmp2 = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp2.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = RetentionFeed()
        try:
            second = self.run_scenario()
            self.assertEqual(second['outcome'], 'passed', second)
            self.assertEqual(
                names,
                sorted(entry['ref'] for entry in second['evidence']))
            for name, text in contents.items():
                self.assertEqual(
                    (self.evidence.parent / name).read_text(), text,
                    name)
        finally:
            self.tmp2.cleanup()

    def test_history_event_journaled_fails(self):
        # A History-declared emission landing in the durable journal —
        # the routed record it never reached.
        self.feed.history_to_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('shift', record.get('detail', ''))
        self.assertIn('journal', record.get('detail', ''))
        report.validate_scenario(record)

    def test_history_not_growing_on_repeat_fails(self):
        self.feed.history_freeze = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not grow the event-history record',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latest_accumulating_records_fails(self):
        self.feed.latest_accumulates = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('accumulates standing latest records',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latest_stale_on_repeat_fails(self):
        self.feed.latest_stale = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stale', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_journaling_routed_events_fails(self):
        self.feed.standby_journals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('event_emitted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_admitting_the_drive_fails(self):
        self.feed.standby_admits = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('admitted the driven command',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_without_descriptor_fails(self):
        self.feed.peer_undocumented = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('publishes no descriptor',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_qualifying_component_is_inconclusive(self):
        self.feed.no_qualifier = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_declared_but_unserved_component_is_inconclusive(self):
        # The registry declares the qualifying interface but the
        # resource view never serves the instance — locating runs
        # through both endpoints, so nothing qualifies.
        self.feed.unserved = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_no_emission_path_is_inconclusive(self):
        self.feed.no_path = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_drives_without_emissions_is_inconclusive(self):
        self.feed.no_emissions = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)


class AvailabilityFeed:
    """A stubbed monitor pair for the command-availability scenario.
    ctrl-a serves the settled active; ctrl-b reports a tracking
    standby answering the same verdict rows. The served model: a
    managed-latching-alarm instance whose `shelve`/`suppress` ports
    bind non-writable bool points — the BoundPointWritable refusals
    the pump-station rig declares — a motor whose `oos` port binds the
    writable p101-oos point, a tunable parameter, and, while
    `declared` is set, a sequencer instance whose kind-declared
    `advance` command stands refused on its completed table. POST
    /command settles receipts through the same shapes the receipted
    path answers: a refused submission rejects at admission, an
    accepted one settles at the next read's scan boundary. Fault
    flags stage each named failure the issue calls out."""

    def __init__(self):
        self.tick = 0
        self.receipts = []
        self.point = False
        self.declared = True  # a kind-declared command rides the model
        # Fault injection for the named-failure cases.
        self.unnamed_refusal = False    # an available:false row lacks
                                        # its named refusal
        self.refusal_on_available = False  # an available row serves one
        self.applies_refused = False    # dispatch applies the refused
                                        # probe anyway
        self.mismatched_refusal = False  # settled reason != served text
        self.refuses_available = False  # served-available probe settles
                                        # a named refusal
        self.diverged_standby = False   # the tracking peer flips a row
        self.no_commands = False        # /resources serves no rows
        self.no_tracking = False        # ctrl-b reports no convergence

    def _components(self):
        components = [('lah-alarm:5', 'managed-latching-alarm'),
                      ('motor:22', 'motor')]
        if self.declared:
            components.append(('seq:9', 'sequencer'))
        return components

    def _commands(self, standby=False):
        commands = {
            'lah-alarm:5': [
                {'name': 'write_value:shelve', 'point': 1001,
                 'available': False,
                 'refusal': 'I/O point PointId(1001) is not declared '
                            'writable'},
                {'name': 'write_value:suppress', 'point': 331,
                 'available': False,
                 'refusal': 'I/O point PointId(331) is not declared '
                            'writable'},
                {'name': 'set_parameter:high_limit', 'available': True}],
            'motor:22': [
                {'name': 'write_value:oos', 'point': 302,
                 'available': True}],
        }
        if self.declared:
            commands['seq:9'] = [
                {'name': 'advance', 'available': False,
                 'refusal': 'the step table is complete'},
                {'name': 'reset', 'available': True}]
        if self.unnamed_refusal:
            commands['lah-alarm:5'][0] = {
                'name': 'write_value:shelve', 'point': 1001,
                'available': False}
        if self.refusal_on_available:
            commands['motor:22'][0] = dict(commands['motor:22'][0],
                                           refusal='served anyway')
        if standby and self.diverged_standby:
            commands['motor:22'][0] = {
                'name': 'write_value:oos', 'point': 302,
                'available': False,
                'refusal': 'I/O point PointId(302) is not declared '
                           'writable'}
        return commands

    def _interface(self, kind):
        if kind == 'managed-latching-alarm':
            commands = [
                {'name': 'write_value:shelve',
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'bound_point_writable',
                 'adapted': 'write_value', 'point': 1001},
                {'name': 'write_value:suppress',
                 'request': [{'name': 'value', 'kind': 'bool'}],
                 'availability': 'bound_point_writable',
                 'adapted': 'write_value', 'point': 331},
                {'name': 'set_parameter:high_limit',
                 'request': [{'name': 'value', 'kind': 'float'}],
                 'availability': 'always', 'adapted': 'set_parameter'}]
        elif kind == 'motor':
            commands = [{'name': 'write_value:oos',
                         'request': [{'name': 'value', 'kind': 'bool'}],
                         'availability': 'bound_point_writable',
                         'adapted': 'write_value', 'point': 302}]
        else:
            commands = [
                {'name': 'advance',
                 'request': [{'name': 'count', 'kind': 'int'}],
                 'availability': 'kind_declared',
                 'adapted': 'declared'},
                {'name': 'reset', 'request': [],
                 'availability': 'always', 'adapted': 'declared'}]
        return {'version': 1, 'kind': kind, 'measurements': [],
                'configuration': [], 'state': [], 'commands': commands,
                'events': []}

    def _advance(self):
        # One completed scan per measurement read: the boundary settles
        # every queued receipt whose apply_tick has arrived.
        self.tick += 1
        for receipt in self.receipts:
            accepted = receipt['outcome'].get('accepted')
            if not accepted or self.tick < accepted['apply_tick']:
                continue
            command = receipt['command']
            write = command.get('write_value')
            invoke = command.get('invoke')
            if write and write['point'] == 302:
                self.point = write['value']['bool']
                receipt['outcome'] = {'applied': {'tick': self.tick}} \
                    if not self.refuses_available \
                    else {'rejected': {'reason': {
                        'not_writable': {'point': 302}}}}
            elif invoke and invoke['command'] == 'advance' \
                    and not self.applies_refused:
                reason = 'the step table is complete'
                if self.mismatched_refusal:
                    reason = 'a different refusal'
                receipt['outcome'] = {'rejected': {'reason': {
                    'command_refused': {
                        'component': invoke['component'],
                        'command': invoke['command'],
                        'reason': reason}}}}
            else:
                receipt['outcome'] = {'applied': {'tick': self.tick}}

    def _admit(self, body):
        command = body['command']
        write = command.get('write_value')
        if write and write['point'] != 302 and not self.applies_refused:
            reason = {'not_writable': {'point': write['point']}}
            if self.mismatched_refusal:
                reason = {'unknown_point': {'point': write['point']}}
            receipt = {'command': command,
                       'outcome': {'rejected': {'reason': reason}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return receipt
        receipt = {'command': command,
                   'outcome': {'accepted': {'apply_tick': self.tick + 1}},
                   'actor': body.get('actor')}
        self.receipts.append(receipt)
        return receipt

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        self._advance()
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                sync = {'tracking': {'aligned': self.tick}} \
                    if not self.no_tracking else {'degraded': {}}
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': sync}
            if (method, route) == ('GET', '/resources'):
                return 200, {
                    'publication': self.tick, 'tick': self.tick,
                    'components': [
                        {'name': name, 'kind': kind,
                         'measurements': [], 'configuration': [],
                         'state': [],
                         'commands': [] if self.no_commands
                         else self._commands(standby=True).get(name, []),
                         'events': []}
                        for name, kind in self._components()]}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 302, 'signal': 10302, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True},
                {'point': 1001, 'signal': 11001, 'name': 'lah-shelve',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': False},
                {'point': 331, 'signal': 10331, 'name': 'lah-suppress',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': False}],
                'components': [{'name': name, 'kind': kind}
                               for name, kind in self._components()]}
        if (method, route) == ('GET', '/schema'):
            return 200, {'publication': self.tick, 'tick': self.tick,
                         'interfaces': [
                             {'name': name,
                              'interface': self._interface(kind)}
                             for name, kind in self._components()]}
        if (method, route) == ('GET', '/resources'):
            return 200, {
                'publication': self.tick, 'tick': self.tick,
                'components': [
                    {'name': name, 'kind': kind,
                     'measurements': [], 'configuration': [],
                     'state': [],
                     'commands': [] if self.no_commands
                     else self._commands().get(name, []),
                     'events': []}
                    for name, kind in self._components()]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('POST', '/command'):
            return 200, self._admit(body)
        raise AssertionError('unexpected request %s %s' % (method, url))


class CommandAvailabilityTests(unittest.TestCase):
    """scenario_command_availability against the stubbed pair: every
    fault flag stages a named acceptance failure — an inconsistent
    verdict row, a refused command that applies anyway, a settled
    refusal naming a different reason than the served row, an
    available command settling refused, a diverged tracking peer —
    and the inconclusive and coverage-limitation paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = AvailabilityFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, evidence=None):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'evidence_dir': str(evidence or self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'AVAILABILITY_DEADLINE', 0.5):
            return scenarios.scenario_command_availability(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        self.assertIn(scenarios.scenario_command_availability,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('command-availability'),
                      scenarios.scenario_command_availability)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

        # The refused probe is the BoundPointWritable write the
        # managed-alarm shelve port declares; the available probe is
        # the writable p101-oos write.
        refused = json.loads(
            (self.evidence / 'command-availability-refused.json')
            .read_text())
        self.assertEqual(refused['command'],
                         {'write_value': {'point': 1001, 'kind': 'bool',
                                          'value': {'bool': True}}})
        self.assertIn('rejected', refused['settled']['outcome'])
        applied = json.loads(
            (self.evidence / 'command-availability-available.json')
            .read_text())
        self.assertEqual(applied['command'],
                         {'write_value': {'point': 302, 'kind': 'bool',
                                          'value': {'bool': True}}})
        self.assertIn('applied', applied['settled']['outcome'])

    def test_unnamed_refusal_fails(self):
        self.feed.unnamed_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('without a named refusal',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refusal_on_available_fails(self):
        self.feed.refusal_on_available = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('available with a refusal',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_applies_refused_fails(self):
        self.feed.applies_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('settled applied anyway',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_mismatched_refusal_fails(self):
        self.feed.mismatched_refusal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('names a different reason than the served row',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refuses_available_fails(self):
        self.feed.refuses_available = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('settled a named refusal',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_diverged_standby_fails(self):
        self.feed.diverged_standby = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reports different verdicts',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_command_rows_is_inconclusive(self):
        self.feed.no_commands = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no command rows', record.get('detail', ''))
        report.validate_scenario(record)

    def test_undeclared_model_notes_the_coverage_limitation(self):
        # The pump-station rig shape: no kind-declared-availability
        # command — the limitation is recorded, the bound-point
        # verdicts still assert.
        self.feed.declared = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(any('no kind-declared-availability command'
                            in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)

    def test_no_tracking_peer_notes_the_uncovered_leg(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue(any('no tracking standby' in note
                            for note in record['observations']),
                        record['observations'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        feed2 = AvailabilityFeed()
        record2 = self.run_scenario(ctx=self._ctx(evidence2), feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)


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
                        response = self.dispatch_for(conn,
                                                     json.loads(line))
                        conn.sendall(json.dumps(response).encode()
                                     + b'\n')
        except OSError:
            pass
        finally:
            self.release_conn(conn)
            conn.close()

    def dispatch_for(self, conn, request):
        """The connection-aware request hook — claim-arbitrating
        subclasses need the attachment's identity to track claim
        holders; the base contract is per-request."""
        return self.dispatch(request)

    def release_conn(self, conn):
        """The connection's end — claim-holding subclasses drop the
        attachment's hold here; a standing claim never releases on
        disconnect."""

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

    def ctl(self, *args):
        """The ctx['plant_ctl'] seam the migrated scenarios drive: the
        argv's one wire request over a fresh connection — the same
        exchange the shipped dcs-plant-ctl runs — wrapped in the
        CompletedProcess shape the runner's `docker exec` action
        returns. A server `error` result is the tool's nonzero exit
        with the refusal on stderr."""
        request = _ctl_request(args)
        host, _, port = self.address.rpartition(':')
        try:
            with socket.create_connection((host, int(port)),
                                          timeout=5) as conn:
                conn.sendall(json.dumps(request).encode() + b'\n')
                buffer = b''
                while b'\n' not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionError('the plant closed '
                                              'mid-answer')
                    buffer += chunk
            body = json.loads(buffer.split(b'\n', 1)[0])
        except Exception as exc:
            return _ctl_process(stderr=str(exc), returncode=1)
        if (body or {}).get('result') == 'error':
            return _ctl_process(stderr=json.dumps(body.get('error')),
                                returncode=1)
        return _ctl_process(body)

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
               'plant_ctl': self.plant.ctl,
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


class ClaimPlantPeer:
    """A plant-protocol peer enforcing the single-writer field claim
    the field-claim scenario exercises: claim_writer preempts
    unconditionally, ensure_writer grants only into an unclaimed or
    same-owner claim — `claimed_shared` while other live attachments
    hold the token — release_writer drops only the caller's hold (the
    claim freed when the holder set empties, never on disconnect), and
    write/step fence every attachment outside the holder set —
    `unclaimed` while no claim stands at all. Fault flags stage each
    named failure the scenario reports."""

    def __init__(self):
        self.samples = {
            20: {'value': {'float': 1.5}, 'quality': 'good', 'tick': 0},
            40: {'value': {'bool': False}, 'quality': 'good',
                 'tick': 0},
            200: {'value': {'bool': False}, 'quality': 'good',
                  'tick': 0},
        }
        self.directions = {20: 'in', 40: 'in', 200: 'out'}
        self.plant_tick = 0
        self.claim = None         # {'owner': token, 'holders': set()}
        self.shared_conns = set()  # holders that joined via ensure
        self.next_conn = 0
        self.conn_ids = {}
        self.conns = set()
        self.requests = []
        self.lock = threading.Lock()
        # Fault injection for the named-failure cases.
        self.open_field = False           # mutations ignore the claim
        self.ensure_preempts = False      # foreign ensure grants anyway
        self.ensure_done = False          # shared grant answers `done`
        self.shared_write_fenced = False  # a holder's write is refused
        self.release_drops_claim = False  # one release frees the claim
        self.idle_release_fails = False   # a holder of nothing refused
        self.refuse_rogue = False         # claim_writer answers fenced
        self.rogue_token = scenarios.CLAIM_ROGUE
        self.listener = socket.socket()
        self.listener.setsockopt(socket.SOL_SOCKET,
                                 socket.SO_REUSEADDR, 1)
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen()
        self.address = ('127.0.0.1:'
                        + str(self.listener.getsockname()[1]))
        self.thread = threading.Thread(target=self._serve,
                                       daemon=True)
        self.thread.start()

    def ctl(self, *args):
        """The ctx['plant_ctl'] seam the scenario's field census
        drives: the argv's one wire request over a fresh connection —
        the same exchange the shipped dcs-plant-ctl runs — wrapped in
        the CompletedProcess shape the runner's `docker exec` action
        returns. A server `error` result is the tool's nonzero exit
        with the refusal on stderr."""
        request = _ctl_request(args)
        host, _, port = self.address.rpartition(':')
        try:
            with socket.create_connection((host, int(port)),
                                          timeout=5) as conn:
                conn.sendall(json.dumps(request).encode() + b'\n')
                buffer = b''
                while b'\n' not in buffer:
                    chunk = conn.recv(65536)
                    if not chunk:
                        raise ConnectionError('the plant closed '
                                              'mid-answer')
                    buffer += chunk
            body = json.loads(buffer.split(b'\n', 1)[0])
        except Exception as exc:
            return _ctl_process(stderr=str(exc), returncode=1)
        if (body or {}).get('result') == 'error':
            return _ctl_process(stderr=json.dumps(body.get('error')),
                                returncode=1)
        return _ctl_process(body)

    def _conn_id(self, conn):
        key = id(conn)
        if key not in self.conn_ids:
            self.conn_ids[key] = self.next_conn
            self.next_conn += 1
        return self.conn_ids[key]

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            self.conns.add(conn)
            threading.Thread(target=self._handle, args=(conn,),
                             daemon=True).start()

    def close(self):
        self.listener.close()
        for conn in list(self.conns):
            conn.close()

    def _fenced(self):
        return {'result': 'error',
                'error': {'kind': 'fenced',
                          'detail': 'writer claim held by another '
                                    'attachment'}}

    def _handle(self, conn):
        try:
            buf = b''
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
                while b'\n' in buf:
                    line, _, buf = buf.partition(b'\n')
                    request = json.loads(line)
                    response = self._respond(conn, request)
                    conn.sendall(
                        json.dumps(response).encode() + b'\n')
        except OSError:
            pass
        finally:
            # Disconnect releases nothing: the claim outlives a dead
            # owner so the field fails closed until another claim.
            self.conn_ids.pop(id(conn), None)
            self.conns.discard(conn)
            conn.close()

    def _respond(self, conn, request):
        with self.lock:
            self.requests.append(request.get('op'))
            cid = self._conn_id(conn)
            op = request.get('op')
            if op == 'list_points':
                points = [
                    {'point': p, 'direction': self.directions[p],
                     'path': 'field.' + str(p),
                     'type': 'scalar', 'index': i,
                     'sample': dict(self.samples[p])}
                    for i, p in enumerate(sorted(self.samples))
                ]
                return {'result': 'points', 'points': points}
            if op == 'read':
                return {'result': 'sample',
                        'sample': dict(
                            self.samples[request['point']])}
            if op == 'claim_writer':
                owner = request['owner']
                if self.refuse_rogue and owner == self.rogue_token:
                    return self._fenced()
                self.claim = {'owner': owner, 'holders': {cid}}
                self.shared_conns = set()
                return {'result': 'done'}
            if op == 'ensure_writer':
                owner = request['owner']
                if self.claim is None:
                    self.claim = {'owner': owner, 'holders': {cid}}
                    self.shared_conns = set()
                    return {'result': 'done'}
                if owner != self.claim['owner']:
                    if self.ensure_preempts:
                        self.claim = {'owner': owner,
                                      'holders': {cid}}
                        self.shared_conns = set()
                        return {'result': 'done'}
                    return self._fenced()
                self.claim['holders'].add(cid)
                self.shared_conns.add(cid)
                if self.ensure_done:
                    return {'result': 'done'}
                return {'result': 'claimed_shared',
                        'owner': owner}
            if op == 'release_writer':
                if self.claim is not None \
                        and cid in self.claim['holders']:
                    self.claim['holders'].discard(cid)
                    self.shared_conns.discard(cid)
                    if self.release_drops_claim \
                            or not self.claim['holders']:
                        self.claim = None
                        self.shared_conns = set()
                    return {'result': 'done'}
                if self.idle_release_fails:
                    return {'result': 'error',
                            'error': {'kind': 'invalid_request',
                                      'detail': 'no hold'}}
                return {'result': 'done'}
            if op == 'write':
                if not self.open_field:
                    if self.claim is None:
                        return {'result': 'error',
                                'error': {'kind': 'unclaimed',
                                          'detail': 'no writer '
                                                    'claim stands'}}
                    if cid not in self.claim['holders'] \
                            or (self.shared_write_fenced
                                and cid in self.shared_conns):
                        return {'result': 'error',
                                'error': {
                                    'kind': 'io',
                                    'error': {'fenced':
                                              self.claim['owner']}}}
                self.samples[request['point']]['value'] \
                    = request['value']
                return {'result': 'done'}
            if op == 'step':
                if not self.open_field:
                    if self.claim is None:
                        return {'result': 'error',
                                'error': {'kind': 'unclaimed',
                                          'detail': 'no writer '
                                                    'claim stands'}}
                    if cid not in self.claim['holders']:
                        return self._fenced()
                self.plant_tick += 1
                return {'result': 'stepped',
                        'tick': self.plant_tick}
            return {'result': 'error',
                    'error': {'kind': 'invalid_request',
                              'detail': 'unknown op'}}


class FieldClaimFeed:
    """A stubbed monitor pair for the field-claim scenario: ctrl-a the
    launched active holding the plant's claim under TOKEN_A through
    its own sim-net attachment, ctrl-b a tracking standby. Each
    ctrl-a request is one scan boundary: an active scan writes the
    field through the held claim, a fenced answer drives the
    contract's degrade path — field_claim_lost journaled, demoting
    then standby — a standby reconverges over a fixed count of scans,
    and POST /promote re-claims under the pinned token. Fault flags
    stage each named failure the scenario reports."""

    TOKEN_A = 0xD5C00A
    TOKEN_B = 0xD5C00B

    def __init__(self, plant, unclaimed=False):
        self.plant = plant
        self.tick = 0
        self.role = 'active'
        self.reconverge = 0
        self.journal = []
        self.next_seq = 1
        self.failed_writes = 0
        self.stream = self._connect()
        self.holds = False
        self.dead = False
        self.peer_up = False           # ctrl-b promoted and stayed
        # Fault injection for the named-failure cases.
        self.no_journal = False        # the loss demotes unrecorded
        self.dies_on_fence = False     # the preempted owner exits
        self.no_demote = False         # the loss never moves the role
        self.never_converges = False   # the demoted peer re-promotes
        self.never_reclaims = False    # the promotion re-takes nothing
        self.peer_promotes = False     # ctrl-b reports active later
        self.stalls = False            # the active's tick never grows
        if not unclaimed:
            self._roundtrip({'op': 'claim_writer',
                             'owner': self.TOKEN_A})
            self.holds = True

    def close(self):
        self.stream.close()

    def _connect(self):
        host, _, port = self.plant.address.rpartition(':')
        return socket.create_connection((host, int(port)),
                                        timeout=5)

    def _roundtrip(self, request):
        self.stream.sendall(json.dumps(request).encode() + b'\n')
        line = b''
        while not line.endswith(b'\n'):
            line += self.stream.recv(65536)
        return json.loads(line)

    def _fenced(self, response):
        error = (response or {}).get('error') or {}
        inner = error.get('error')
        return error.get('kind') == 'fenced' or (
            error.get('kind') == 'io' and isinstance(inner, dict)
            and 'fenced' in inner)

    def _journal(self, event):
        self.journal.append({'seq': self.next_seq,
                             'tick': self.tick, 'event': event})
        self.next_seq += 1

    def _supersede(self):
        # The contract's degrade path: count the fenced write,
        # journal the loss, and walk demoting -> standby.
        self.failed_writes += 1
        self.holds = False
        self.peer_up = True
        if not self.no_journal:
            self._journal({'field_claim_lost': {'point': 200}})
        if self.dies_on_fence:
            self.dead = True
            return
        if not self.no_demote:
            self.role = 'demoting'
            self._journal({'role_changed': {'from': 'active',
                                            'to': 'demoting'}})

    def _scan(self):
        self.tick += 1
        if self.role == 'demoting':
            self.role = 'standby'
            self._journal({'role_changed': {'from': 'demoting',
                                            'to': 'standby'}})
            self.reconverge = 2
            return
        if self.role == 'standby':
            if self.reconverge:
                self.reconverge -= 1
            return
        # A field-owning role writes every scan; the gate follows the
        # role, so a promoted peer that re-claimed nothing meets the
        # standing claim's fence and supersedes again.
        if self._fenced(self._roundtrip(
                {'op': 'write', 'point': 200,
                 'value': {'bool': False}})):
            self._supersede()
            return
        if self.role == 'promoting':
            self.role = 'active'
            self._journal({'role_changed': {'from': 'promoting',
                                            'to': 'active'}})

    def _refuse(self, url):
        raise urllib.error.HTTPError(url, 409, 'conflict', None,
                                     None)

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if host == 'ctrl-b:2':
            if (method, route) == ('GET', '/role'):
                role = 'active' if (self.peer_promotes
                                    and self.peer_up) else 'standby'
                report = {'role': role, 'tick': self.tick}
                if role == 'standby':
                    report['sync'] = {
                        'tracking': {'aligned': self.tick}}
                return 200, report
            if (method, route) == ('GET', '/checkpoint'):
                return 200, {'tick': self.tick}
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if self.dead:
            raise urllib.error.URLError('connection refused')
        if not self.stalls:
            self._scan()
        if (method, route) == ('GET', '/role'):
            return 200, {'role': self.role, 'tick': self.tick}
        if (method, route) == ('GET', '/snapshot'):
            points = [{'point': p,
                       'sample': dict(self.plant.samples[p])}
                      for p in sorted(self.plant.samples)]
            return 200, {'tick': self.tick, 'points': points,
                         'io_health': {
                             'failed_reads': 0,
                             'failed_writes': self.failed_writes,
                             'consecutive_failures': 0,
                             'last_error': None,
                             'driver': {'link': 'connected'}}}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/promote'):
            if self.role != 'standby' or self.reconverge \
                    or self.never_converges:
                self._refuse(url)
            if not self.never_reclaims:
                self._roundtrip({'op': 'claim_writer',
                                 'owner': self.TOKEN_A})
                self.holds = True
            self.role = 'promoting'
            self._journal({'role_changed': {'from': 'standby',
                                            'to': 'promoting'}})
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class ForcePeer:
    """The tracking half of the force-release pair: ctrl-b's monitor.
    Each of its scans adopts the checkpoint line — the active's force
    set and held image — so its own snapshot serves the same
    substituted stamp while a force stands and the same recovered
    state once the release crosses. Fault flags stage the
    standby-side named failures the issue calls out."""

    def __init__(self, line):
        self.line = line          # the active's published state
        self.tick = 0
        self.force = None
        self.oos = {'value': {'bool': False}, 'quality': 'good',
                    'tick': 0}
        # Fault injection for the standby-side named failures.
        self.never_tracks = False      # sync never reports tracking
        self.unreachable = False       # the endpoint never answers
        self.adopted_tainted = False   # the adopted image stays Substituted

    def http_json(self, method, url, body=None, timeout=10):
        if self.unreachable:
            raise urllib.error.URLError('connection refused')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self.tick += 1
        if not self.never_tracks:
            # The adopted checkpoint: the force set and the held image
            # cross together — stamp included.
            self.force = self.line.force
            self.oos = dict(self.line._oos_sample())
            if self.adopted_tainted and self.line.released_once:
                self.oos['quality'] = {'uncertain': 'substituted'}
        if (method, route) == ('GET', '/role'):
            sync = {'unsynchronized': {}} if self.never_tracks \
                else {'tracking': {'aligned': self.tick}}
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': sync}
        if (method, route) == ('GET', '/snapshot'):
            forces = [] if self.force is None \
                else [{'point': 302, 'value': {'bool': self.force}}]
            oos = self.oos
            ok = {'value': {'bool': not oos['value']['bool']},
                  'quality': oos['quality'], 'tick': self.tick}
            return 200, {'tick': self.tick, 'forces': forces,
                         'points': [
                             {'point': 302, 'direction': 'in',
                              'sample': oos},
                             {'point': 308, 'direction': 'out',
                              'sample': ok}]}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class FieldClaimTests(unittest.TestCase):
    """The field-claim scenario under fakes: the plant enforces the
    single-writer claim — fenced/claimed_shared/unclaimed/done
    answers — and the stubbed pair walks the superseded owner's
    degrade path and the restore promotion."""

    def _ctx(self, plant, evidence):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant_owner': {
                    'active': FieldClaimFeed.TOKEN_A,
                    'standby': FieldClaimFeed.TOKEN_B},
                'plant': plant.address, 'plant_ctl': plant.ctl,
                'evidence_dir': evidence}

    def _run(self, evidence, plant_flags=None, feed_flags=None,
             unclaimed=False):
        plant = ClaimPlantPeer()
        for name, value in (plant_flags or {}).items():
            setattr(plant, name, value)
        feed = FieldClaimFeed(plant, unclaimed=unclaimed)
        for name, value in (feed_flags or {}).items():
            setattr(feed, name, value)
        try:
            with patch.object(scenarios, 'http_json', feed.http_json), \
                    patch.object(scenarios, 'CLAIM_DEADLINE', 2):
                record = scenarios.scenario_field_claim(
                    self._ctx(plant, evidence))
        finally:
            feed.close()
            plant.close()
        return plant, feed, record

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_field_claim, order)
        # The restored pre-switch window — behind the stale-freshness
        # case that leaves the launch pair it shares, ahead of the
        # tune case's a->b switch.
        self.assertLess(
            order.index(scenarios.scenario_stale_freshness),
            order.index(scenarios.scenario_field_claim))
        self.assertLess(
            order.index(scenarios.scenario_field_claim),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('field-claim'),
                      scenarios.scenario_field_claim)

    def test_passed(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant, feed, record = self._run(evidence)
            self.assertEqual(record['outcome'], 'passed')
            self.assertTrue(report.validate_scenario(record))
            for name in ('field-claim-probes.json',
                         'field-claim-owner.json',
                         'field-claim-lifecycle.json',
                         'field-claim-rogue.json',
                         'field-claim-superseded.json',
                         'field-claim-promote.json',
                         'field-claim-restored.json'):
                path = os.path.join(evidence, name)
                self.assertTrue(os.path.exists(path), name)
                json.loads(Path(path).read_text())
            # The restore re-claimed under the owner's token; the
            # scenario's finally released its attachment's hold.
            self.assertEqual(plant.claim,
                             {'owner': feed.TOKEN_A,
                              'holders': {0}})

    def test_fails_when_the_field_is_open(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'open_field': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('enforceable writer claim',
                          record.get('detail', ''))

    def test_fails_when_unclaimed(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(evidence, unclaimed=True)
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('enforceable writer claim',
                          record.get('detail', ''))

    def test_fails_when_ensure_preempts(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'ensure_preempts': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('conditional grant preempted',
                          record.get('detail', ''))

    def test_fails_when_shared_answers_done(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'ensure_done': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('claimed_shared',
                          record.get('detail', ''))

    def test_fails_when_the_shared_write_is_fenced(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, plant_flags={'shared_write_fenced': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('write under the shared claim',
                          record.get('detail', ''))

    def test_fails_when_release_drops_the_claim(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence,
                plant_flags={'release_drops_claim': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('release', record.get('detail', ''))

    def test_fails_when_idle_release_is_refused(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence,
                plant_flags={'idle_release_fails': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('holder of nothing',
                          record.get('detail', ''))

    def test_refused_rogue_claim_still_passes(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant, feed, record = self._run(
                evidence, plant_flags={'refuse_rogue': True})
            self.assertEqual(record['outcome'], 'passed')
            self.assertTrue(report.validate_scenario(record))
            # The refused claim left the owner's claim standing.
            self.assertEqual(plant.claim['owner'], feed.TOKEN_A)

    def test_fails_when_the_rogue_kills_the_active(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'dies_on_fence': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('killed', record.get('detail', ''))

    def test_fails_when_the_preemption_is_silent(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'no_journal': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('silent', record.get('detail', ''))

    def test_fails_when_the_owner_never_demotes(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'no_demote': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('never demoted',
                          record.get('detail', ''))

    def test_fails_when_the_peer_never_repomotes(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'never_converges': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('never re-promoted',
                          record.get('detail', ''))

    def test_fails_when_the_promotion_reclaims_nothing(self):
        # A promotion that never re-takes the claim meets the standing
        # claim's fence on its next write and supersedes again — the
        # pair never settles back onto the field owner.
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'never_reclaims': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('never settled active',
                          record.get('detail', ''))

    def test_fails_when_the_peer_ends_active(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'peer_promotes': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('return to standby',
                          record.get('detail', ''))

    def test_fails_when_the_active_stalls(self):
        with tempfile.TemporaryDirectory() as evidence:
            _, _, record = self._run(
                evidence, feed_flags={'stalls': True})
            self.assertEqual(record['outcome'], 'failed')
            self.assertIn('stalled', record.get('detail', ''))

    def test_inconclusive_without_a_plant(self):
        plant = ClaimPlantPeer()
        feed = FieldClaimFeed(plant)
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())
            ctx['plant'] = None
            with patch.object(scenarios, 'http_json', feed.http_json):
                record = scenarios.scenario_field_claim(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
        finally:
            feed.close()
            plant.close()

    def test_inconclusive_without_a_token(self):
        plant = ClaimPlantPeer()
        feed = FieldClaimFeed(plant)
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())
            ctx['plant_owner'] = {}
            with patch.object(scenarios, 'http_json', feed.http_json):
                record = scenarios.scenario_field_claim(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
        finally:
            feed.close()
            plant.close()

    def test_inconclusive_when_the_pair_is_down(self):
        plant = ClaimPlantPeer()
        try:
            ctx = self._ctx(plant, tempfile.mkdtemp())

            def down(method, url, body=None, timeout=10):
                raise urllib.error.URLError('connection refused')
            with patch.object(scenarios, 'http_json', down), \
                    patch.object(scenarios, 'CLAIM_DEADLINE', 2):
                record = scenarios.scenario_field_claim(ctx)
            self.assertEqual(record['outcome'], 'inconclusive')
        finally:
            plant.close()

    def test_fails_when_nothing_is_settled(self):
        plant = ClaimPlantPeer()
        feed = FieldClaimFeed(plant)
        feed.role = 'standby'
        try:
            with tempfile.TemporaryDirectory() as evidence:
                with patch.object(scenarios, 'http_json',
                                  feed.http_json), \
                        patch.object(scenarios, 'CLAIM_DEADLINE', 2):
                    record = scenarios.scenario_field_claim(
                        self._ctx(plant, evidence))
                self.assertEqual(record['outcome'], 'failed')
                self.assertIn('role=active',
                              record.get('detail', ''))
        finally:
            feed.close()
            plant.close()

    def test_detaches_and_restores_rig_state(self):
        with tempfile.TemporaryDirectory() as evidence:
            plant, feed, record = self._run(evidence)
            self.assertEqual(record['outcome'], 'passed')
            # The scenario's attachment released every hold it took:
            # only the owner's own attachment holds the claim.
            self.assertEqual(plant.claim['holders'], {0})
            # And the pair returned to its original layout.
            self.assertEqual(feed.role, 'active')

    def test_identical_evidence_across_runs(self):
        runs = []
        for _ in range(2):
            with tempfile.TemporaryDirectory() as evidence:
                _, _, record = self._run(evidence)
                runs.append((
                    record['outcome'], record['observations'],
                    [(item['kind'], item['ref'],
                      item.get('detail'))
                     for item in record['evidence']],
                    {name: Path(os.path.join(evidence, name))
                     .read_text()
                     for name in sorted(os.listdir(evidence))}))
        self.assertEqual(runs[0], runs[1])


class BackupHealthFeed:
    """A stubbed monitor pair for the backup-health scenario: a tiny
    executor over the failover-select's backup leg and the wired
    managed bool-latching alarm, against a real FakePlantPeer. Every
    `http_json` call is one completed scan: the link lands the health
    output's last image on the alarm's `in` carrier, the selector
    drives backup_unhealthy off the plant-served backup quality, and
    the latching alarm stands on `in`, holds unacknowledged on its
    edge latch, and ack-dominates — so the carrier delay means the
    full annunciation lands a scan behind the injected fault.
    Declared-journaled points record point_changed; the carrier does
    not. Fault flags stage each named failure the issue calls out."""

    PRIMARY, BACKUP, SELECTED = 10, 11, 200
    BACKUP_ACTIVE = 216
    UNHEALTHY, CARRIER = 222, 223
    ACK, ALARM, UNACK = 1060, 1063, 1064
    JOURNALED = (216, 222, 1063, 1064)

    SIGNALS = [
        {'point': 10, 'signal': 10010, 'name': 'level-primary',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 11, 'signal': 10011, 'name': 'level-backup',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 200, 'signal': 10200, 'name': 'level-selected',
         'direction': 'out', 'value_type': 'float', 'writable': False},
        {'point': 216, 'signal': 10216, 'name': 'backup-active',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 222, 'signal': 10222, 'name': 'backup-unhealthy',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 223, 'signal': 10223, 'name': 'backup-unhealthy-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 1060, 'signal': 11060, 'name': 'backup-unhealthy-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1063, 'signal': 11063, 'name': 'backup-unhealthy-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1064, 'signal': 11064,
         'name': 'backup-unhealthy-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []
        self.ack = False
        self.state = False      # the alarm's tracked `in`
        self.latched = False    # the unacknowledged latch
        self.ever_faulted = False
        self.values = {216: False, 222: False, 223: False,
                       1063: False, 1064: False}
        self._role_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False          # ctrl-a never reports active
        self.no_tracking = False        # the peer never reports tracking
        self.bare_signals = False       # the annunciation wiring absent
        self.mute_unhealthy = False     # backup_unhealthy never asserts
        self.mute_alarm = False         # the alarm never stands
        self.mute_latch = False         # unacknowledged never latches
        self.selects_backup = False     # the selection moves anyway
        self.role_moves = False         # the fault reads as peer loss
        self.journals_role = False      # a role_changed entry lands
        self.no_journal = False         # transitions never journal
        self.ack_rejected = False       # the ack submission is refused
        self.ack_never_applies = False  # the accepted ack never settles
        self.ack_never_journals = False # the settled ack never journals
        self.ack_unattributed = False   # the settlement loses its actor
        self.never_recovers = False     # the clear never returns

    def _entry(self, event):
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})
        self.seq += 1

    def _drive(self, point, value):
        previous = self.values[point]
        self.values[point] = value
        if previous != value and point in self.JOURNALED \
                and not self.no_journal:
            self._entry({'point_changed': {
                'point': point, 'from': {'bool': previous},
                'to': {'bool': value}}})

    def _backup_sample(self):
        sample = dict(self.plant.samples.get(self.BACKUP) or
                      {'value': {'float': 0.0}, 'quality': 'good',
                       'tick': 0})
        fault = self.plant.faults.get(self.BACKUP)
        if isinstance(fault, dict) and 'quality' in fault:
            sample['quality'] = fault['quality']
        if self.never_recovers and self.ever_faulted:
            sample['quality'] = {'bad': 'device_fault'}
        return sample

    # One completed scan: the boundary settles queued commands, then
    # the link, the selector, and the alarm step in order.
    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            if self.ack_never_applies:
                self.pending.append(receipt)
                continue
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            if write['point'] == self.ACK:
                self.ack = write['value']['bool']
            if not self.ack_never_journals:
                body = receipt
                if self.ack_unattributed:
                    body = {'command': receipt['command'],
                            'outcome': receipt['outcome']}
                self._entry({'command_settled': {'receipt': body}})
        # The link's carrier: `in` reads the health output's last
        # image — a one-scan lag, as the wired scan order implies.
        self.values[self.CARRIER] = self.values[self.UNHEALTHY]
        quality = self._backup_sample().get('quality')
        degraded = quality != 'good'
        if degraded:
            self.ever_faulted = True
        self._drive(self.UNHEALTHY,
                    degraded and not self.mute_unhealthy)
        self._drive(self.BACKUP_ACTIVE,
                    bool(self.selects_backup and degraded))
        condition = self.values[self.CARRIER]
        fresh = condition and not self.state
        self.state = condition
        self.latched = (self.latched or fresh) and not self.ack
        self._drive(self.ALARM, condition and not self.mute_alarm)
        self._drive(self.UNACK, self.latched and not self.mute_latch)
        if degraded and self.journals_role and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._scan()
        if (method, route) == ('GET', '/role'):
            if host == 'ctrl-b:2':
                sync = 'unsynchronized' if self.no_tracking \
                    else {'tracking': {'aligned': self.tick}}
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': sync}
            if self.no_active \
                    or (self.role_moves and self.values[self.UNHEALTHY]):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            if self.bare_signals:
                return 200, {'points': [
                    {'point': 10, 'signal': 10010, 'name':
                     'level-primary', 'direction': 'in',
                     'value_type': 'float', 'writable': False}]}
            return 200, {'points': list(self.SIGNALS)}
        if (method, route) == ('GET', '/snapshot'):
            backup = self._backup_sample()
            selected = dict(self.plant.served(self.PRIMARY)['value'])
            if self.values[self.BACKUP_ACTIVE]:
                selected = dict(backup['value'])
            points = [
                {'point': self.PRIMARY, 'direction': 'in',
                 'sample': dict(self.plant.served(self.PRIMARY),
                                tick=self.tick)},
                {'point': self.BACKUP, 'direction': 'in',
                 'sample': dict(backup, tick=self.tick)},
                {'point': self.SELECTED, 'direction': 'out',
                 'sample': {'value': selected, 'quality': 'good',
                            'tick': self.tick}},
                {'point': self.ACK, 'direction': 'in',
                 'sample': {'value': {'bool': self.ack},
                            'quality': 'good', 'tick': self.tick}}]
            for point in (self.BACKUP_ACTIVE, self.UNHEALTHY,
                          self.CARRIER, self.ALARM, self.UNACK):
                points.append({
                    'point': point,
                    'direction': 'in' if point == self.CARRIER
                    else 'out',
                    'sample': {'value': {'bool': self.values[point]},
                               'quality': 'good', 'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value')
            if write and write.get('point') == self.ACK:
                if self.ack_rejected:
                    return 200, {'command': body['command'],
                                 'outcome': {'rejected': {'reason': {
                                     'queue_full': {
                                         'point': write['point'],
                                         'capacity': 4}}}},
                                 'actor': body.get('actor')}
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
                return 200, receipt
            return 200, {'command': (body or {}).get('command'),
                         'outcome': {'rejected': {'reason': {
                             'not_writable': {'point':
                                              (write or {})
                                              .get('point')}}}},
                         'actor': (body or {}).get('actor')}
        raise AssertionError('unexpected request %s %s' % (method, url))


class BackupHealthTests(unittest.TestCase):
    """scenario_backup_health against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    each annunciation assertion, the no-transition invariant, the
    acknowledgment leg, and the inconclusive paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = FakePlantPeer()
        self.plant.samples = {
            10: {'value': {'float': 1.5}, 'quality': 'good', 'tick': 0},
            11: {'value': {'float': 1.4}, 'quality': 'good', 'tick': 0}}
        self.feed = BackupHealthFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'BACKUP_HEALTH_DEADLINE', 2.0):
            return scenarios.scenario_backup_health(ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The same restored pre-switch window as the force case, ahead
        # of the tune case's a->b switch — the settled tracking pair.
        # The source-failover leg — the complementary primary-faulted
        # half — shares the window directly behind, then lag-staging.
        self.assertEqual(
            order.index(scenarios.scenario_force_carryover) + 1,
            order.index(scenarios.scenario_backup_health))
        self.assertEqual(
            order.index(scenarios.scenario_backup_health) + 1,
            order.index(scenarios.scenario_source_failover))
        self.assertIs(verify.case_function('backup-health'),
                      scenarios.scenario_backup_health)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: one injection and its clear,
        # and the run left no fault standing.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertEqual(ops.count('inject_fault'), 1)
        self.assertEqual(ops.count('clear_fault'), 1)
        self.assertEqual(self.plant.faults, {})
        # The ack point was written true then restored false through
        # the receipted path.
        self.assertFalse(self.feed.ack)
        self.assertFalse(self.feed.latched)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = FakePlantPeer()
        self.addCleanup(plant2.close)
        plant2.samples = dict(self.plant.samples)
        feed2 = BackupHealthFeed(plant2)
        ctx2 = self._ctx()
        ctx2['plant'] = plant2.address
        ctx2['plant_ctl'] = plant2.ctl
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx=ctx2, feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_unhealthy_never_asserting_fails(self):
        self.feed.mute_unhealthy = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('backup_unhealthy never asserted',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_standing_fails(self):
        self.feed.mute_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the wired alarm never stood',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unacknowledged_never_latching_fails(self):
        self.feed.mute_latch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never latched unacknowledged',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_selection_moving_to_the_backup_fails(self):
        # The no-failover invariant: backup_active asserts and the
        # served selection leaves the primary under a backup-only
        # fault.
        self.feed.selects_backup = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('backup_active asserted', detail)
        self.assertIn('selection left the primary', detail)
        report.validate_scenario(record)

    def test_role_move_under_backup_fault_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved the active role', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_fails(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role_changed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_transitions_never_journaling_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never recorded point_changed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_refusal_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack write was refused', record.get('detail', ''))
        report.validate_scenario(record)
        # The refused write never stood — nothing to restore.
        self.assertFalse(self.feed.ack)

    def test_ack_never_applying_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never cleared the unacknowledged latch',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_settlement_never_journaling_fails(self):
        self.feed.ack_never_journals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('CommandSettled never journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unattributed_ack_fails(self):
        self.feed.ack_unattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unattributed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_recovery_never_landing_fails(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never returned after the clear',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_tracking_pair_is_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_wiring_is_inconclusive(self):
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('annunciation wiring', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_point_unserved_is_inconclusive(self):
        # The field's own census does not list the backup point — the
        # scenario may not fault what the plant does not serve.
        self.plant.samples.pop(11)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not a field in-point',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        # A pair mid-transition — no endpoint reports settled active —
        # is the rig's own failure, not an unreachable one.
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)


class SourceFailoverFeed:
    """A stubbed monitor pair for the source-failover scenario: a tiny
    executor over the failover-select, the wired managed bool-latching
    alarm on its backup_active flag, and the threshold chain on the
    selected level, against a real FakePlantPeer. Every `http_json`
    call is one completed scan: the internal carriers deliver last
    tick's image one scan later (the chain's level input and the
    alarm's `in`), the selector serves the backup sample verbatim once
    the primary's served quality degrades, the chain advances its
    held demand on the failover-fed level under the declared
    setpoints, and the alarm stands on `in`, latches unacknowledged on
    the edge, and releases it while ack reads true. Declared-journaled
    points record point_changed; the carriers do not. Fault flags
    stage each named failure the issue calls out."""

    PRIMARY, BACKUP, SELECTED, CHAIN_IN = 10, 11, 200, 201
    DEMAND, DUTY_CALL, LAG_CALL = 204, 212, 213
    BELOW_CUTOFF, HIGH_LEVEL = 214, 215
    BACKUP_ACTIVE, CARRIER, UNHEALTHY = 216, 219, 222
    ACK, ALARM, UNACK = 1020, 1023, 1024
    JOURNALED = (214, 215, 216, 222, 1023, 1024)
    INS = (201, 219, 1020)
    CUTOFF, STOP, START, LAG_START, HIGH = 0.5, 1.0, 2.0, 3.0, 4.0
    ON_BAD = 0

    SIGNALS = [
        {'point': 10, 'signal': 10010, 'name': 'level-primary',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 11, 'signal': 10011, 'name': 'level-backup',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 200, 'signal': 10200, 'name': 'level-selected',
         'direction': 'out', 'value_type': 'float', 'writable': False},
        {'point': 201, 'signal': 10201, 'name': 'level-chain',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 204, 'signal': 10204, 'name': 'demand',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 212, 'signal': 10212, 'name': 'duty-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 213, 'signal': 10213, 'name': 'lag-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 214, 'signal': 10214, 'name': 'below-cutoff',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 215, 'signal': 10215, 'name': 'high-level',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 216, 'signal': 10216, 'name': 'backup-active',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 219, 'signal': 10219, 'name': 'backup-active-in',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 222, 'signal': 10222, 'name': 'backup-unhealthy',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1020, 'signal': 11020, 'name': 'backup-active-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1023, 'signal': 11023, 'name': 'backup-active-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1024, 'signal': 11024,
         'name': 'backup-active-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []
        self.ack = False
        self.state = False      # the alarm's tracked `in`
        self.latched = False    # the unacknowledged latch
        self.ever_faulted = False
        # The well sits above `start`: demand 1 is the honest
        # evaluation, so a stalled or fallback answer reads as a
        # divergence the scenario can name.
        self.demand = 1
        self.selected = {'value': {'float': 2.5}, 'quality': 'good',
                         'tick': 0}
        self.chain_in = dict(self.selected)
        self.frozen_demand_tick = None
        self.values = {212: True, 213: False, 214: False, 215: False,
                       216: False, 219: False, 222: False,
                       1023: False, 1024: False}
        self._role_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False          # ctrl-a never reports active
        self.bare_schema = False        # no failover-select declared
        self.bare_signals = False       # the signal index is a stub
        self.no_alarm_wired = False     # no managed alarm on the flag
        self.no_chain = False           # no threshold-chain declared
        self.shared_source = False      # primary and backup coincide
        self.bad_ack_signal = False     # the ack point is unwritable
        self.mute_active = False        # backup_active never asserts
        self.tracks_degraded = False    # out keeps the degraded source
        self.holds_last_known = False   # out freezes the last image
        self.mute_alarm = False         # the alarm never stands
        self.mute_latch = False         # unacknowledged never latches
        self.stall_demand = False       # demand reads the fallback
                                        # under a healthy level
        self.freeze_demand = False      # demand's sample tick stalls
        self.unlatches_on_clear = False # the latch drops with the
                                        # condition — the lifecycle
                                        # changed by the excursion
        self.role_moves = False         # the fault reads as peer loss
        self.journals_role = False      # a role_changed entry lands
        self.no_journal = False         # transitions never journal
        self.ack_rejected = False       # the ack submission is refused
        self.ack_never_applies = False  # the accepted ack never settles
        self.ack_never_journals = False # the settled ack never journals
        self.ack_unattributed = False   # the settlement loses its actor
        self.never_recovers = False     # the clear never re-selects

    def _entry(self, event):
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})
        self.seq += 1

    def _drive(self, point, value):
        previous = self.values[point]
        self.values[point] = value
        if previous != value and point in self.JOURNALED \
                and not self.no_journal:
            self._entry({'point_changed': {
                'point': point, 'from': {'bool': previous},
                'to': {'bool': value}}})

    def _source_sample(self, point):
        sample = dict(self.plant.samples.get(point) or
                      {'value': {'float': 0.0}, 'quality': 'good',
                       'tick': 0})
        fault = self.plant.faults.get(point)
        if isinstance(fault, dict) and 'quality' in fault:
            sample['quality'] = fault['quality']
        if self.never_recovers and point == self.PRIMARY \
                and self.ever_faulted:
            sample['quality'] = {'bad': 'device_fault'}
        return sample

    def _chain_advance(self, held, level):
        if level <= self.STOP:
            return 0
        if held == 2 and level <= self.START:
            return 1
        if level >= self.LAG_START:
            return 2
        if held == 0 and level >= self.START:
            return 1
        return held

    # One completed scan: the boundary settles queued commands, then
    # the carriers, the selector, the chain, and the alarm step in the
    # wired order.
    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            if self.ack_never_applies:
                self.pending.append(receipt)
                continue
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            if write['point'] == self.ACK:
                self.ack = write['value']['bool']
            if not self.ack_never_journals and not self.no_journal:
                body = receipt
                if self.ack_unattributed:
                    body = {'command': receipt['command'],
                            'outcome': receipt['outcome']}
                self._entry({'command_settled': {'receipt': body}})
        # The carriers deliver last scan's image — the one-scan lag
        # the wired scan order implies.
        self.chain_in = dict(self.selected)
        self.values[self.CARRIER] = self.values[self.BACKUP_ACTIVE]
        # The selector: the backup serves while the primary is
        # untrusted; the fault flags stage the value-path bugs the
        # scenario names.
        primary = self._source_sample(self.PRIMARY)
        backup = self._source_sample(self.BACKUP)
        on_backup = primary['quality'] != 'good'
        if on_backup:
            self.ever_faulted = True
        if not (self.holds_last_known and on_backup):
            source = primary if self.tracks_degraded \
                else (backup if on_backup else primary)
            self.selected = dict(source, tick=self.tick)
        self._drive(self.BACKUP_ACTIVE,
                    on_backup and not self.mute_active)
        self._drive(self.UNHEALTHY, backup['quality'] != 'good')
        # The chain evaluates the failover-fed image: a trusted level
        # advances the held demand, an untrusted one falls back to the
        # declared on_bad_demand — the fault flags stall or freeze the
        # evaluation outright.
        level = self.chain_in['value'].get('float', 0.0)
        trusted = self.chain_in['quality'] == 'good'
        if self.freeze_demand and on_backup:
            if self.frozen_demand_tick is None:
                self.frozen_demand_tick = self.tick
        elif self.stall_demand and on_backup:
            self.demand = self.ON_BAD
        elif not trusted:
            self.demand = self.ON_BAD
        else:
            self.demand = self._chain_advance(self.demand, level)
        self._drive(self.DUTY_CALL, self.demand >= 1)
        self._drive(self.LAG_CALL, self.demand >= 2)
        self._drive(self.BELOW_CUTOFF,
                    bool(trusted and level <= self.CUTOFF))
        self._drive(self.HIGH_LEVEL,
                    bool(trusted and level >= self.HIGH))
        # The wired managed alarm: `in` is the flag's carrier, the
        # latch holds until ack reads true.
        condition = self.values[self.CARRIER]
        fresh = condition and not self.state
        self.state = condition
        self.latched = (self.latched or fresh) and not self.ack
        if self.unlatches_on_clear and not condition:
            self.latched = False
        self._drive(self.ALARM, condition and not self.mute_alarm)
        self._drive(self.UNACK, self.latched and not self.mute_latch)
        if on_backup and self.journals_role \
                and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})

    @staticmethod
    def _interface(kind, measurements=(), state=()):
        return {'kind': kind,
                'measurements': [{'name': name, 'point': point}
                                 for name, point in measurements],
                'state': [{'name': name, 'point': point}
                          for name, point in state],
                'configuration': [], 'commands': [], 'events': []}

    def _schema(self):
        if self.bare_schema:
            return {'interfaces': [
                {'name': 'chain', 'interface': self._interface(
                    'threshold-chain')}]}
        select = [('primary', 10), ('backup', 11), ('out', 200)]
        if self.shared_source:
            select = [('primary', 10), ('backup', 10), ('out', 200)]
        interfaces = [
            {'name': 'select', 'interface': self._interface(
                'failover-select', select,
                [('backup_active', 216), ('backup_unhealthy', 222)])}]
        if not self.no_chain:
            interfaces.append({'name': 'chain', 'interface':
                               self._interface(
                                   'threshold-chain',
                                   [('level', 201), ('demand', 204)],
                                   [('duty_call', 212),
                                    ('lag_call', 213),
                                    ('below_cutoff', 214),
                                    ('high_level', 215)])})
        if not self.no_alarm_wired:
            interfaces.append({'name': 'ba-alarm', 'interface':
                               self._interface(
                                   'managed-bool-latching-alarm',
                                   [('in', 219)],
                                   [('ack', 1020), ('alarm', 1023),
                                    ('unacknowledged', 1024)])})
        return {'interfaces': interfaces}

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._scan()
        if (method, route) == ('GET', '/role'):
            if host == 'ctrl-b:2':
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': {'tracking': {'aligned':
                                                   self.tick}}}
            if self.no_active \
                    or (self.role_moves and
                        self._source_sample(self.PRIMARY)['quality']
                        != 'good'):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = list(self.SIGNALS)
            if self.bad_ack_signal:
                points = [dict(entry, writable=False)
                          if entry['name'] == 'backup-active-ack'
                          else entry for entry in points]
            if self.bare_signals:
                points = points[:1]
            return 200, {'points': points}
        if (method, route) == ('GET', '/schema'):
            return 200, self._schema()
        if (method, route) == ('GET', '/snapshot'):
            demand_tick = self.tick
            if self.freeze_demand \
                    and self.frozen_demand_tick is not None:
                demand_tick = self.frozen_demand_tick
            points = [
                {'point': self.PRIMARY, 'direction': 'in',
                 'sample': dict(self._source_sample(self.PRIMARY),
                                tick=self.tick)},
                {'point': self.BACKUP, 'direction': 'in',
                 'sample': dict(self._source_sample(self.BACKUP),
                                tick=self.tick)},
                {'point': self.SELECTED, 'direction': 'out',
                 'sample': dict(self.selected)},
                {'point': self.CHAIN_IN, 'direction': 'in',
                 'sample': dict(self.chain_in, tick=self.tick)},
                {'point': self.DEMAND, 'direction': 'out',
                 'sample': {'value': {'int': self.demand},
                            'quality': 'good', 'tick': demand_tick}},
                {'point': self.ACK, 'direction': 'in',
                 'sample': {'value': {'bool': self.ack},
                            'quality': 'good', 'tick': self.tick}}]
            for point in sorted(self.values):
                points.append({
                    'point': point,
                    'direction': 'in' if point in self.INS else 'out',
                    'sample': {'value': {'bool': self.values[point]},
                               'quality': 'good', 'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points,
                         'parameters': [
                             {'name': 'chain', 'values': {
                                 'cutoff': {'float': self.CUTOFF},
                                 'stop': {'float': self.STOP},
                                 'start': {'float': self.START},
                                 'lag_start': {'float': self.LAG_START},
                                 'high': {'float': self.HIGH},
                                 'on_bad_demand': {'int': self.ON_BAD}}}]}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value')
            if write and write.get('point') == self.ACK:
                if self.ack_rejected:
                    return 200, {'command': body['command'],
                                 'outcome': {'rejected': {'reason': {
                                     'not_writable': {
                                         'point': write['point']}}}},
                                 'actor': body.get('actor')}
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
                return 200, receipt
            return 200, {'command': (body or {}).get('command'),
                         'outcome': {'rejected': {'reason': {
                             'not_writable': {'point':
                                              (write or {})
                                              .get('point')}}}},
                         'actor': (body or {}).get('actor')}
        raise AssertionError('unexpected request %s %s' % (method, url))


class SourceFailoverTests(unittest.TestCase):
    """scenario_source_failover against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    the failover assertions, the alarmed transition, the demand
    continuity, the recovery re-selection, the acknowledgment leg, and
    the inconclusive paths the issue declares."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = FakePlantPeer()
        # Distinct live values on the two sources so the served
        # selection's tracking is observable: 2.5 sits above `start`,
        # holding demand at 1.
        self.plant.samples = {
            10: {'value': {'float': 2.5}, 'quality': 'good', 'tick': 0},
            11: {'value': {'float': 2.4}, 'quality': 'good', 'tick': 0}}
        self.feed = SourceFailoverFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'SOURCE_FAILOVER_DEADLINE', 2.0):
            return scenarios.scenario_source_failover(ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The same settled tracking window as the backup-health leg —
        # the complementary primary-faulted half — ahead of the
        # lag-staging leg's shared window.
        self.assertEqual(
            order.index(scenarios.scenario_backup_health) + 1,
            order.index(scenarios.scenario_source_failover))
        self.assertEqual(
            order.index(scenarios.scenario_source_failover) + 1,
            order.index(scenarios.scenario_lag_staging))
        self.assertIs(verify.case_function('source-failover'),
                      scenarios.scenario_source_failover)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: one injection and its clear,
        # and the run left no fault standing.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertEqual(ops.count('inject_fault'), 1)
        self.assertGreaterEqual(ops.count('clear_fault'), 1)
        self.assertEqual(self.plant.faults, {})
        # The ack point was written true then restored false through
        # the receipted path, and the latch released through it.
        self.assertFalse(self.feed.ack)
        self.assertFalse(self.feed.latched)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = FakePlantPeer()
        self.addCleanup(plant2.close)
        plant2.samples = dict(self.plant.samples)
        feed2 = SourceFailoverFeed(plant2)
        ctx2 = self._ctx()
        ctx2['plant'] = plant2.address
        ctx2['plant_ctl'] = plant2.ctl
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx=ctx2, feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_backup_active_never_asserting_fails(self):
        # The named failover clause: the selection moved but
        # backup_active stayed deasserted — a dead flag beside a live
        # switch.
        self.feed.mute_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('backup_active never asserted',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_selection_tracking_the_degraded_source_fails(self):
        self.feed.tracks_degraded = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking the degraded source or a frozen '
                      'last-known', record.get('detail', ''))
        report.validate_scenario(record)

    def test_frozen_last_known_fails(self):
        self.feed.holds_last_known = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking the degraded source or a frozen '
                      'last-known', record.get('detail', ''))
        report.validate_scenario(record)

    def test_alarm_never_standing_fails(self):
        self.feed.mute_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('the wired alarm never stood',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unacknowledged_never_latching_fails(self):
        self.feed.mute_latch = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never latched unacknowledged',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_stalled_demand_fails(self):
        # The chain answers the untrusted-input fallback while the
        # failover-fed level serves healthy — demand stalled.
        self.feed.stall_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demand stalled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_frozen_demand_fails(self):
        self.feed.freeze_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('demand evaluation froze',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_fails(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role_changed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_transitions_never_journaling_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never journaled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_role_move_under_primary_fault_fails(self):
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved the active role',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_never_reselecting_primary_fails(self):
        self.feed.never_recovers = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never re-selected the primary',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_latch_releasing_with_the_condition_fails(self):
        # The excursion changed the two-flag lifecycle: the
        # unacknowledged latch dropped with the alarm condition
        # instead of standing for the declared ack.
        self.feed.unlatches_on_clear = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('latch released before the declared ack',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_refusal_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack write was refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_never_applying_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never cleared the standing unacknowledged',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_settlement_never_journaling_fails(self):
        self.feed.ack_never_journals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('CommandSettled never journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unattributed_ack_fails(self):
        self.feed.ack_unattributed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unattributed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_failover_select_is_inconclusive(self):
        self.feed.bare_schema = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no failover-select instance',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_wired_alarm_is_inconclusive(self):
        self.feed.no_alarm_wired = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no managed alarm is wired',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_threshold_chain_is_inconclusive(self):
        self.feed.no_chain = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no threshold-chain', record.get('detail', ''))
        report.validate_scenario(record)

    def test_single_source_is_inconclusive(self):
        # The model declares no second source to switch between —
        # nothing the leg may prove.
        self.feed.shared_source = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('distinct pair', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.bad_ack_signal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writable bool input', record.get('detail', ''))
        report.validate_scenario(record)

    def test_bare_signals_is_inconclusive(self):
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not a served float field input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unhealthy_backup_is_inconclusive(self):
        # The field's own census shows the backup already degraded —
        # no two distinct healthy field source points to switch
        # between.
        self.plant.faults[11] = {'quality': {'bad': 'device_fault'}}
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('healthy field source points',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_point_unserved_is_inconclusive(self):
        self.plant.samples.pop(11)
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not a field in-point',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)


class FallbackFeed:
    """A stubbed monitor pair for the unavailable-fallback scenario —
    the pump-station level path the fixture wires: the failover-select
    over the plant's level-primary (10) and level-backup (11) field
    inputs serving `out` on 200, fanned out to the threshold-chain's
    `level` on 201 and driving `demand` on 204, the journaled
    backup_active (216) and backup_unhealthy (222) reports, and the
    bool-latching alarm the engagement fans out to through 219. Every
    `http_json` call is one completed scan: the derived points are
    recomputed from the plant's served samples — `out` carries the
    primary while it reads Good and finite, else the backup verbatim —
    and the journaled carriers' point_changed records land at the scan
    boundary their value transitions. Fault flags stage each named
    failure the issue calls out."""

    PRIMARY, BACKUP = 10, 11
    OUT, LEVEL, DEMAND = 200, 201, 204
    ACTIVE, ALARM_IN, UNHEALTHY = 216, 219, 222
    ALARM, UNACK = 1003, 1004
    ON_BAD = 0      # the declared on_bad_demand the parameters serve
    COMPUTED = 2    # a computed stage count distinct from ON_BAD

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.journal = []
        self.next_seq = 1
        self.prev_active = False
        self.prev_unhealthy = False
        self.phantom_journaled = False
        self.held_out = None
        self._out = {'value': {'float': 0.0}, 'quality': 'good'}
        self._demand = self.COMPUTED
        self._active = False
        self._unhealthy = False
        # Fault injection for the named-failure cases.
        self.silent_annunciation = False    # backup_unhealthy never asserts
        self.selects_on_backup_fault = False  # a backup-only fault moves the mode
        self.phantom_failover_journal = False  # a failover journals unserved
        self.silent_failover = False        # backup_active never asserts
        self.holds_last_value = False       # all-bad: out keeps its last Good
        self.controls_on_untrusted = False  # demand keeps computing on Bad level
        self.never_resumes = False          # the mode flag stays latched
        self.annunciation_sticks = False    # backup_unhealthy stays latched
        self.skips_journal = False          # transitions never reach the journal
        self.drop_unhealthy_port = False    # the descriptor drops the port
        self.backup_owns_field = False      # no healthy baseline presents

    @staticmethod
    def _float(sample):
        value = (sample or {}).get('value')
        if isinstance(value, dict):
            value = next(iter(value.values()), None)
        return value

    @classmethod
    def _ok(cls, sample):
        """The selector's trustworthy-measurement predicate: Good with
        a finite numeric value."""
        value = cls._float(sample)
        return (sample or {}).get('quality') == 'good' \
            and isinstance(value, (int, float)) and math.isfinite(value)

    def _changed(self, point, old, new):
        self.journal.append({'seq': self.next_seq, 'tick': self.tick,
                             'event': {'point_changed': {
                                 'point': point,
                                 'from': {'bool': old},
                                 'to': {'bool': new}}}})
        self.next_seq += 1

    # The scan: derived points recomputed from the plant's served
    # samples, each fault flag bending the one behavior it names.
    def _advance(self):
        self.tick += 1
        primary = self.plant.served(self.PRIMARY)
        backup = self.plant.served(self.BACKUP)
        primary_ok = self._ok(primary)
        backup_ok = self._ok(backup)
        # `out` follows the declared selection — the primary while it
        # reads Good and finite, else the backup verbatim — unless a
        # flag substitutes the served sample.
        declared = primary if primary_ok else backup
        out = declared
        if self.holds_last_value and not (primary_ok or backup_ok):
            out = self.held_out or declared
        elif self._ok(declared):
            self.held_out = dict(declared)
        active = not primary_ok
        if self.backup_owns_field \
                or (self.selects_on_backup_fault and not backup_ok) \
                or (self.never_resumes and self.prev_active):
            active = True
        if self.silent_failover:
            active = False
        unhealthy = not backup_ok
        if self.silent_annunciation:
            unhealthy = False
        if self.annunciation_sticks and self.prev_unhealthy:
            unhealthy = True
        # The point-to-point fanout delivers `out` to the chain's
        # `level` verbatim; the chain emits its declared on_bad_demand
        # while the delivered level is untrusted.
        level_ok = self._ok(out)
        self._demand = self.COMPUTED if level_ok or \
            self.controls_on_untrusted else self.ON_BAD
        self._out = {'value': out.get('value'),
                     'quality': out.get('quality')}
        self._active = active
        self._unhealthy = unhealthy
        # The journaled carriers' point_changed records land at the
        # scan boundary their served value transitions.
        if not self.skips_journal:
            if active != self.prev_active:
                self._changed(self.ACTIVE, self.prev_active, active)
            if unhealthy != self.prev_unhealthy:
                self._changed(self.UNHEALTHY, self.prev_unhealthy,
                              unhealthy)
        if self.phantom_failover_journal and primary_ok \
                and not backup_ok and not self.phantom_journaled:
            # A failover transition the telemetry never served still
            # reaches the durable record — the journaled-without-served
            # form of the backup-only leg's named failure.
            self.phantom_journaled = True
            self._changed(self.ACTIVE, False, True)
        self.prev_active = active
        self.prev_unhealthy = unhealthy

    def _port(self, name, direction, kind, point):
        return {'name': name, 'direction': direction, 'kind': kind,
                'point': point}

    def _descriptors(self):
        select_ports = [
            self._port('primary', 'in', 'float', self.PRIMARY),
            self._port('backup', 'in', 'float', self.BACKUP),
            self._port('out', 'out', 'float', self.OUT),
            self._port('backup_active', 'out', 'bool', self.ACTIVE)]
        if not self.drop_unhealthy_port:
            select_ports.append(
                self._port('backup_unhealthy', 'out', 'bool',
                           self.UNHEALTHY))
        return [
            {'name': 'level-select', 'kind': 'failover-select',
             'label': 'level-select', 'ports': select_ports,
             'parameters': []},
            {'name': 'level-chain', 'kind': 'threshold-chain',
             'label': 'level-chain',
             'ports': [self._port('level', 'in', 'float', self.LEVEL),
                       self._port('demand', 'out', 'int', self.DEMAND)],
             'parameters': [{'name': 'on_bad_demand', 'kind': 'int'}]},
            {'name': 'backup-active-alarm', 'kind': 'bool-latching-alarm',
             'label': 'backup-active-alarm',
             'ports': [self._port('in', 'in', 'bool', self.ALARM_IN),
                       self._port('alarm', 'out', 'bool', self.ALARM),
                       self._port('unacknowledged', 'out', 'bool',
                                  self.UNACK)],
             'parameters': []}]

    def http_json(self, method, url, body=None, timeout=10):
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance()
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/snapshot'):
            def served(point):
                return dict(self.plant.served(point), tick=self.tick)

            def computed(value):
                return {'value': value, 'quality': 'good',
                        'tick': self.tick}
            out = dict(self._out, tick=self.tick)
            return 200, {
                'tick': self.tick,
                'points': [
                    {'point': self.PRIMARY, 'direction': 'in',
                     'sample': served(self.PRIMARY)},
                    {'point': self.BACKUP, 'direction': 'in',
                     'sample': served(self.BACKUP)},
                    {'point': self.OUT, 'direction': 'out',
                     'sample': out},
                    {'point': self.LEVEL, 'direction': 'in',
                     'sample': dict(out)},
                    {'point': self.DEMAND, 'direction': 'out',
                     'sample': computed({'int': self._demand})},
                    {'point': self.ACTIVE, 'direction': 'out',
                     'sample': computed({'bool': self._active})},
                    {'point': self.ALARM_IN, 'direction': 'in',
                     'sample': computed({'bool': self._active})},
                    {'point': self.UNHEALTHY, 'direction': 'out',
                     'sample': computed({'bool': self._unhealthy})},
                    {'point': self.ALARM, 'direction': 'out',
                     'sample': computed({'bool': self._active})},
                    {'point': self.UNACK, 'direction': 'out',
                     'sample': computed({'bool': self._active})}],
                'descriptors': self._descriptors(),
                'parameters': [{'name': 'level-chain', 'values': {
                    'on_bad_demand': {'int': self.ON_BAD}}}]}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        raise AssertionError('unexpected request %s %s' % (method, url))


class UnavailableFallbackTests(unittest.TestCase):
    """scenario_unavailable_fallback against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named acceptance
    failure — the backup-degraded no-transition leg, the all-bad
    declared-demand leg, both ordered-recovery legs, the
    no-unintended-step assertion, and the inconclusive paths."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = FakePlantPeer()
        # The level sources the served failover-select is wired to.
        self.plant.samples = {
            FallbackFeed.PRIMARY: {'value': {'float': 1.5},
                                   'quality': 'good', 'tick': 0},
            FallbackFeed.BACKUP: {'value': {'float': 1.4},
                                  'quality': 'good', 'tick': 0}}
        self.feed = FallbackFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'FALLBACK_DEADLINE', 2.0):
            return scenarios.scenario_unavailable_fallback(
                ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertEqual(
            order.index(scenarios.scenario_unclaimed_rearm) + 1,
            order.index(scenarios.scenario_unavailable_fallback))
        self.assertIs(verify.case_function('unavailable-fallback'),
                      scenarios.scenario_unavailable_fallback)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The legs drove the documented per-point fault surface — one
        # injection and one clear per level source — and the run left
        # no fault behind on the shared field.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertEqual(ops.count('inject_fault'), 2)
        self.assertEqual(ops.count('clear_fault'), 2)
        self.assertEqual(self.plant.faults, {})

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = FakePlantPeer()
        self.addCleanup(plant2.close)
        plant2.samples = dict(self.plant.samples)
        feed2 = FallbackFeed(plant2)
        ctx2 = self._ctx()
        ctx2['plant'] = plant2.address
        ctx2['plant_ctl'] = plant2.ctl
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx=ctx2, feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_silent_annunciation_fails(self):
        # The backup-degraded leg: the standby-health report never
        # asserts though the backup is serving Bad.
        self.feed.silent_annunciation = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never asserted the backup_unhealthy',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_only_fault_moving_the_selection_fails(self):
        # The no-transition clause: a backup-only fault may not move
        # the selection off the healthy primary.
        self.feed.selects_on_backup_fault = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved the selection', record.get('detail', ''))
        report.validate_scenario(record)

    def test_backup_only_fault_journaling_a_failover_fails(self):
        # The durable-record half of the no-transition clause.
        self.feed.phantom_failover_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journaled a failover transition',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_annunciation_never_journaled_fails(self):
        self.feed.skips_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('annunciation never journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_all_bad_never_engaging_the_fallback_fails(self):
        # The all-bad leg: the mode flag never asserts, so the declared
        # fallback engagement never presents.
        self.feed.silent_failover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never engaged the declared fallback',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_held_output_under_all_bad_fails(self):
        # The no-unintended-step clause: with every source bad the
        # select holds its last Good stamp rather than serving Bad.
        self.feed.holds_last_value = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('an unintended output step',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_controlling_on_untrusted_level_fails(self):
        # The chain keeps emitting a computed stage count while its
        # delivered level is untrusted — the never-silently-controls
        # clause.
        self.feed.controls_on_untrusted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('controls on an untrusted level',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_recovered_primary_never_reselected_fails(self):
        # The first ordered-recovery leg: clearing the primary must
        # re-select it through the named transition.
        self.feed.never_resumes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never resumed control', record.get('detail', ''))
        report.validate_scenario(record)

    def test_annunciation_never_clearing_fails(self):
        # The second ordered-recovery leg: clearing the backup must
        # drop the annunciation through the named transition.
        self.feed.annunciation_sticks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never cleared the annunciation',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwired_annunciation_port_is_inconclusive(self):
        # A model without the optional standby-health port declares no
        # annunciation for the leg to check.
        self.feed.drop_unhealthy_port = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('does not serve the fallback leg',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_healthy_baseline_is_inconclusive(self):
        self.feed.backup_owns_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no healthy settled baseline',
                      record.get('detail', ''))
        report.validate_scenario(record)


class StagingPlantPeer(FakePlantPeer):
    """The lag-staging rig's plant half: FakePlantPeer plus the
    write-ownership claim (`ensure_writer`) and `write` ops the
    scenario drives through the shared-claim path. The standing owner
    is pre-seeded with a live holder so the scenario attachment answers
    `claimed_shared` — exactly what a rig whose active controller holds
    the claim replies to a second attachment on the same token."""

    def __init__(self, owner):
        super().__init__()
        # Only `inflow` needs plant-side storage: the feed's own
        # dynamics integrate it into the level it serves. The seed is
        # the dynamics document's forcing input — zero, so the well is
        # still until the scenario writes it.
        self.samples = {
            10: {'value': {'float': 0.8}, 'quality': 'good', 'tick': 0},
            11: {'value': {'float': 0.8}, 'quality': 'good', 'tick': 0},
            12: {'value': {'float': 0.0}, 'quality': 'good', 'tick': 0}}
        self.owner = owner
        self.holders = {'controller'}   # the active's standing claim
        self.writer_granted = False
        self.write_count = 0
        # Fault injection for the named-failure cases.
        self.refuse_claim = False   # ensure_writer answers fenced
        self.lying_restore = False  # the restore write reports done
                                    # but reads back a type-confused
                                    # zero — the write never took

    def dispatch(self, request):
        op = request.get('op')
        if op in ('ensure_writer', 'claim_writer'):
            self.requests.append(request)
            owner = request.get('owner')
            if self.refuse_claim or owner != self.owner:
                return {'result': 'error', 'error': {
                    'kind': 'fenced',
                    'detail': 'the field is owned by another attachment'}}
            shared = bool(self.holders)
            self.holders.add('scenario')
            self.writer_granted = True
            return {'result': 'claimed_shared' if shared else 'done',
                    'owner': owner}
        if op == 'write':
            self.requests.append(request)
            if not self.writer_granted:
                return {'result': 'error', 'error': {
                    'kind': 'unclaimed',
                    'detail': 'no writer claim stands'}}
            self.write_count += 1
            if self.lying_restore and self.write_count > 1:
                self.samples[request['point']]['value'] = {'int': 0}
            else:
                self.samples[request['point']]['value'] = \
                    request['value']
            return {'result': 'done'}
        return super().dispatch(request)


class LagStagingFeed:
    """A stubbed monitor pair for the lag-staging scenario: a tiny
    executor over the station's threshold chain, the pump group's
    delayed staging, and the managed latching alarm, against a real
    plant-protocol peer whose `inflow` point the scenario writes.
    Every `http_json` call is one completed scan — the internal
    carriers (level into the chain and the LAH, demand into the group)
    deliver the last image one scan later, the chain holds its demand
    between the hysteresis bands, the group's starts wait the declared
    start_delay_ticks, and the LAH trips at high_limit and releases on
    its hysteresis while the unacknowledged latch waits for the
    receipted ack. Declared-journaled points record point_changed —
    the first observed sample included — the carrier and staging
    points do not. Fault flags stage each named failure the issue
    calls out."""

    PRIMARY, BACKUP, SELECTED = 10, 11, 200
    CHAIN_IN, LAH_IN = 201, 202
    DEMAND, DEMAND_IN = 204, 205
    DUTY, STAGED = 210, 211
    DUTY_CALL, LAG_CALL = 212, 213
    BELOW_CUTOFF, HIGH_LEVEL = 214, 215
    ACK, ALARM, UNACK = 1000, 1003, 1004
    SHELVED, SUPPRESSED, OOS = 1005, 1006, 1007
    P1_RUN, P2_RUN = 40, 41
    P1_CMD, P2_CMD = 100, 101
    P1_MODE, P1_OOS, P2_MODE, P2_OOS = 300, 302, 332, 334
    INFLOW = 12
    JOURNALED = (40, 41, 214, 215, 300, 302, 332, 334,
                 1003, 1004, 1005, 1006, 1007)
    INS = (10, 11, 12, 201, 202, 205, 1000, 300, 302, 332, 334)
    # The declared setpoint chain, the staging delay, and the alarm
    # limits — the same table the deployed model serves.
    CUTOFF, STOP, START, LAG_START, HIGH = 0.5, 1.0, 2.0, 3.0, 4.0
    START_DELAY = 1
    HIGH_LIMIT, HYSTERESIS = 4.0, 0.1
    DRAW, DT = -1.0, 0.1    # each running pump's draw, sim dt per scan

    SIGNALS = [
        {'point': 10, 'signal': 10010, 'name': 'level-primary',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 11, 'signal': 10011, 'name': 'level-backup',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 12, 'signal': 10012, 'name': 'inflow',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 200, 'signal': 10200, 'name': 'level-selected',
         'direction': 'out', 'value_type': 'float', 'writable': False},
        {'point': 201, 'signal': 10201, 'name': 'level-chain-in',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 202, 'signal': 10202, 'name': 'level-lah-in',
         'direction': 'in', 'value_type': 'float', 'writable': False},
        {'point': 204, 'signal': 10204, 'name': 'demand',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 205, 'signal': 10205, 'name': 'demand-in',
         'direction': 'in', 'value_type': 'int', 'writable': False},
        {'point': 210, 'signal': 10210, 'name': 'duty',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 211, 'signal': 10211, 'name': 'staged',
         'direction': 'out', 'value_type': 'int', 'writable': False},
        {'point': 212, 'signal': 10212, 'name': 'duty-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 213, 'signal': 10213, 'name': 'lag-call',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 214, 'signal': 10214, 'name': 'below-cutoff',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 215, 'signal': 10215, 'name': 'high-level',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1000, 'signal': 11000, 'name': 'lah-ack',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 1003, 'signal': 11003, 'name': 'lah-alarm',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1004, 'signal': 11004, 'name': 'lah-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1005, 'signal': 11005, 'name': 'lah-shelved',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1006, 'signal': 11006, 'name': 'lah-suppressed',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 1007, 'signal': 11007, 'name': 'lah-out-of-service',
         'direction': 'out', 'value_type': 'bool', 'writable': False},
        {'point': 40, 'signal': 10040, 'name': 'p101-run',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 41, 'signal': 10041, 'name': 'p102-run',
         'direction': 'in', 'value_type': 'bool', 'writable': False},
        {'point': 300, 'signal': 10300, 'name': 'p101-mode',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 302, 'signal': 10302, 'name': 'p101-oos',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 332, 'signal': 10332, 'name': 'p102-mode',
         'direction': 'in', 'value_type': 'bool', 'writable': True},
        {'point': 334, 'signal': 10334, 'name': 'p102-oos',
         'direction': 'in', 'value_type': 'bool', 'writable': True}]

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.seq = 1
        self.journal = []
        self.pending = []          # accepted commands awaiting boundary
        self.demand_held = 0       # the chain's held stage count
        self.last_demand = 0       # the group's last Good demand
        self.cmd = [False, False]  # commanded pumps, 0-based
        self.last_start = None     # tick the last start issued
        self.pending_start = {}    # pump -> first target tick
        self.duty_index = 0        # pump 1 holds duty
        self.alarm_state = 'clear'
        self.latched = False
        self.level = 0.8
        self.values = {
            200: 0.8, 201: 0.8, 202: 0.8,
            204: 0, 205: 0, 210: 0, 211: 0,
            212: False, 213: False, 214: False, 215: False,
            1000: False, 1003: False, 1004: False, 1005: False,
            1006: False, 1007: False,
            40: False, 41: False, 100: False, 101: False,
            300: False, 302: False, 332: False, 334: False}
        self.jseen = {}            # last journaled value per point
        self.history = {}          # point -> [{'seq','sample'}]
        self.hseq = {}
        self._role_journaled = False
        self._demand_journaled = False
        # Fault injection for the named-failure cases.
        self.no_active = False         # ctrl-a never reports active
        self.no_tracking = False       # the peer never reports tracking
        self.bare_signals = False      # the staging wiring absent
        self.bad_ack_signal = False    # lah-ack served non-writable
        self.bare_schema = False       # schema lacks the group/alarm
        self.bad_setpoints = False     # served chain not increasing
        self.bad_high_limit = False    # lah high_limit != chain high
        self.mute_chain = False        # demand never advances
        self.missing_demand = False    # 204 absent from the snapshot
        self.missing_ack = False       # 1000 absent from the snapshot
        self.slow_staging = False      # starts land beyond the delay
        self.mute_alarm = False        # the alarm never stands
        self.mute_unack = False        # the latch never latches
        self.sticky_alarm = False      # alarm clears past its order
        self.role_moves = False        # the level drive reads as peer loss
        self.journals_role = False     # a role_changed entry lands
        self.journals_demand = False   # a non-journaled point journals
        self.no_journal = False        # transitions never journal
        self.ack_rejected = False      # the ack submission is refused
        self.ack_never_applies = False # the accepted ack never settles
        self.moves_pump_state = False  # p1_oos flips mid-leg

    @staticmethod
    def _wrap(value):
        if isinstance(value, bool):
            return {'bool': value}
        if isinstance(value, int):
            return {'int': value}
        return {'float': value}

    def _entry(self, event):
        self.journal.append({'seq': self.seq, 'tick': self.tick,
                             'event': event})
        self.seq += 1

    def _journal_values(self):
        """The recorder's per-scan diff over declared-journaled points:
        the first observed sample lands `from: null` like the real
        recorder's creation record."""
        for point in self.JOURNALED:
            value = self.values[point]
            previous = self.jseen.get(point)
            if point in self.jseen and previous == value:
                continue
            self.jseen[point] = value
            if not self.no_journal:
                self._entry({'point_changed': {
                    'point': point,
                    'from': (None if point not in self.jseen
                             or previous is None
                             else self._wrap(previous)),
                    'to': self._wrap(value)}})

    # The pump group's step on the delivered demand: duty first then
    # rotation order, each fresh start gated on start_delay_ticks since
    # the last one, the tail de-staging first on falling demand.
    def _step_group(self):
        demand_eff = max(0, min(2, int(self.values[self.DEMAND_IN])))
        if self.last_demand >= 1 and demand_eff == 0:
            self.duty_index = (self.duty_index + 1) % 2  # alternate
        self.last_demand = demand_eff
        order = [self.duty_index, (self.duty_index + 1) % 2]
        targets = order[:demand_eff]
        for index in targets:
            if self.cmd[index]:
                continue
            self.pending_start.setdefault(index, self.tick)
            if self.slow_staging:
                delay = self.START_DELAY + 3
                allowed = self.tick >= \
                    self.pending_start[index] + delay
            else:
                allowed = self.last_start is None \
                    or self.tick >= self.last_start + self.START_DELAY
            if allowed:
                self.cmd[index] = True
                self.last_start = self.tick
                self.pending_start.pop(index, None)
        for index in range(2):
            if index not in targets:
                self.cmd[index] = False
                self.pending_start.pop(index, None)
        staged = int(sum(self.cmd))
        for point, value in ((self.STAGED, staged),
                             (self.DUTY, self.duty_index + 1),
                             (self.P1_CMD, self.cmd[0]),
                             (self.P2_CMD, self.cmd[1]),
                             (self.P1_RUN, self.cmd[0]),
                             (self.P2_RUN, self.cmd[1])):
            self.values[point] = value

    # The LAH's two-flag lifecycle on the delivered level: trips at
    # high_limit, holds through the hysteresis, and the unacknowledged
    # latch stands until the receipted ack dominates.
    def _step_alarm(self):
        pv = self.values[self.LAH_IN]
        previous = self.alarm_state
        if self.sticky_alarm:
            state = 'high' if pv >= self.HIGH_LIMIT \
                or (previous == 'high' and pv >= self.STOP) else 'clear'
        elif previous == 'high':
            state = 'high' \
                if pv >= self.HIGH_LIMIT - self.HYSTERESIS else 'clear'
        else:
            state = 'high' if pv >= self.HIGH_LIMIT else 'clear'
        self.alarm_state = state
        fresh = state == 'high' and previous != 'high'
        self.latched = (self.latched or fresh) \
            and not self.values[self.ACK]
        self.values[self.ALARM] = state == 'high' \
            and not self.mute_alarm
        self.values[self.UNACK] = self.latched and not self.mute_unack

    # One completed scan: boundary-settled commands first, then the
    # carriers' one-scan delivery, the components in order, the journaled
    # diffs, the per-point history append, and the plant's dynamics step.
    def _scan(self):
        self.tick += 1
        pending, self.pending = self.pending, []
        for receipt in pending:
            if self.ack_never_applies:
                self.pending.append(receipt)
                continue
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            if write['point'] == self.ACK:
                self.values[self.ACK] = write['value']['bool']
            if not self.no_journal:
                self._entry({'command_settled': {'receipt':
                                                 dict(receipt)}})
        # The internal carriers deliver last tick's image.
        self.values[self.CHAIN_IN] = self.values[self.SELECTED]
        self.values[self.LAH_IN] = self.values[self.SELECTED]
        self.values[self.DEMAND_IN] = self.values[self.DEMAND]
        # failover-select serves the primary; the backup never engages.
        self.values[self.SELECTED] = self.level
        level = self.values[self.CHAIN_IN]
        if not self.mute_chain:
            if level <= self.STOP:
                self.demand_held = 0
            elif self.demand_held == 2 and level <= self.START:
                self.demand_held = 1
            elif level >= self.LAG_START:
                self.demand_held = 2
            elif self.demand_held == 0 and level >= self.START:
                self.demand_held = 1
        demand = self.demand_held
        for point, value in ((self.DEMAND, demand),
                             (self.DUTY_CALL, demand >= 1),
                             (self.LAG_CALL, demand >= 2),
                             (self.BELOW_CUTOFF, level <= self.CUTOFF),
                             (self.HIGH_LEVEL, level >= self.HIGH)):
            self.values[point] = value
        self._step_group()
        self._step_alarm()
        self._journal_values()
        # Injected journal-contract violations.
        if self.journals_role and self.values[self.ALARM] \
                and not self._role_journaled:
            self._role_journaled = True
            self._entry({'role_changed': {'from': 'active',
                                          'to': 'standby'}})
        if self.journals_demand and demand >= 1 \
                and not self._demand_journaled:
            self._demand_journaled = True
            self._entry({'point_changed': {
                'point': self.DEMAND, 'from': {'int': 0},
                'to': {'int': demand}}})
        # Operator state the leg never drove must not move.
        if self.moves_pump_state and demand >= 2:
            self.values[self.P1_OOS] = True
        for point, value in self.values.items():
            self.hseq[point] = self.hseq.get(point, 0) + 1
            self.history.setdefault(point, []).append({
                'seq': self.hseq[point],
                'sample': {'value': self._wrap(value),
                           'quality': 'good', 'tick': self.tick}})
        # The plant's dynamics step: inflow plus each commanded pump's
        # draw integrates into the level the next scan serves. A
        # type-confused held value reads as still water.
        inflow = self.plant.samples[self.INFLOW]['value'] \
            .get('float', 0.0)
        self.level += (inflow + self.DRAW * sum(self.cmd)) * self.DT

    def _parameters(self):
        if self.bad_setpoints:
            table = {'cutoff': 0.5, 'stop': 1.0, 'start': 3.0,
                     'lag_start': 2.0, 'high': 4.0}
        else:
            table = {'cutoff': self.CUTOFF, 'stop': self.STOP,
                     'start': self.START, 'lag_start': self.LAG_START,
                     'high': self.HIGH}
        high_limit = 4.5 if self.bad_high_limit else self.HIGH_LIMIT
        return [
            {'name': 'select', 'values': {}},
            {'name': 'chain', 'values': dict(
                {key: {'float': value}
                 for key, value in table.items()},
                on_bad_demand={'int': 0})},
            {'name': 'group', 'values': {
                'rotation': {'int': 0},
                'start_delay_ticks': {'int': self.START_DELAY},
                'restage_delay_ticks': {'int': 0},
                'min_off_ticks': {'int': 2}}},
            {'name': 'lah', 'values': {
                'high_limit': {'float': high_limit},
                'hysteresis': {'float': self.HYSTERESIS},
                'low_limit': {'float': -1000000000.0}}},
            {'name': 'lal', 'values': {
                'high_limit': {'float': 1000000000.0},
                'hysteresis': {'float': self.HYSTERESIS},
                'low_limit': {'float': 0.5}}}]

    @staticmethod
    def _interface(kind, measurements=(), state=()):
        return {'kind': kind,
                'measurements': [{'name': name, 'point': point}
                                 for name, point in measurements],
                'state': [{'name': name, 'point': point}
                          for name, point in state],
                'configuration': [], 'commands': [], 'events': []}

    def _schema(self):
        if self.bare_schema:
            return {'interfaces': [
                {'name': 'chain',
                 'interface': self._interface('threshold-chain')}]}
        return {'interfaces': [
            {'name': 'select', 'interface': self._interface(
                'failover-select', [('out', 200)])},
            {'name': 'chain', 'interface': self._interface(
                'threshold-chain', [('level', 201), ('demand', 204)],
                [('duty_call', 212), ('lag_call', 213),
                 ('below_cutoff', 214), ('high_level', 215)])},
            {'name': 'group', 'interface': self._interface(
                'pump-group', [('demand', 205), ('staged', 211)],
                [('duty', 210)])},
            {'name': 'lah', 'interface': self._interface(
                'managed-latching-alarm', [('in', 202)],
                [('alarm', 1003), ('unacknowledged', 1004),
                 ('shelved', 1005), ('suppressed', 1006),
                 ('out_of_service', 1007)])},
            {'name': 'lal', 'interface': self._interface(
                'managed-latching-alarm', [('in', 203)],
                [('alarm', 1013)])}]}

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._scan()
        if (method, route) == ('GET', '/role'):
            if host == 'ctrl-b:2':
                sync = 'unsynchronized' if self.no_tracking \
                    else {'tracking': {'aligned': self.tick}}
                return 200, {'role': 'standby', 'tick': self.tick,
                             'sync': sync}
            if self.no_active \
                    or (self.role_moves and self.values[self.ALARM]):
                return 200, {'role': 'standby', 'tick': self.tick}
            return 200, {'role': 'active', 'tick': self.tick}
        if (method, route) == ('GET', '/signals'):
            points = list(self.SIGNALS)
            if self.bad_ack_signal:
                points = [dict(entry, writable=False)
                          if entry['name'] == 'lah-ack' else entry
                          for entry in points]
            if self.bare_signals:
                points = points[:1]
            return 200, {'points': points}
        if (method, route) == ('GET', '/schema'):
            return 200, self._schema()
        if (method, route) == ('GET', '/snapshot'):
            points = [
                {'point': self.PRIMARY, 'direction': 'in',
                 'sample': {'value': {'float': self.level},
                            'quality': 'good', 'tick': self.tick}},
                {'point': self.BACKUP, 'direction': 'in',
                 'sample': {'value': {'float': self.level},
                            'quality': 'good', 'tick': self.tick}}]
            for point, value in sorted(self.values.items()):
                if self.missing_demand and point == self.DEMAND:
                    continue
                if self.missing_ack and point == self.ACK:
                    continue
                points.append({
                    'point': point,
                    'direction': 'in' if point in self.INS else 'out',
                    'sample': {'value': self._wrap(value),
                               'quality': 'good', 'tick': self.tick}})
            return 200, {'tick': self.tick, 'points': points,
                         'parameters': self._parameters()}
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1])
            return 200, [entry for entry in self.journal
                         if entry['seq'] > since]
        if (method, route) == ('GET', '/history'):
            params = {}
            for pair in query.split('&'):
                key, _, val = pair.partition('=')
                params.setdefault(key, []).append(val)
            since = int(params.get('since', ['0'])[0])
            wanted = [int(point) for point in params.get('point', [])]
            if not wanted:
                wanted = sorted(self.history)
            return 200, [
                {'point': point,
                 'samples': [sample for sample in
                             self.history.get(point, [])
                             if sample['seq'] > since]}
                for point in wanted]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value')
            if write and write.get('point') == self.ACK:
                if self.ack_rejected:
                    return 200, {'command': body['command'],
                                 'outcome': {'rejected': {'reason': {
                                     'not_writable': {
                                         'point': write['point']}}}},
                                 'actor': body.get('actor')}
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
                return 200, receipt
            return 200, {'command': (body or {}).get('command'),
                         'outcome': {'rejected': {'reason': {
                             'not_writable': {'point':
                                              (write or {})
                                              .get('point')}}}},
                         'actor': (body or {}).get('actor')}
        raise AssertionError('unexpected request %s %s' % (method, url))


class LagStagingTests(unittest.TestCase):
    """scenario_lag_staging against the stubbed rig: the feed's
    transitions are call-count keyed so each run emits identical
    evidence, and every fault flag stages a named acceptance failure —
    each staging leg, the delay bound, the annunciation lifecycle, the
    journal contract, the falling-edge order, the restore, and the
    inconclusive paths."""

    OWNER = 424243

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = StagingPlantPeer(self.OWNER)
        self.feed = LagStagingFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'plant_owner': {'active': self.OWNER, 'standby': 424244},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'LAG_STAGING_POLL', 0.001), \
                patch.object(scenarios, 'LAG_STAGING_DEADLINE', 3.0):
            return scenarios.scenario_lag_staging(ctx or self._ctx())

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The same settled tracking window as the backup-health and
        # source-failover legs, ahead of the tune case and the
        # failover switch.
        self.assertEqual(
            order.index(scenarios.scenario_source_failover) + 1,
            order.index(scenarios.scenario_lag_staging))
        self.assertEqual(
            order.index(scenarios.scenario_lag_staging) + 1,
            order.index(scenarios.scenario_standby_loss))
        # The standby-loss and demote-settle legs share the same
        # restored window and still run ahead of the tune case's a->b
        # switch.
        self.assertEqual(
            order.index(scenarios.scenario_standby_loss) + 1,
            order.index(scenarios.scenario_demote_settle_uniqueness))
        self.assertEqual(
            order.index(scenarios.scenario_demote_settle_uniqueness)
            + 1,
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('lag-staging'),
                      scenarios.scenario_lag_staging)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # The documented request surface: the shared-claim attachment,
        # one read for the baseline, the rise write and its restore.
        ops = [request.get('op') for request in self.plant.requests]
        self.assertIn('list_points', ops)
        self.assertIn('ensure_writer', ops)
        self.assertIn('read', ops)
        self.assertEqual(ops.count('write'), 2)
        # The driven input restored and the roles never moved.
        self.assertEqual(
            self.plant.samples[12]['value'], {'float': 0.0})
        self.assertFalse(self.feed.latched)
        self.assertFalse(self.feed.values[self.feed.ACK])
        self.assertEqual(self.feed.demand_held, 0)
        self.assertEqual(sum(self.feed.cmd), 0)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        plant2 = StagingPlantPeer(self.OWNER)
        self.addCleanup(plant2.close)
        feed2 = LagStagingFeed(plant2)
        ctx2 = self._ctx()
        ctx2['plant'] = plant2.address
        ctx2['plant_ctl'] = plant2.ctl
        ctx2['evidence_dir'] = str(evidence2)
        record2 = self.run_scenario(ctx=ctx2, feed=feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_demand_never_staging_fails(self):
        # The chain never advances: the start crossing's served output
        # never lands.
        self.feed.mute_chain = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('staging-failed', record.get('detail', ''))
        self.assertIn('duty-call leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_staging_beyond_delay_bound_fails(self):
        # The group answers the consumed demand later than
        # start_delay_ticks — the named nondeterminism the bound exists
        # to catch.
        self.feed.slow_staging = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('beyond start_delay_ticks', detail)
        report.validate_scenario(record)

    def test_alarm_never_standing_fails(self):
        self.feed.mute_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('annunciated leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unacknowledged_never_latching_fails(self):
        self.feed.mute_unack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('annunciated leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_out_of_order_release_fails(self):
        # The alarm holding past the lag's de-stage point breaks the
        # declared falling-edge order.
        self.feed.sticky_alarm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('did not release in order', detail)
        report.validate_scenario(record)

    def test_transitions_never_journaling_fails(self):
        self.feed.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never recorded the declared point_changed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_journaled_role_change_fails(self):
        self.feed.journals_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('role_changed', detail)
        report.validate_scenario(record)

    def test_nonjournaled_point_journaling_fails(self):
        self.feed.journals_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        detail = record.get('detail', '')
        self.assertIn('staging-nondeterministic', detail)
        self.assertIn('non-journaled point', detail)
        report.validate_scenario(record)

    def test_role_move_under_drive_fails(self):
        # A process drive that reads as peer loss — the scenario's
        # role-stability check must catch it.
        self.feed.role_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('active role moved', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_refusal_fails(self):
        self.feed.ack_rejected = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ack write was refused', record.get('detail', ''))
        report.validate_scenario(record)

    def test_ack_never_applying_fails(self):
        self.feed.ack_never_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('acknowledged leg never landed',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unrestored_inflow_fails(self):
        # The restore write reports done but the field reads back a
        # type-confused value — the restore audit must catch it.
        self.plant.lying_restore = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not restore', record.get('detail', ''))
        report.validate_scenario(record)

    def test_moved_pump_operator_state_fails(self):
        self.feed.moves_pump_state = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('moved pump operator state',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_active_is_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_tracking_pair_is_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_wiring_is_inconclusive(self):
        self.feed.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('staging wiring', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unwritable_ack_is_inconclusive(self):
        self.feed.bad_ack_signal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writable bool ack input',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_schema_instances_is_inconclusive(self):
        self.feed.bare_schema = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('lacks the threshold-chain',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unordered_setpoints_is_inconclusive(self):
        self.feed.bad_setpoints = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('not strictly increasing',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_mismatched_high_limit_is_inconclusive(self):
        self.feed.bad_high_limit = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('high_limit is not the chain',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_writer_claim_is_inconclusive(self):
        self.plant.refuse_claim = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('writer claim refused',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_plant_endpoint_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant endpoint', record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_owner_token_is_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant_owner']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no plant-writer owner token',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_demand_never_reporting_is_inconclusive(self):
        # The chain's own output never reports a sample — the
        # baseline's settled check can never trust it.
        self.feed.missing_demand = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('settled low-level baseline',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_output_never_reporting_is_inconclusive(self):
        # A leg whose awaited output is absent from the served snapshot
        # is inconclusive, not a staging failure — the scenario's own
        # never-reported split.
        self.feed.missing_ack = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('outputs never reported',
                      record.get('detail', ''))
        report.validate_scenario(record)


class ForceFeed:
    """A stubbed monitor pair for the force-release scenario: ctrl-a
    owns the field and ctrl-b tracks it over one checkpoint line, a
    tiny internal-point executor on the rig's writable p101-oos point
    and its inverted p101-oos-ok carrier. Every call on the
    measurement channel is one completed scan — reads observe,
    commands queue for the next scan boundary and journal as they
    settle — mirroring the held-value/force substitution semantics
    the executor documents for an internal `In` point: the release
    boundary keeps the force's last stamp as the held image,
    re-stamped Good. Fault flags stage each named failure the issue
    calls out."""

    def __init__(self):
        self.tick = 0
        self.held = False       # p101-oos's held operator value
        self.force = None       # the forced value while a force stands
        self.released_once = False  # an unforce applied at a boundary
        self.receipts = []
        self.journal = []
        self.next_seq = 1
        self.peer = ForcePeer(self)
        # Fault injection for the named-failure cases.
        self.force_unseen = False     # telemetry never shows the force
        self.forces_omitted = False   # the forces list stays empty
        self.control_ignores = False  # oos-ok never follows the force
        self.never_settled = False    # commands apply but never settle
        self.wrong_actor = False      # settled receipts lose attribution
        self.release_sticks = False   # unforce never clears the force
        self.release_refused = False  # unforce is rejected at submission
        self.no_recovery = False      # the point never reads Good again
        self.cone_tainted = False     # oos-ok keeps the Substituted stamp
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
                    # The release boundary: the held-value rule resumes
                    # on the force's last stamp — the held image keeps
                    # the forced value, re-stamped Good.
                    if self.force is not None:
                        self.held = self.force
                    self.force = None
                    self.released_once = True
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
    # the scenario watches follow the force. The cone_tainted flag
    # holds the propagated Substituted stamp past the release — the
    # cone that never untaints.
    def _oos_ok_sample(self, oos):
        observed = oos['value']['bool']
        driven = not self.held if self.control_ignores else not observed
        quality = {'uncertain': 'substituted'} \
            if self.cone_tainted and self.released_once \
            else oos['quality']
        return {'value': {'bool': driven}, 'quality': quality,
                'tick': self.tick}

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        if host == 'ctrl-b':
            return self.peer.http_json(method, url, body, timeout)
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
        # Finding #498's regression shape: the badge clears but the
        # released sample keeps its Substituted stamp — the leg must
        # catch it before the restore write could paper it over.
        self.feed.no_recovery = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('did not recover', record.get('detail', ''))
        self.assertIn('Good quality', record.get('detail', ''))
        self.assertIn('substituted', record.get('detail', ''))
        # The restamp write never ran — the failure was observed on the
        # release itself.
        self.assertFalse(any('write_value' in receipt['command']
                             for receipt in self.feed.receipts))
        report.validate_scenario(record)

    def test_released_cone_staying_tainted_fails(self):
        # The point re-stamps Good but the inverted carrier keeps the
        # propagated Substituted mark — the cone-untaint half of the
        # same observation fails.
        self.feed.cone_tainted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('cone untainted', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_snapshot_staying_substituted_fails(self):
        # The active recovers but the tracking standby's adopted
        # snapshot keeps the Substituted stamp — the parity leg names
        # the tainted peer.
        self.feed.peer.adopted_tainted = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tracking standby', record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_never_tracking_is_inconclusive(self):
        self.feed.peer.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking convergence',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_standby_unreachable_is_inconclusive(self):
        self.feed.peer.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_two_runs_produce_identical_records(self):
        first = self.run_scenario()
        feed, self.feed = self.feed, ForceFeed()
        try:
            second = self.run_scenario()
        finally:
            self.feed = feed
        self.assertEqual(first, second)

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
        self.never_retracks_on = set()      # the demoted peer stays unsynchronized

    def _advance(self, peer):
        """One completed scan on `peer`: pending role transitions
        settle, the tracking peer pulls the line's force set, and each
        accepted command whose apply_tick has arrived applies and
        journals its settlement."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            # the demoted peer follows its successor — unless the
            # never_retracks_on fault holds it unsynchronized
            peer.tracking = peer.name not in self.never_retracks_on
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

    def test_demoted_peer_never_retracking_fails(self):
        # The original active re-activates but the demoted successor
        # stays unsynchronized — the launch role pair is not restored.
        self.pair.never_retracks_on.add('b')
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


class LatencyFeed:
    """A stubbed rig for the dead-peer-latency scenario. ctrl-a owns
    the field; ctrl-b is the tracking standby whose every monitor read
    is one paced scan cycle — while the owner is stopped each read is
    another failed pull, reported as the degraded sync the pair view
    names. ctrl-d is the run's driven third peer: never paced, so it
    reports 'unsynchronized' until a POST /scan batch runs its
    per-scan pulls — each pull waits pull_wait seconds while the batch
    occupies its worker, lands 'degraded' while the source is dead,
    and 'tracking' once the source answers again. Every transition is
    call-count keyed — never wall-clock — so two scenario runs emit
    identical evidence; the pull waits are the only sleeps, staging
    the dead peer's fetch wait and the batch's hold on its worker.
    Fault flags stage each named failure the issue calls out."""

    POINT = 210

    def __init__(self):
        self.owner_up = True
        self.driven_up = False
        self.misses = 0
        self.saw_isolation = False
        self.pulls = 0
        self.driven_sync = 'unsynchronized'
        self.batches = []          # the scans counts posted to ctrl-d
        self.pull_wait = 0.05      # each pull's wait inside a batch
        self.in_flight = 0         # batches currently on their worker
        self.in_flight_reads = 0   # driven reads served mid-batch
        self.tick = 30
        self.stops = []
        self.starts = []
        self.launches = []
        self.removes = []
        self.commands = 0
        # Fault flags stage each named failure the issue calls out.
        self.pulls_refuse = False      # the dead peer refuses, not hangs
        self.slow_survivor = False     # survivor reads exceed the bound
        self.slow_driven = False       # driven reads starve mid-batch
        self.batch_refused = False     # the mid-window batch answers 500
        self.never_unsynced = False    # ctrl-d reports converged at launch
        self.never_reconverge = False  # ctrl-b stays degraded
        self.queue_full = False        # the command is refused at admission
        self.stop_fails = False
        self.start_fails = False
        self.launch_fails = False
        self.teardown_fails = False
        self.driven_never_serves = False

    # --- the runner-owned actions the ctx carries ---

    def stop(self, name):
        if self.stop_fails:
            raise RuntimeError('docker stop failed')
        self.stops.append(name)
        self.owner_up = False

    def start(self, name):
        if self.start_fails:
            raise RuntimeError('docker start failed')
        self.starts.append(name)
        self.owner_up = True

    def launch(self, name):
        if self.launch_fails:
            raise RuntimeError('docker run failed')
        self.launches.append(name)
        if not self.driven_never_serves:
            self.driven_up = True
        return {'container': 'dcs-hw-qa-1-d'}

    def teardown(self):
        if self.teardown_fails:
            raise RuntimeError('docker rm failed')
        self.removes.append('d')
        self.driven_up = False

    # --- the stubbed monitor surface ---

    def http_json(self, method, url, body=None, timeout=5):
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a'):
            return self.owner(method, path, body)
        if host.startswith('ctrl-b'):
            return self.survivor(method, path, body)
        if host.startswith('ctrl-d'):
            return self.driven(method, path, body)
        raise urllib.error.URLError('unknown host ' + host)

    def owner(self, method, path, body):
        if not self.owner_up:
            raise urllib.error.URLError('connection refused')
        if path == '/role':
            return 200, {'role': 'active', 'tick': self.tick}
        raise urllib.error.URLError('no route ' + path)

    def survivor(self, method, path, body):
        if self.slow_survivor:
            time.sleep(0.5)   # past the patched bound: starvation
        # one paced cycle: the pull the loop runs before its scan —
        # a failed pull is the degraded sync the pair view names
        if self.owner_up:
            self.misses = 0
        else:
            self.misses += 1
            self.saw_isolation = True
        self.tick += 1
        if path == '/role':
            if self.misses == 0 and not (self.never_reconverge
                                         and self.saw_isolation):
                sync = {'tracking': {'aligned': self.tick}}
            else:
                sync = {'degraded': {'detail': 'fetch failed: refused'}}
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': sync}
        if path == '/signals':
            return 200, {'points': [
                {'name': 'p101-oos', 'point': self.POINT,
                 'kind': 'bool', 'writable': True,
                 'direction': 'in', 'value_type': 'bool'}]}
        if path == '/snapshot':
            return 200, {'tick': self.tick}
        if path.startswith('/journal'):
            return 200, [{'seq': self.commands, 'tick': self.tick}]
        if path == '/command' and method == 'POST':
            self.commands += 1
            if self.queue_full:
                reason = {'queue_full': {}}
            else:
                reason = {'not_active': {}}
            return 200, {'id': 'r' + str(self.commands),
                         'outcome': {'rejected': {'reason': reason}}}
        raise urllib.error.URLError('no route ' + path)

    def driven(self, method, path, body):
        if not self.driven_up:
            raise urllib.error.URLError('connection refused')
        if self.in_flight:
            if self.slow_driven:
                time.sleep(0.5)   # the batch starves the endpoint
            else:
                self.in_flight_reads += 1
        if path == '/role':
            sync = {'tracking': {'aligned': self.tick}} \
                if self.never_unsynced else self.driven_sync
            return 200, {'role': 'standby', 'tick': self.tick,
                         'sync': sync}
        if path == '/snapshot':
            return 200, {'tick': self.tick}
        if path.startswith('/journal'):
            return 200, [{'seq': self.pulls}]
        if path == '/scan' and method == 'POST':
            scans = int((body or {}).get('scans') or 0)
            self.batches.append(scans)
            if self.batch_refused and not self.owner_up:
                return 500, {'error': 'no worker free'}
            self.in_flight += 1
            try:
                for _ in range(scans):
                    self.pulls += 1
                    if self.pull_wait:
                        time.sleep(self.pull_wait)
                    if self.owner_up:
                        self.driven_sync = {'tracking':
                                            {'aligned': self.tick}}
                    elif self.pulls_refuse:
                        self.driven_sync = {'degraded':
                                            {'detail': 'refused'}}
                    else:
                        self.driven_sync = {'degraded':
                                            {'detail': 'fetch timed '
                                             'out'}}
            finally:
                self.in_flight -= 1
            return 200, {'scanned': scans, 'tick': self.tick}
        raise urllib.error.URLError('no route ' + path)


class DeadPeerLatencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = LatencyFeed()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'driven': 'http://ctrl-d:3',
               'evidence_dir': str(self.evidence),
               'stop_controller': feed.stop,
               'start_controller': feed.start,
               'start_driven': feed.launch,
               'stop_driven': feed.teardown}
        ctx.update(extra)
        return ctx

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'LATENCY_BOUND', 0.3), \
                patch.object(scenarios, 'LATENCY_POLL', 0.001), \
                patch.object(scenarios, 'LATENCY_WINDOW', 2.0), \
                patch.object(scenarios, 'LATENCY_GRACE', 0.0), \
                patch.object(scenarios, 'LATENCY_SERVE_DEADLINE', 0.5), \
                patch.object(scenarios, 'LATENCY_SETTLE_DEADLINE', 1.0), \
                patch.object(scenarios, 'LATENCY_HEALTHY_SCANS', 6):
            return scenarios.scenario_dead_peer_latency(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_dead_peer_latency, order)
        # The same restored pre-switch window as the force case —
        # ahead of the tune case's a->b switch.
        self.assertLess(
            order.index(scenarios.scenario_stale_freshness),
            order.index(scenarios.scenario_dead_peer_latency))
        self.assertLess(
            order.index(scenarios.scenario_dead_peer_latency),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('dead-peer-latency'),
                      scenarios.scenario_dead_peer_latency)

    def test_dead_peer_window_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertEqual(self.feed.stops, ['active'])
        self.assertEqual(self.feed.starts, ['active'])
        self.assertEqual(self.feed.launches, ['active'])
        self.assertEqual(self.feed.removes, ['d'])
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        window = json.loads(
            (self.evidence / 'dead-peer-latency-window.json')
            .read_text())
        for label, entry in window['endpoints'].items():
            self.assertTrue(entry['answered'], label)
            self.assertTrue(entry['within_bound'], label)
        self.assertEqual(window['command']['status'], 200)
        self.assertEqual(window['command']['outcome'],
                         'rejected:not_active')
        self.assertIn('unsynchronized', window['grace_fault'])
        self.assertTrue(window['batch']['completed'])
        self.assertEqual(window['batch']['status'], 200)
        # The batch occupied its worker while the driven monitor kept
        # answering the sampled reads.
        self.assertGreater(self.feed.in_flight_reads, 0)
        health = json.loads(
            (self.evidence / 'dead-peer-latency-health.json')
            .read_text())
        self.assertEqual(health['faults'], [])
        self.assertEqual(health['roles']['driven'], 'standby')
        batch = json.loads(
            (self.evidence / 'dead-peer-latency-healthy-batch.json')
            .read_text())
        self.assertTrue(batch['completed'])
        self.assertEqual(batch['status'], 200)
        self.assertEqual(batch['scans'], 6)
        for label, entry in batch['endpoints'].items():
            self.assertTrue(entry['answered'], label)
            self.assertTrue(entry['within_bound'], label)

    def test_refusing_pull_path_also_passes(self):
        self.feed.pulls_refuse = True
        self.feed.pull_wait = 0
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        window = json.loads(
            (self.evidence / 'dead-peer-latency-window.json')
            .read_text())
        self.assertTrue(window['batch']['completed'])

    def test_slow_survivor_read_fails_by_name(self):
        self.feed.slow_survivor = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('starved', record['detail'])
        report.validate_scenario(record)

    def test_driven_reads_starving_mid_batch_fail(self):
        self.feed.slow_driven = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('starved', record['detail'])
        report.validate_scenario(record)

    def test_admission_rejection_fails_by_name(self):
        self.feed.queue_full = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('admission', record['detail'])
        report.validate_scenario(record)

    def test_mid_window_batch_starving_fails(self):
        self.feed.batch_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('batch starved', record['detail'])
        report.validate_scenario(record)

    def test_missing_grace_fault_fails_by_name(self):
        self.feed.never_unsynced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unsynchronized', record['detail'])
        report.validate_scenario(record)

    def test_no_reconvergence_fails(self):
        self.feed.never_reconverge = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reconverged', record['detail'])
        report.validate_scenario(record)

    def test_missing_driven_actions_inconclusive(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'stop_controller': self.feed.stop,
               'start_controller': self.feed.start}
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no documented seam', record['detail'])
        report.validate_scenario(record)

    def test_failed_launch_inconclusive(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('launch never completed', record['detail'])

    def test_driven_never_serving_inconclusive(self):
        self.feed.driven_never_serves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered /role', record['detail'])

    def test_failed_stop_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('induction never completed', record['detail'])

    def test_identical_evidence_across_runs(self):
        runs = []
        for _ in range(2):
            feed = LatencyFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed,
                                       ctx=self._ctx(feed))
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

    # The plant's sim-net service — the wire dispatch the ctx
    # ['plant_ctl'] seam wraps, covering the tool's list/read
    # subcommands the scenario's field legs drive.
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

    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.field_request(None, request), *args)


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
                'plant_ctl': self.feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_revised': self.feed.start,
                'journal_files': {
                    'active': str(self.journals['a']),
                    'standby': str(self.journals['b']),
                    'revised': str(self.journals['c'])}}

    def run_scenario(self):
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
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
        ctx2['plant_ctl'] = feed2.plant_ctl
        ctx2['start_revised'] = feed2.start
        ctx2['journal_files'] = {
            'active': str(journals2['a']),
            'standby': str(journals2['b']),
            'revised': str(journals2['c'])}
        with patch.object(scenarios, 'http_json', feed2.http_json), \
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

    # The plant's sim-net service — the wire dispatch the ctx
    # ['plant_ctl'] seam wraps, covering the tool's list/read
    # subcommands the scenario's field legs drive.
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

    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.field_request(None, request), *args)


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
                'plant_ctl': feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_revised': feed.start,
                'journal_files': {
                    'active': str(feed.journals['a']),
                    'standby': str(feed.journals['b']),
                    'revised': str(feed.journals['c'])}}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
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
                'plant_ctl': feed2.plant_ctl,
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

    # The plant's sim-net service — the wire dispatch the ctx
    # ['plant_ctl'] seam wraps, covering the tool's list/read
    # subcommands the scenario's field legs drive.
    def field_request(self, ctx, request):
        if request['op'] == 'list_points':
            return {'result': 'points', 'points': [
                {'point': self.WATCH, 'direction': 'out',
                 'sample': self.field, 'fault': None}]}
        if request['op'] == 'read':
            return {'result': 'sample', 'sample': self.field}
        raise AssertionError('unexpected plant request %s' % request)

    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.field_request(None, request), *args)


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
                'plant_ctl': self.feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_foreign': self.feed.start,
                'stop_foreign': self.feed.stop}

    def run_scenario(self, ctx=None):
        with patch.object(scenarios, 'http_json', self.feed.http_json), \
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
        ctx2['plant_ctl'] = feed2.plant_ctl
        ctx2['start_foreign'] = feed2.start
        ctx2['stop_foreign'] = feed2.stop
        with patch.object(scenarios, 'http_json', feed2.http_json), \
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


class DoomedStartupFeed:
    """A stubbed rig for the doomed-startup-claim scenario. ctrl-b
    owns the field — the post-failover layout the suite reaches this
    case in — ctrl-a is its standby, and ctrl-f is the foreign peer
    `start` launches onto the runner-owned --journal-file the scenario
    just corrupted. The fixed shape dies inside the startup replay and
    never touches the plant's single-writer claim, so its monitor
    never answers and the journal file keeps the corrupt record as
    line 1; the pre-fix `claim_then_die` shape preempts the claim on
    its way out — a dead claim fencing the incumbent's next write,
    which demotes it — and `release_on_exit` hands the claim back,
    which still preempted the incumbent and leaves the field
    unclaimed. The plant answers the census, reads, and third-party
    `step` probes: fenced while a claim stands, unclaimed while none
    does. Each incumbent snapshot read is one completed scan landing
    its field write. Every transition is call-count keyed — never
    wall-clock — so two runs emit identical evidence. Fault flags
    stage each named failure the issue calls out."""

    WATCH = 100    # the field `out` point the window watches
    COMMAND = 302  # the writable bool in-point the receipt leg writes

    def __init__(self, journal, state, document):
        self.journal = Path(journal)
        self.state = Path(state)
        self.document = document
        self.ticks = {'a': 0, 'b': 40}
        self.field_tick = 7
        self.owner = 'b'            # the plant's standing claim owner
        self.demoted = False        # ctrl-b met the fence and demoted
        self.launched = False       # ctrl-f's container ran at all
        self.incumbent_down = False
        self.monitor_down = False
        self.open_field = False     # third-party steps slip the fence
        self.receipts = [{'command': {'write_value': {
            'point': self.COMMAND, 'kind': 'bool',
            'value': {'bool': True}}},
            'outcome': {'applied': {'tick': 30}},
            'actor': 'qa-lane'}]
        self.calls = []
        # Fault injection for the named-failure cases.
        self.claim_then_die = False   # the pre-fix stale-claim shape
        self.release_on_exit = False  # the preempting claim released
        self.unclaimed_field = False  # the claim silently evaporates
        self.serves = False           # f's monitor answers anyway
        self.journal_appends = False  # f ran past the failed replay
        self.leaves_state = False     # f persisted a checkpoint
        self.opens_field = False      # probes answer silently writable
        self.no_writable = False      # the model declares no bool target
        self.partner_promotes = False # ctrl-a reports a role change
        self.stall_incumbent = False  # ctrl-b's tick stops advancing
        self.field_stall = False      # its writes stop reaching the field
        self.command_refused = False  # it rejects the mid-window command
        self.receipt_lost = False     # the settled receipt never logged
        self.incumbent_dies = False   # its process stops answering
        self.monitor_lost = False     # only its monitor stops answering
        self.plant_down = False       # the plant refuses every probe
        self.launch_fails = False     # the start action raises
        self.stop_fails = False       # the teardown action raises

    # The runner-owned actions — replace ctx['start_foreign'] and
    # ctx['stop_foreign'].
    def start(self, name):
        self.calls.append(('start_foreign', name))
        if self.launch_fails:
            raise RuntimeError('docker run failed: name in use')
        self.launched = True
        if self.incumbent_dies:
            self.incumbent_down = True
        if self.monitor_lost:
            self.monitor_down = True
        if self.claim_then_die or self.release_on_exit:
            # The doomed startup's preemptive claim landed before its
            # replay failed — the incumbent is superseded whether the
            # dead claim stands (`claim_then_die`) or is handed back
            # (`release_on_exit`).
            self.demoted = True
            self.owner = 'f' if self.claim_then_die else None
        if self.unclaimed_field:
            self.owner = None
        if self.opens_field:
            self.open_field = True
        if self.journal_appends:
            with self.journal.open('a') as stream:
                stream.write(json.dumps(
                    {'run_boundary': {'run': 1, 'tick': 0}}) + '\n')
        if self.leaves_state:
            self.state.parent.mkdir(parents=True, exist_ok=True)
            self.state.write_text(json.dumps({'tick': 9}) + '\n')
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
            role = ('active'
                    if self.partner_promotes and self.launched
                    else 'standby')
            report = {'role': role, 'tick': self.ticks['a']}
            if role == 'standby':
                report['sync'] = 'unsynchronized'
            return report
        if peer == 'b':
            role = 'standby' if self.demoted else 'active'
            report = {'role': role, 'tick': self.ticks['b']}
            if role == 'standby':
                report['sync'] = 'unsynchronized'
            return report
        return {'role': 'standby', 'tick': 0, 'sync': 'unsynchronized'}

    def _snapshot(self):
        # One completed scan: the incumbent's write lands on the field
        # only while its claim stands — a preempted or stalled writer
        # leaves the field tick where the claim died.
        if not self.stall_incumbent:
            self.ticks['b'] += 1
            if not self.demoted and not self.field_stall:
                self.field_tick = self.ticks['b']
        return {'tick': self.ticks['b'],
                'points': [{'point': self.WATCH,
                            'sample': {'value': {'bool': True},
                                       'quality': 'good'}}]}

    def _command(self, body):
        if self.demoted or self.command_refused:
            return 200, {'command': body.get('command'),
                         'outcome': {'rejected': {
                             'reason': {'not_active': None}}},
                         'actor': body.get('actor')}
        receipt = {'command': body.get('command'),
                   'outcome': {'accepted': {
                       'apply_tick': self.ticks['b'] + 1}},
                   'actor': body.get('actor')}
        if not self.receipt_lost:
            self.receipts.append(receipt)
        return 200, receipt

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        route = '/' + url.split('/', 3)[3].partition('?')[0]
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b',
                'ctrl-f:4': 'f'}.get(host)
        if peer is None \
                or (peer == 'b' and (
                    self.incumbent_down or self.monitor_down)) \
                or (peer == 'f' and not (self.launched and self.serves)):
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            return 200, self._role(peer)
        if peer != 'b':
            raise AssertionError('unexpected request %s %s'
                                 % (method, url))
        if (method, route) == ('GET', '/signals'):
            points = [{'point': 10, 'signal': 10010,
                       'name': 'level-primary', 'direction': 'in',
                       'value_type': 'float', 'writable': False}]
            if not self.no_writable:
                points.insert(0, {'point': self.COMMAND,
                                  'signal': 10302, 'name': 'p101-oos',
                                  'direction': 'in',
                                  'value_type': 'bool',
                                  'writable': True})
            return 200, {'points': points}
        if (method, route) == ('GET', '/snapshot'):
            return 200, self._snapshot()
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('POST', '/command'):
            return self._command(body)
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The plant wire protocol — replaces scenarios._plant_probe.
    def plant_request(self, ctx, request, timeout=5):
        if self.plant_down:
            raise urllib.error.URLError('connection refused')
        if self.monitor_down and self.owner == 'b' \
                and not self.field_stall:
            # A monitor-less incumbent keeps scanning — the field
            # tick moves between the window's reads.
            self.field_tick += 1
        if request['op'] == 'list_points':
            sample = {'value': {'bool': True}, 'quality': 'good',
                      'tick': self.field_tick}
            return {'result': 'points', 'points': [
                {'point': 10, 'direction': 'in', 'sample': sample,
                 'fault': None},
                {'point': self.WATCH, 'direction': 'out',
                 'sample': sample, 'fault': None}]}
        if request['op'] == 'read':
            return {'result': 'sample',
                    'sample': {'value': {'bool': True},
                               'quality': 'good',
                               'tick': self.field_tick}}
        if request['op'] == 'step':
            if self.open_field:
                # The silently-writable field: a third attachment's
                # mutation applied under no fencing verdict at all.
                return {'result': 'stepped', 'tick': self.field_tick}
            if self.owner is None:
                return {'result': 'error',
                        'error': {'kind': 'unclaimed',
                                  'detail': 'no attachment holds '
                                            'field writes'}}
            return {'result': 'error',
                    'error': {'kind': 'fenced',
                              'detail': 'another attachment owns '
                                        'field writes'}}
        raise AssertionError('unexpected plant request %s' % request)

    # The shipped plant tool — replaces ctx['plant_ctl'] for the
    # covered subcommands (the census and the field reads); the bare
    # `step` fencing probes stay on the _plant_probe patch above.
    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.plant_request(None, request), *args)


class DoomedStartupClaimTests(unittest.TestCase):
    """scenario_doomed_startup_claim against the stubbed rig: the
    feed's transitions are call-count keyed so each run emits
    identical evidence, and every fault flag stages a named acceptance
    failure — the pre-fix stale claim that demotes the incumbent, the
    released claim's unclaimed window, the silently writable field,
    the doomed peer that serves or journaled past its failed replay,
    and every incumbent disturbance the ordering fix exists to
    prevent."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        directory = Path(self.tmp.name) / 'controllers' / 'foreign'
        self.journal = directory / 'journal.jsonl'
        self.state = directory / 'state.json'
        self.document = Path(self.tmp.name) / 'model-foreign.json'
        self.document.write_text(json.dumps({'revised': True}))
        self.feed = DoomedStartupFeed(self.journal, self.state,
                                      self.document)

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'foreign': 'http://ctrl-f:4',
                'plant': '127.0.0.1:9',
                'plant_ctl': self.feed.plant_ctl,
                'evidence_dir': str(self.evidence),
                'start_foreign': self.feed.start,
                'stop_foreign': self.feed.stop,
                'state_files': {'foreign': str(self.state)},
                'journal_files': {'foreign': str(self.journal)}}

    def run_scenario(self, ctx=None, feed=None):
        feed = feed or self.feed
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_plant_probe',
                             feed.plant_request), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DOOMED_STARTUP_POLL', 0.001):
            return scenarios.scenario_doomed_startup_claim(
                ctx or self._ctx())

    def test_registered_ahead_of_model_revision(self):
        order = list(scenarios.SCENARIOS)
        self.assertLess(
            order.index(scenarios.scenario_checkpoint_negotiation),
            order.index(scenarios.scenario_doomed_startup_claim))
        self.assertLess(
            order.index(scenarios.scenario_doomed_startup_claim),
            order.index(scenarios.scenario_model_revision))

    def test_fixed_shape_passes_validates_and_tears_down(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        kinds = [call[0] for call in self.feed.calls]
        self.assertEqual(kinds, ['start_foreign', 'stop_foreign'])
        self.assertEqual(dict(
            call for call in self.feed.calls if len(call) == 2
        )['start_foreign'], 'standby')
        self.assertFalse(self.feed.launched)
        # The induction artifact is gone — the seat is clean for the
        # revision cases that follow.
        self.assertFalse(self.journal.exists())

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {p.name: p.read_bytes()
                 for p in self.evidence.iterdir()}
        second_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(second_tmp.cleanup)
        evidence2 = Path(second_tmp.name) / 'evidence'
        evidence2.mkdir()
        directory2 = Path(second_tmp.name) / 'controllers' / 'foreign'
        feed2 = DoomedStartupFeed(directory2 / 'journal.jsonl',
                                  directory2 / 'state.json',
                                  self.document)
        ctx2 = self._ctx()
        ctx2['evidence_dir'] = str(evidence2)
        ctx2['plant_ctl'] = feed2.plant_ctl
        ctx2['start_foreign'] = feed2.start
        ctx2['stop_foreign'] = feed2.stop
        ctx2['state_files'] = {'foreign': str(directory2
                                            / 'state.json')}
        ctx2['journal_files'] = {'foreign': str(directory2
                                              / 'journal.jsonl')}
        record2 = self.run_scenario(ctx2, feed2)
        self.assertEqual(record2['outcome'], 'passed', record2)
        second = {p.name: p.read_bytes() for p in evidence2.iterdir()}
        self.assertEqual(set(first), set(second))
        for name, data in first.items():
            self.assertEqual(data, second[name], name)

    def test_claim_then_die_strands_the_incumbent_fails(self):
        # The pre-fix shape: the doomed startup claimed before its
        # replay failed — the dead claim fences the incumbent into a
        # demotion only a restart could clear.
        self.feed.claim_then_die = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record.get('detail', ''))
        report.validate_scenario(record)
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_released_claim_still_disturbs_the_incumbent(self):
        # A claim handed back on the way out still preempted the
        # incumbent mid-window — the field's unclaimed window and the
        # demotion are the disturbance either way.
        self.feed.release_on_exit = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role=active', record.get('detail', ''))
        report.validate_scenario(record)

    def test_unclaimed_window_fails(self):
        self.feed.unclaimed_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unclaimed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_silently_writable_probe_fails(self):
        self.feed.opens_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('fencing', record.get('detail', ''))
        report.validate_scenario(record)

    def test_doomed_peer_serving_fails(self):
        # A monitor that answers never ran the startup replay its
        # corrupt journal file demanded.
        self.feed.serves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('served its monitor', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_append_past_the_replay_fails(self):
        self.feed.journal_appends = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('ran past the failed replay',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_state_file_written_fails(self):
        self.feed.leaves_state = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('persisted a checkpoint',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_partner_role_change_fails(self):
        self.feed.partner_promotes = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('spurious role change',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_tick_stall_fails(self):
        self.feed.stall_incumbent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('tick stalled', record.get('detail', ''))
        report.validate_scenario(record)

    def test_field_writes_stall_fails(self):
        self.feed.field_stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped receiving', record.get('detail', ''))
        report.validate_scenario(record)

    def test_refused_command_fails(self):
        self.feed.command_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('refused the mid-window command',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unlogged_receipt_fails(self):
        self.feed.receipt_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt log never carried',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_incumbent_silence_is_inconclusive(self):
        # The incumbent's monitor refusing every poll while its scans
        # keep landing field writes is unreachable evidence, not a
        # disturbance.
        self.feed.monitor_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered during the window',
                      record.get('detail', ''))
        report.validate_scenario(record)
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_dead_incumbent_fails(self):
        # A halted incumbent — monitor silent AND its field writes
        # stopped — is the disturbance itself, whatever caused it.
        self.feed.incumbent_dies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stopped receiving', record.get('detail', ''))
        report.validate_scenario(record)
        self.assertIn(('stop_foreign',), self.feed.calls)

    def test_failed_launch_action_is_inconclusive(self):
        self.feed.launch_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('action never completed',
                      record.get('detail', ''))
        report.validate_scenario(record)
        # The induction artifact still comes back out.
        self.assertFalse(self.journal.exists())

    def test_failed_teardown_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never removed', record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_seams_are_inconclusive(self):
        ctx = self._ctx()
        del ctx['start_foreign']
        del ctx['stop_foreign']
        del ctx['foreign']
        del ctx['journal_files']
        record = self.run_scenario(ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unreachable_plant_is_inconclusive(self):
        self.feed.plant_down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_unwritable_model_is_inconclusive(self):
        # No writable bool in the served model — the receipted-path
        # leg cannot run, so the case reports inconclusive rather than
        # skipping the leg.
        self.feed.no_writable = True
        record = self.run_scenario()
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

    # The shipped plant tool — replaces ctx['plant_ctl'] for the
    # covered census; the bare `step` fencing probes stay on the
    # _plant_probe patch above.
    def plant_ctl(self, *args):
        return _ctl_wrap(
            lambda request: self.plant_request(None, request), *args)

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
               'plant_ctl': self.feed.plant_ctl,
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
    named rejection — while http_json covers the raw liveness gate.
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
        self.receipts_log = []  # the executor's settled-receipt log
        self.calls = []      # the recorded (addr, argv) transcript
        self.binaries = []   # the binary path each invocation ran
        # Fault injection for the named-failure cases.
        self.role_mismatch = False     # the CLI's role reads both standby
        self.missing_kind = False      # the schema read drops an instance
        self.resources_misses = False  # the resources view drops an instance
        self.resources_entry_wrong = False  # a named entry mismatches
        self.events_map_broken = False  # the keyed events view drops a key
        self.receipts_missing = False  # the receipt log loses the invoke
        self.history_empty = False     # the point retains no samples
        self.no_bound_measurement = False  # no measurement binds a point
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
            if len(args) == 1:
                keyed = {record['name']: self._events_for(record['name'])
                         for record in self.COMPONENTS}
                if self.events_map_broken:
                    keyed.pop('motor:21', None)
                return self._ok(keyed)
            entry = self._view_entry(args[1])
            if entry is None:
                return '', 1, 'dcs-ctl: ' + addr \
                    + ': no served component named ' + repr(args[1])
            return self._ok(entry['events'])
        if args[0] == 'resources':
            if len(args) == 1:
                return self._ok(self._view())
            entry = self._view_entry(args[1])
            if entry is None:
                return '', 1, 'dcs-ctl: ' + addr \
                    + ': no served component named ' + repr(args[1])
            return self._ok(entry)
        if args == ['receipts']:
            return self._ok(self.receipts_log)
        if args[0] == 'history':
            points = [int(args[index + 1])
                      for index, arg in enumerate(args)
                      if arg == '--point']
            return self._ok([{'point': point,
                              'samples': self._samples(point)}
                             for point in points])
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
        # the real executor's durable record. The receipt log mirrors
        # the same settled outcome. `carryover` injects the
        # entry a checkpoint-adopted receipt re-journals on this peer —
        # an earlier leg's identical command under its own actor —
        # landing above the consumer's pre-submission cursor, ahead of
        # this submission's own settlement.
        settled = dict(receipt)
        if refused is None:
            settled['outcome'] = {'applied': {'tick': self.tick + 1}}
        if self.wrong_actor:
            settled['actor'] = 'qa-lane'
        if not self.receipts_missing:
            self.receipts_log.append(settled)
        if not self.no_journal_entry:
            if self.carryover is not None:
                self.journal.append({'seq': self.next_seq,
                                     'tick': self.tick,
                                     'event': {'command_settled': {
                                         'receipt': self.carryover}}})
                self.next_seq += 1
                self.carryover = None
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
        """One kind's declared interface — a bound measurement, a
        writable-bool write, an unavailable non-writable write (the
        unavailable probe's target), a parameter tune, and the emitted
        settled-event entry."""
        measurement = {'name': 'in', 'kind': 'bool'}
        if not self.no_bound_measurement:
            measurement['point'] = 302
        return {'version': 1, 'kind': kind,
                'measurements': [measurement],
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
        interface = self._interface(record['kind'])
        commands = []
        for spec in interface['commands']:
            available = spec.get('adapted') != 'write_value' \
                or spec.get('point') in self.WRITABLE
            commands.append({'name': spec['name'], 'available': available,
                             'refusal': None if available else
                             'point ' + str(spec.get('point'))
                             + ' is not writable'})
        measurements = [{'name': spec['name'], 'point': spec.get('point'),
                         'sample': {'value': {'bool': True},
                                    'quality': {'good': {}},
                                    'tick': self.tick}}
                        for spec in interface['measurements']]
        entry = {'name': record['name'], 'kind': record['kind'],
                 'measurements': measurements, 'configuration': [],
                 'state': [], 'commands': commands,
                 'events': self._events_for(record['name'])}
        if self.resources_entry_wrong:
            entry['commands'] = []
        return entry

    def _view(self):
        """The served ResourceView every resources/events read derives
        from — one live record per component instance."""
        records = self.COMPONENTS[:1] if self.resources_misses \
            else self.COMPONENTS
        return {'publication': self.tick, 'tick': self.tick,
                'components': [self._resources(record)
                               for record in records]}

    def _view_entry(self, name):
        """The named instance's ComponentResources — the lookup
        `resources <component>`/`events <component>` share."""
        for record in self.COMPONENTS:
            if record['name'] == name:
                return self._resources(record)
        return None

    def _events_for(self, name):
        """One instance's attributed events — the retained journal
        tail the served view attributes to it."""
        if self.no_events:
            return []
        return [entry for entry in self.journal
                if self._attributed(entry, name)]

    def _samples(self, point):
        """The retained samples a `history --point` read answers for
        a declared point."""
        if self.history_empty or point not in self.KINDS:
            return []
        value = {'bool': True} if self.KINDS[point] == 'bool' \
            else {'float': 1.0}
        return [{'seq': seq,
                 'sample': {'value': value, 'quality': {'good': {}},
                            'tick': seq}}
                for seq in (1, 2, 3)]

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
    # gate; every served-resource read rides the binary seam now.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        if (method, route) == ('GET', '/role'):
            return 200, {'role': 'active' if host == 'ctrl-b:2'
                         else 'standby', 'tick': self.tick}
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
        for name in ('dcs-ctl-roles', 'dcs-ctl-schema',
                     'dcs-ctl-resources', 'dcs-ctl-invoke',
                     'dcs-ctl-journal', 'dcs-ctl-events',
                     'dcs-ctl-receipts', 'dcs-ctl-history',
                     'dcs-ctl-refusals', 'dcs-ctl-transcript'):
            self.assertTrue((self.evidence / (name + '.json')).is_file(),
                            name)
        transcript = json.loads(
            (self.evidence / 'dcs-ctl-transcript.json').read_text())
        argvs = [entry['argv'] for entry in transcript]
        # The consumer contract end to end: role on both endpoints, the
        # served-resource reads, the picked command with the declared
        # actor, the journal and events reads, receipts and history,
        # and both refusal probes.
        self.assertIn(['ctrl-a:1', 'role'], argvs)
        self.assertIn(['ctrl-b:2', 'role'], argvs)
        self.assertIn(['ctrl-b:2', 'resources'], argvs)
        self.assertIn(['ctrl-b:2', 'resources', 'digital-input:12'],
                      argvs)
        self.assertIn(['ctrl-b:2', 'write', '302', 'true',
                       '--actor', scenarios.CTL_ACTOR], argvs)
        self.assertIn(['ctrl-b:2', 'events'], argvs)
        self.assertIn(['ctrl-b:2', 'events', 'digital-input:12'], argvs)
        self.assertIn(['ctrl-b:2', 'receipts'], argvs)
        self.assertIn(['ctrl-b:2', 'history', '--point', '302'], argvs)
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

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        first = {path.name: path.read_text()
                 for path in self.evidence.iterdir()}
        self.evidence = self.evidence.parent / 'evidence-2'
        self.evidence.mkdir()
        self.feed = CtlFeed()
        again = self.run_scenario()
        second = {path.name: path.read_text()
                  for path in self.evidence.iterdir()}
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

    def test_resources_missing_component_fails(self):
        self.feed.resources_misses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('one kind-matched record per declared component',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_resources_entry_mismatch_fails(self):
        self.feed.resources_entry_wrong = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('does not mirror the served interface',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_keyed_events_missing_component_fails(self):
        self.feed.events_map_broken = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('keyed events read', record.get('detail', ''))
        report.validate_scenario(record)

    def test_receipts_missing_invoke_fails(self):
        self.feed.receipts_missing = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt log never recorded',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_history_without_samples_fails(self):
        self.feed.history_empty = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('retains no served samples',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_no_bound_measurement_is_inconclusive(self):
        self.feed.no_bound_measurement = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no served measurement binds a point',
                      record.get('detail', ''))
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


class StarvationFeed:
    """A stubbed armed pair for the monitor-starvation leg. ctrl-a is
    the flooded active — each served /snapshot is one paced scan
    having landed — and ctrl-b is the armed tracking standby: each
    /role poll on it is one landed checkpoint pull, its reported
    tracking alignment advancing one scan, unless a doctor moves its
    sync or role mid-hold. The leg's raw-connection seam hands out
    FakeSockets; the plant fences every third-party probe while the
    writer claim stands."""
    POINT = 204

    def __init__(self):
        self.tick = 0         # the active's run tick
        self.aligned = 0      # the standby's landed checkpoint pulls
        self.polls = 0        # standby /role polls — the heartbeat
        self.receipts = []
        self.journal_a = []
        self.journal_b = []
        self.seq = 0
        self.sockets = []
        # The doctors for the named-failure cases.
        self.starve_reads = False    # the serving reads never answer
        self.late_reads = False      # they answer past the bound
        self.promote_after = None    # poll count the standby promotes at
        self.stall_after = None      # poll count its pulls stop landing
        self.never_tracks = False    # no armed tracking standby settles
        self.unfenced = False        # probes write through the claim
        self.claim_lost = False      # a field_claim_lost is journaled
        self.role_journaled = False  # a role_changed is journaled
        self.no_recover = False      # the submission lane never frees

    # --- the runner-owned transport seams ---

    def connect(self, base, timeout=5):
        stream = FakeSocket()
        self.sockets.append(stream)
        return stream

    def plant(self, ctx, request, timeout=5):
        if self.unfenced:
            return {'ok': {'stepped': True}}
        return {'error': {'kind': 'fenced'}}

    def request_status(self, method, url, body=None, timeout=5):
        if self.no_recover:
            raise urllib.error.URLError('timed out')
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a') and path == '/scan' \
                and method == 'POST':
            return 409, b'{"error": "paced"}'
        raise urllib.error.URLError('no route ' + path)

    # --- the stubbed monitor surface ---

    def http_json(self, method, url, body=None, timeout=5):
        host, _, path = url[7:].partition('/')
        path = '/' + path
        if host.startswith('ctrl-a'):
            return self.active(method, path, body)
        if host.startswith('ctrl-b'):
            return self.standby(method, path, body)
        raise urllib.error.URLError('unknown host ' + host)

    def active(self, method, path, body):
        if method == 'GET' \
                and any(not stream.closed for stream in self.sockets):
            if self.starve_reads:
                raise urllib.error.URLError('timed out')
            if self.late_reads:
                time.sleep(0.5)   # past the patched bound — a late
                                  # answer, the bound contract broken
        if path == '/role':
            return 200, {'role': 'active', 'tick': self.tick}
        if path == '/signals':
            return 200, {'points': [
                {'name': 'p101-oos', 'point': self.POINT,
                 'kind': 'bool', 'writable': True,
                 'direction': 'in', 'value_type': 'bool'}]}
        if path == '/snapshot':
            self.tick += 1   # one paced scan landed per served read
            return 200, {'tick': self.tick,
                         'points': [{'point': self.POINT,
                                     'sample': {'value': {
                                         'bool': False}}}]}
        if path == '/checkpoint':
            return 200, {'tick': self.tick,
                         'model_fingerprint': 'fp'}
        if path == '/receipts':
            for receipt in self.receipts:
                if 'accepted' in receipt['outcome']:
                    receipt['outcome'] = {'applied': {'tick': self.tick}}
                    self.seq += 1
                    self.journal_a.append({
                        'seq': self.seq, 'tick': self.tick,
                        'event': {'command_settled': {'receipt': {
                            'id': receipt['id'], 'actor': 'qa-lane',
                            'command': receipt['command'],
                            'outcome': {'applied': {}}}}}})
            return 200, list(self.receipts)
        if path.startswith('/journal'):
            entries = list(self.journal_a)
            if '?since=' in path and self.claim_lost:
                entries.append({'seq': 90, 'tick': self.tick,
                                'event': {'field_claim_lost': {
                                    'point': 9}}})
            return 200, entries
        if path == '/command' and method == 'POST':
            if self.no_recover:
                raise urllib.error.URLError('timed out')
            receipt = {'id': 'r' + str(len(self.receipts) + 1),
                       'command': body['command'],
                       'outcome': {'accepted': {}},
                       'actor': body.get('actor')}
            self.receipts.append(receipt)
            return 200, receipt
        raise urllib.error.URLError('no route ' + path)

    def standby(self, method, path, body):
        if path == '/role':
            self.polls += 1
            if self.never_tracks:
                return 200, {'role': 'standby', 'tick': self.aligned,
                             'sync': {'unsynchronized': {}}}
            if self.promote_after is not None \
                    and self.polls >= self.promote_after:
                return 200, {'role': 'promoting', 'tick': self.aligned}
            if self.stall_after is not None \
                    and self.polls >= self.stall_after:
                return 200, {'role': 'standby', 'tick': self.aligned,
                             'sync': {'degraded': {'detail':
                                      'fetch failed: timed out'}}}
            self.aligned += 1   # one landed checkpoint pull
            return 200, {'role': 'standby', 'tick': self.aligned,
                         'sync': {'tracking': {'aligned': self.aligned}}}
        if path.startswith('/journal'):
            entries = list(self.journal_b)
            if '?since=' in path and self.role_journaled:
                entries.append({'seq': 91, 'tick': self.aligned,
                                'event': {'role_changed': {
                                    'from': 'standby',
                                    'to': 'promoting'}}})
            return 200, entries
        raise urllib.error.URLError('no route ' + path)


class MonitorStarvationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = StarvationFeed()

    def _ctx(self, feed=None, **extra):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'plant': 'tcp://plant:9', 'failover_misses': 120,
               'evidence_dir': str(self.evidence)}
        ctx.update(extra)
        return ctx

    def run_scenario(self, feed=None, ctx=None, **patches):
        feed = feed or self.feed
        constants = {'POLL_INTERVAL': 0.001, 'LATENCY_BOUND': 0.3,
                     'STARVE_SETTLE': 1.0, 'STARVE_POLL': 0.001,
                     'STARVE_DEADLINE': 1.0, 'STARVE_PULL_ADVANCE': 3,
                     'STARVE_RECOVER': 1.0}
        constants.update(patches)
        with patch.multiple(scenarios, **constants), \
                patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, '_connect', feed.connect), \
                patch.object(scenarios, '_plant_probe', feed.plant), \
                patch.object(scenarios, '_request_status',
                             feed.request_status):
            return scenarios.scenario_monitor_starvation(
                ctx or self._ctx(feed))

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        self.assertIn(scenarios.scenario_monitor_starvation, order)
        # The leg sits in the armed pre-switch window — ahead of the
        # tune case's a->b switch, behind the dead-peer case that
        # restores the rig it shares the window with.
        self.assertLess(
            order.index(scenarios.scenario_dead_peer_latency),
            order.index(scenarios.scenario_monitor_starvation))
        self.assertLess(
            order.index(scenarios.scenario_monitor_starvation),
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(verify.case_function('monitor-starvation'),
                      scenarios.scenario_monitor_starvation)

    def test_armed_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        self.assertEqual(record['observations'],
                         ['two starvation passes, identical digests'])
        self.assertEqual(len(self.feed.sockets),
                         scenarios.STARVE_CONNECTIONS * 2)
        self.assertTrue(all(stream.closed
                            for stream in self.feed.sockets))
        self.assertTrue(all(stream.sent in scenarios.STARVE_REQUESTS
                            for stream in self.feed.sockets))
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        digest = None
        for number in (1, 2):
            window = json.loads(
                (self.evidence
                 / ('monitor-starvation-pass-' + str(number)
                    + '.json')).read_text())
            self.assertEqual(window['digest']['reads'], {
                '/role': 'bounded', '/snapshot': 'bounded',
                '/checkpoint': 'bounded'})
            self.assertEqual(window['digest']['scans'], 'advanced')
            self.assertEqual(window['digest']['tracking'], 'advanced')
            self.assertEqual(window['digest']['fencing'], 'fenced')
            self.assertEqual(window['digest']['journal'], 'clean')
            self.assertEqual(window['digest']['recovery'], 'settled')
            self.assertEqual(window['digest']['roles'], 'unchanged')
            self.assertEqual(window['violations'], {})
            self.assertEqual(window['armed_miss_budget'], 120)
            if digest is not None:
                self.assertEqual(digest, window['digest'])
            digest = window['digest']

    def test_starved_liveness_reads_report_failed(self):
        self.feed.starve_reads = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-failed'), record['detail'])
        report.validate_scenario(record)

    def test_late_liveness_reads_report_nondeterministic(self):
        self.feed.late_reads = True
        record = self.run_scenario(STARVE_PULL_ADVANCE=1,
                                   STARVE_DEADLINE=4.0)
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('past the', record['detail'])
        report.validate_scenario(record)

    def test_promoting_standby_reports_nondeterministic(self):
        self.feed.promote_after = 4
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('promoting', record['detail'])
        report.validate_scenario(record)

    def test_stalled_pulls_report_failed(self):
        self.feed.stall_after = 3
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-failed'), record['detail'])
        report.validate_scenario(record)

    def test_unfenced_probe_reports_nondeterministic(self):
        self.feed.unfenced = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('wrote through', record['detail'])
        report.validate_scenario(record)

    def test_journaled_claim_loss_reports_nondeterministic(self):
        self.feed.claim_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('field_claim_lost', record['detail'])
        report.validate_scenario(record)

    def test_journaled_role_change_reports_nondeterministic(self):
        self.feed.role_journaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-nondeterministic'), record['detail'])
        self.assertIn('role_changed', record['detail'])
        report.validate_scenario(record)

    def test_unrecovered_submission_lane_reports_failed(self):
        self.feed.no_recover = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'monitor-starvation-failed'), record['detail'])
        self.assertIn('never answered a command', record['detail'])
        report.validate_scenario(record)

    def test_no_armed_tracking_standby_inconclusive(self):
        self.feed.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('no armed tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_missing_plant_endpoint_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('fencing probes', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = StarvationFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed,
                                       ctx=self._ctx(feed))
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


class RearmPlantPeer(FakePlantPeer):
    """The unclaimed-rearm rig's plant half: FakePlantPeer plus the
    full write-ownership arbitration the scenario induces — per-
    attachment holder tracking, `claim_writer`'s unconditional
    preemption, `ensure_writer`'s conditional grant, `release_writer`
    dropping only this attachment's hold (the last-holder release
    unclaiming the field), disconnect dropping the hold but never the
    claim, and `write`/`step` fencing against the standing claim —
    the server's contract shape. The feed drives the controller
    side's scan writes through `owner_write`, the RemoteDriver re-arm
    contract applied: fenced under a foreign claim, an unclaimed
    field re-arming the recorded token inline before the write lands."""

    IN, OUT = 10, 100

    def __init__(self, owner):
        super().__init__()
        self.samples = {
            self.IN: {'value': {'float': 0.8}, 'quality': 'good',
                      'tick': 0},
            self.OUT: {'value': {'float': 0.0}, 'quality': 'good',
                       'tick': 0}}
        self.owner = owner
        self.claim = {'owner': owner, 'holders': {'controller'},
                      'phantom': False}   # the standing owner claim
        self.plant_tick = 0
        # The doctors staging each named defect.
        self.down = False           # every attachment drops unanswered
        self.open_field = False     # mutations ignore the claim
        self.phantom_rearm = False  # a re-armed claim never fences
        self.shared = False         # claims answer claimed_shared
        self.release_refuses = False
        self.dropped = False        # the re-arm keeps dropping writes

    def _holders(self):
        return (self.claim or {}).get('holders') or set()

    def owner_write(self, demote_on_unclaimed=False,
                    rearm_drops=False):
        """One controller-scan field write through the RemoteDriver's
        inline re-arm: 'fenced' under a foreign claim (the caller
        demotes); an unclaimed field re-arms the recorded token in
        place and the write lands — unless the doctor drops it."""
        if self.claim is None:
            if demote_on_unclaimed:
                return 'fenced'   # the defect: demote on Unclaimed
            self.claim = {'owner': self.owner,
                          'holders': {'controller'},
                          'phantom': self.phantom_rearm}
            if rearm_drops:
                self.dropped = True
                return 'rearmed'  # claim re-armed, write never landed
        elif 'controller' not in self._holders():
            return 'fenced'
        if self.dropped:
            return 'rearmed'      # the writes keep dropping
        self.plant_tick += 1
        self.samples[self.OUT].update(tick=self.plant_tick,
                                      value={'float': 1.5})
        return 'landed'

    def release_conn(self, conn):
        # Disconnect drops the attachment's hold, never the claim —
        # the dead-owner fencing the standing claim provides.
        if self.claim is not None:
            self.claim['holders'].discard(conn)

    def _claim_for(self, conn, request):
        op, owner = request['op'], request.get('owner')
        if op == 'release_writer':
            if self.release_refuses:
                return {'result': 'error', 'error': {
                    'kind': 'invalid_request',
                    'detail': 'release refused'}}
            if self.claim is not None:
                self.claim['holders'].discard(conn)
                if not self.claim['holders']:
                    self.claim = None
            return {'result': 'done'}
        shared = self.claim is not None \
            and self.claim['owner'] == owner \
            and any(h is not conn for h in self._holders())
        if op == 'ensure_writer' and self.claim is not None \
                and self.claim['owner'] != owner \
                and not self.claim.get('phantom'):
            return {'result': 'error', 'error': {
                'kind': 'fenced',
                'detail': 'the field is owned by another attachment'}}
        if self.claim is None or self.claim['owner'] != owner:
            self.claim = {'owner': owner, 'holders': {conn},
                          'phantom': False}
        else:
            self.claim['holders'].add(conn)
        if shared or self.shared:
            return {'result': 'claimed_shared', 'owner': owner}
        return {'result': 'done'}

    def dispatch_for(self, conn, request):
        if self.down:
            raise OSError('the plant is down')
        op = request.get('op')
        if op in ('claim_writer', 'ensure_writer', 'release_writer'):
            self.requests.append(request)
            return self._claim_for(conn, request)
        if op in ('write', 'step'):
            self.requests.append(request)
            if self.open_field or conn in self._holders():
                if op == 'step':
                    self.plant_tick += 1
                    return {'result': 'stepped',
                            'tick': self.plant_tick}
                self.samples[request['point']].update(
                    value=request['value'], tick=self.plant_tick)
                return {'result': 'done'}
            if self.claim is None:
                return {'result': 'error', 'error': {
                    'kind': 'unclaimed',
                    'detail': 'no attachment holds field writes'}}
            if op == 'step':
                return {'result': 'error', 'error': {
                    'kind': 'fenced',
                    'detail': 'another attachment owns field writes'}}
            return {'result': 'error', 'error': {
                'kind': 'io', 'error': {'fenced': request['point']}}}
        if op == 'list_points':
            self.requests.append(request)
            return {'result': 'points', 'points': [
                {'point': p, 'sample': self.served(p),
                 'direction': 'out' if p == self.OUT else 'in',
                 'fault': self.faults.get(p)}
                for p in sorted(self.samples)]}
        return super().dispatch_for(conn, request)


class UnclaimedRearmFeed:
    """A stubbed pair for the unclaimed-rearm scenario: ctrl-a is the
    field owner — each served /snapshot is one scan's write attempted
    through the plant peer's claim arbitration — and ctrl-b is the
    tracking standby. The pair reports the settled active/standby
    layout the leg induces on, the journals and the io_health
    fencing-loss ledger the leg audits, and doctor flags stage each
    named defect the issue calls out."""

    OWNER = 424243

    def __init__(self, plant):
        self.plant = plant
        self.tick = 0
        self.demoted = False
        self.failed_writes = 0
        self.journal = []
        self.seq = 0
        self.peer_role_calls = 0
        # The doctors staging each named defect.
        self.silent = False             # ctrl-a never reports
        self.demote_on_unclaimed = False  # the window demotes it
        self.rearm_drops = False        # the re-arm drops the write
        self.ledger_grows = False       # the fencing ledger grows
        self.claim_lost_journaled = False
        self.role_journaled = False
        self.peer_journaled = False
        self.peer_moves = False         # the standby reports a move
        self.peer_wrong_role = False    # the pair never settles
        self.peer_demoted = False       # the restore's demote landed

    def _scan(self):
        """One controller scan: the field write attempted through the
        plant's claim arbitration — the fencing verdict demotes the
        owner through the settled fencing-loss contract."""
        self.tick += 1
        if self.demoted:
            return
        if self.plant.owner_write(
                demote_on_unclaimed=self.demote_on_unclaimed,
                rearm_drops=self.rearm_drops) == 'fenced':
            self.demoted = True
            self.failed_writes += 1
            for event in ({'field_claim_lost':
                           {'point': self.plant.OUT}},
                          {'role_changed': {'from': 'active',
                                            'to': 'standby'}}):
                self.seq += 1
                self.journal.append({'seq': self.seq,
                                     'tick': self.tick,
                                     'event': event})
        if self.ledger_grows:
            self.failed_writes += 1

    def _journal(self, since):
        entries = [dict(e) for e in self.journal if e['seq'] > since]
        if self.claim_lost_journaled:
            entries.append({'seq': since + 1, 'tick': self.tick,
                            'event': {'field_claim_lost':
                                      {'point': self.plant.OUT}}})
        if self.role_journaled:
            entries.append({'seq': since + 2, 'tick': self.tick,
                            'event': {'role_changed':
                                      {'from': 'active',
                                       'to': 'standby'}}})
        return entries

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        since = int(query.split('=', 1)[1]) \
            if query.startswith('since=') else 0
        if host.startswith('ctrl-b'):
            if route == '/role':
                self.peer_role_calls += 1
                role = 'standby'
                if self.peer_moves and self.peer_role_calls > 1 \
                        and not self.peer_demoted:
                    role = 'promoting'
                if self.peer_wrong_role:
                    role = 'active'
                return 200, {'role': role, 'tick': 0,
                             'sync': {'tracking': {'aligned': 1}}}
            if route == '/demote':
                self.peer_demoted = True
                return 200, {'role': 'standby'}
            if route == '/journal':
                entries = []
                if self.peer_journaled:
                    entries = [{'seq': since + 1, 'tick': 0,
                                'event': {'role_changed':
                                          {'from': 'standby',
                                           'to': 'promoting'}}}]
                return 200, entries
            raise AssertionError('unhandled ' + url)
        if not host.startswith('ctrl-a'):
            raise urllib.error.URLError('unknown host ' + host)
        if self.silent:
            raise urllib.error.URLError('unreachable')
        if route == '/role':
            return 200, {'tick': self.tick,
                         'role': 'standby' if self.demoted
                                 else 'active'}
        if route == '/snapshot':
            self._scan()
            return 200, {'tick': self.tick, 'points': [],
                         'io_health':
                         {'failed_writes': self.failed_writes,
                          'journal_errors': 0, 'journals_appended': 0}}
        if route == '/journal':
            return 200, self._journal(since)
        if route == '/promote':
            self.demoted = False
            return 200, {'role': 'active'}
        raise AssertionError('unhandled ' + url)


class RotationPlant:
    """The field half of the duty-rotation rig: the lane dynamics'
    ambient inflow — the net-flow sum's declared bias — integrated
    against the pump draws, plus the field command points the
    field-owning peer writes. One owner scan steps one tick of process
    time."""

    START = 2.0      # the demand chain's asserted bound
    STOP = 1.0       # the demand chain's released bound

    def __init__(self):
        self.level = 0.8
        self.ambient = 0.02     # metres per owner scan, unopposed
        self.draw = 0.16        # metres per commanded pump per scan
        self.inflow_declared = True
        self.never_rises = False    # the declared inflow never reaches
        self.cmd = {100: False, 101: False}

    def step(self):
        net = self.ambient if self.inflow_declared else 0.0
        for running in self.cmd.values():
            if running:
                net -= self.draw
        if not self.never_rises:
            self.level += net


class RotationLine:
    """One peer's checkpointed executor state: the pump-group's
    rotation position, timers, accumulators, and command flags, the
    chain's held demand, the writable inputs, the none-available
    alarm's latch, and the served output image (`values`)."""

    JOURNALED = (40, 41, 217, 302, 334, 1033, 1034)

    def __init__(self):
        self.duty_index = None      # 0 | 1 | None
        self.cursor = 0             # 0-based, mirrors rotation_cursor-1
        self.run_hours = [0, 0]
        self.held_until = [0, 0]
        self.commanded = [False, False]
        self.last_demand = 0
        self.last_start = None
        self.demand = 0
        self.oos = [False, False]
        self.ack = False
        self.alarm_ticks = 0
        self.unack = False
        self.values = {}            # served output point -> raw value

    def pump_state(self):
        """The pump-group's checkpointed StateMap — the carried
        vocabulary the scenario reads back."""
        return {
            'rotation': {'int': 0},
            'min_off_ticks': {'int': 2},
            'start_delay_ticks': {'int': 1},
            'duty': {'int': 0 if self.duty_index is None
                     else self.duty_index + 1},
            'rotation_cursor': {'int': self.cursor + 1},
            'last_demand': {'int': self.last_demand},
            'run_hours_1': {'int': self.run_hours[0]},
            'run_hours_2': {'int': self.run_hours[1]},
            'held_until_1': {'int': self.held_until[0]},
            'held_until_2': {'int': self.held_until[1]},
            'commanded_1': {'bool': self.commanded[0]},
            'commanded_2': {'bool': self.commanded[1]}}


class RotationPeer:
    """One endpoint of the rotation pair: role, tracking posture, the
    adopted/executed line, its journal, and its receipt log."""

    def __init__(self, name):
        self.name = name
        self.tick = 0
        self.role = 'standby'    # active | standby | demoting | promoting
        self.tracking = False
        self.line = RotationLine()
        self.journal = []
        self.next_seq = 1
        self.receipts = []


class RotationPair:
    """A stubbed redundant pair plus its simulated station for the
    duty-rotation scenario: two monitor endpoints over one plant.
    Every call on the field-owning peer is one completed scan — the
    plant steps, the chain holds demand between `start` and `stop`,
    and the group alternates duty, banks the declared holdout, and
    accrues run-hours exactly as the block does. Calls on the tracking
    peer adopt the owner's published line — the checkpoint pull — so
    its snapshot and checkpoint serve what the owner writes. Fault
    flags stage each named failure the issue calls out."""

    SIGNALS = [
        {'point': 12, 'name': 'inflow', 'direction': 'in',
         'value_type': 'float', 'writable': False},
        {'point': 40, 'name': 'p101-run', 'direction': 'in',
         'value_type': 'bool', 'writable': False},
        {'point': 41, 'name': 'p102-run', 'direction': 'in',
         'value_type': 'bool', 'writable': False},
        {'point': 100, 'name': 'p101-cmd', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 101, 'name': 'p102-cmd', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 200, 'name': 'level-selected', 'direction': 'out',
         'value_type': 'float', 'writable': False},
        {'point': 205, 'name': 'demand-in', 'direction': 'in',
         'value_type': 'int', 'writable': False},
        {'point': 210, 'name': 'duty', 'direction': 'out',
         'value_type': 'int', 'writable': False},
        {'point': 211, 'name': 'staged', 'direction': 'out',
         'value_type': 'int', 'writable': False},
        {'point': 217, 'name': 'none-available', 'direction': 'out',
         'value_type': 'bool', 'writable': False},
        {'point': 302, 'name': 'p101-oos', 'direction': 'in',
         'value_type': 'bool', 'writable': True},
        {'point': 334, 'name': 'p102-oos', 'direction': 'in',
         'value_type': 'bool', 'writable': True},
        {'point': 1030, 'name': 'none-available-ack', 'direction': 'in',
         'value_type': 'bool', 'writable': True},
        {'point': 1034, 'name': 'none-available-unacknowledged',
         'direction': 'out', 'value_type': 'bool', 'writable': False}]

    def __init__(self, plant):
        self.plant = plant
        self.a = RotationPeer('a')
        self.b = RotationPeer('b')
        self.a.role = 'active'
        self.b.role = 'standby'
        self.b.tracking = True
        self.owner = 'a'
        self.switched_once = False
        # Fault injection for the named-failure cases.
        self.no_active = False       # no peer ever reports active
        self.no_tracking = False     # the standby never converges
        self.bare_signals = False    # the leg's wiring is absent
        self.duty_mute = False       # the duty output never moves
        self.never_stages = False    # the group never stages a pump
        self.never_stops = False     # a staged pump never de-stages
        self.no_alternate = False    # the cursor never advances
        self.no_holdout = False      # the stop banks no holdout
        self.peer_resets = False     # the promoted peer reinitializes
        self.run_hours_frozen = False  # run-hours never accrue
        self.promote_never = False   # every promote is refused
        self.restore_never = False   # the fail-back's promote refuses
        self.no_journal = False      # transitions never journal
        self.no_receipts = False     # settlements never journal
        self.ack_stuck = False       # the unack latch never clears

    def _peers(self):
        return {'a': self.a, 'b': self.b}

    def _other(self, peer):
        return self.b if peer.name == 'a' else self.a

    def _mark(self, peer, event):
        if not self.no_journal:
            peer.journal.append({'seq': peer.next_seq,
                                 'tick': peer.tick, 'event': event})
            peer.next_seq += 1

    def _drive(self, peer, point, value):
        """A journaled output/input transition, mirroring the real
        driver's point_changed on declared-journaled bool points."""
        previous = peer.line.values.get(point)
        peer.line.values[point] = value
        if previous != value and point in RotationLine.JOURNALED:
            self._mark(peer, {'point_changed': {
                'point': point,
                'from': None if previous is None
                else {'bool': previous},
                'to': {'bool': value}}})

    def _assign(self, line):
        avail = [not held for held in line.oos]
        line.duty_index = None
        for offset in range(2):
            idx = (line.cursor + offset) % 2
            if avail[idx]:
                line.duty_index = idx
                break
        if line.duty_index is not None and not self.no_alternate:
            line.cursor = (line.duty_index + 1) % 2

    def _settle(self, peer):
        for receipt in peer.receipts:
            accepted = receipt['outcome'].get('accepted')
            if accepted is None or peer.tick < accepted['apply_tick']:
                continue
            write = receipt['command']['write_value']
            value = write['value']['bool']
            if write['point'] == 302:
                peer.line.oos[0] = value
                self._drive(peer, 302, value)
            elif write['point'] == 334:
                peer.line.oos[1] = value
                self._drive(peer, 334, value)
            elif write['point'] == 1030:
                peer.line.ack = value
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            if not self.no_receipts:
                self._mark(peer, {'command_settled':
                                  {'receipt': dict(receipt)}})

    def _execute(self, peer):
        """One owner scan of the threshold chain plus the pump group,
        mirroring the real blocks' step ordering."""
        line, tick = peer.line, peer.tick
        if self.plant.level >= RotationPlant.START:
            line.demand = 1
        elif self.plant.level < RotationPlant.STOP:
            line.demand = 0
        avail = [not held for held in line.oos]
        if line.duty_index is not None and not avail[line.duty_index]:
            self._assign(line)
        elif line.last_demand >= 1 and line.demand == 0:
            self._assign(line)
        if line.duty_index is None:
            self._assign(line)
        lead = line.duty_index if line.duty_index is not None \
            else line.cursor
        targets = []
        if not self.never_stages:
            for offset in range(2):
                if len(targets) >= line.demand:
                    break
                idx = (lead + offset) % 2
                if avail[idx] and (line.commanded[idx]
                                   or tick >= line.held_until[idx]):
                    targets.append(idx)
        new = [False, False]
        for idx in targets:
            if line.commanded[idx]:
                new[idx] = True
            elif line.last_start is None \
                    or tick >= line.last_start + 1:
                new[idx] = True
                line.last_start = tick
        if self.never_stops:
            new = [old or on for old, on in zip(line.commanded, new)]
        for idx in range(2):
            if line.commanded[idx] and not new[idx]:
                line.held_until[idx] = tick if self.no_holdout \
                    else tick + 2
            line.commanded[idx] = new[idx]
            self.plant.cmd[100 + idx] = new[idx]
            self._drive(peer, 40 + idx, new[idx])
            if new[idx] and not self.run_hours_frozen:
                line.run_hours[idx] += 1
        line.last_demand = line.demand
        line.values[210] = 0 if self.duty_mute else (
            0 if line.duty_index is None else line.duty_index + 1)
        line.values[211] = sum(1 for on in new if on)
        line.values[205] = line.demand
        nav = not any(avail)
        self._drive(peer, 217, nav)
        line.alarm_ticks = line.alarm_ticks + 1 if nav else 0
        self._drive(peer, 1033, line.alarm_ticks >= 5)
        latched = line.unack or line.alarm_ticks >= 5
        line.unack = latched if self.ack_stuck \
            else latched and not line.ack
        self._drive(peer, 1034, line.unack)

    def _advance(self, peer):
        """One completed scan on `peer`: pending role transitions
        settle, due receipts apply and journal, and the peer either
        executes the field-owning line or adopts the other peer's —
        the announced/configured checkpoint pull."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.tracking = True
            if self.owner == peer.name:
                self.owner = None
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            peer.role = 'active'
            self.owner = peer.name
            self.switched_once = True
            if self.peer_resets:
                peer.line = RotationLine()
            self._mark(peer, {'role_changed': {'from': 'promoting',
                                               'to': 'active'}})
        self._settle(peer)
        if peer.name == self.owner:
            self.plant.step()
            self._execute(peer)
        elif peer.role == 'standby' and peer.tracking:
            peer.line = copy.deepcopy(self._other(peer).line)

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def _sample(self, peer, point, value, kind):
        return {'point': point, 'direction': 'in',
                'sample': {'value': {kind: value},
                           'quality': 'good', 'tick': peer.tick}}

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        self._advance(peer)
        line = peer.line
        if (method, route) == ('GET', '/role'):
            role = peer.role
            if peer.name == 'a' and self.no_active:
                role = 'standby'
            report = {'role': role, 'tick': peer.tick}
            if role == 'standby':
                report['sync'] = {'tracking': {'aligned': peer.tick}} \
                    if peer.tracking and not self.no_tracking \
                    else {'unsynchronized': {}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {
                'points': [{'point': 10, 'name': 'level-primary',
                            'direction': 'in', 'value_type': 'float',
                            'writable': False}]
                if self.bare_signals else self.SIGNALS,
                'components': []}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': peer.tick, 'points': [
                # The inflow channel is undriven — the dynamics declare
                # the ambient inflow inside the net-flow sum, so point
                # 12 serves its unwritten zero.
                self._sample(peer, 12, 0.0, 'float'),
                self._sample(peer, 40, line.values.get(40, False),
                             'bool'),
                self._sample(peer, 41, line.values.get(41, False),
                             'bool'),
                self._sample(peer, 100, self.plant.cmd[100], 'bool'),
                self._sample(peer, 101, self.plant.cmd[101], 'bool'),
                self._sample(peer, 200, self.plant.level, 'float'),
                self._sample(peer, 205, line.values.get(205, 0),
                             'int'),
                self._sample(peer, 210, line.values.get(210, 0),
                             'int'),
                self._sample(peer, 211, line.values.get(211, 0),
                             'int'),
                self._sample(peer, 217, line.values.get(217, False),
                             'bool'),
                self._sample(peer, 302, line.oos[0], 'bool'),
                self._sample(peer, 334, line.oos[1], 'bool'),
                self._sample(peer, 1030, line.ack, 'bool'),
                self._sample(peer, 1033, line.values.get(1033, False),
                             'bool'),
                self._sample(peer, 1034, line.unack, 'bool')]}
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {'tick': peer.tick, 'components':
                         {'pump-group:3': line.pump_state()}}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [entry for entry in peer.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            write = (body or {}).get('command', {}).get('write_value')
            if not write or write.get('point') not in (302, 334, 1030):
                return 200, {'command': (body or {}).get('command'),
                             'outcome': {'rejected': {'reason': {
                                 'not_writable': {
                                     'point': (write or {})
                                     .get('point')}}}},
                             'actor': (body or {}).get('actor')}
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
            if self.promote_never \
                    or (self.restore_never and self.switched_once) \
                    or not peer.tracking:
                self._raise(409, {'not_converged':
                                  {'sync': {'unsynchronized': {}}}})
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        raise AssertionError('unexpected request %s %s' % (method, url))


class DutyRotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = RotationPlant()
        self.pair = RotationPair(self.plant)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self):
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', self.pair.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'ROTATION_POLL', 0.001), \
                patch.object(scenarios, 'ROTATION_DEADLINE', 2.0), \
                patch.object(scenarios, 'ROTATION_RISE_DEADLINE', 2.0), \
                patch.object(scenarios, 'ROTATION_CYCLE_DEADLINE', 2.0), \
                patch.object(scenarios, 'ROTATION_SWITCH_DEADLINE', 2.0):
            return scenarios.scenario_duty_rotation(ctx)

    def test_registered_in_scenarios(self):
        order = list(scenarios.SCENARIOS)
        # The restored pre-switch window behind the dead-peer case —
        # the settled tracking pair ahead of the force case's legs,
        # with the monitor-starvation leg sharing the window in front.
        self.assertLess(
            order.index(scenarios.scenario_dead_peer_latency),
            order.index(scenarios.scenario_duty_rotation))
        self.assertEqual(
            order.index(scenarios.scenario_duty_rotation) + 1,
            order.index(scenarios.scenario_force_carryover))
        self.assertIs(verify.case_function('duty-rotation'),
                      scenarios.scenario_duty_rotation)

    def test_clean_feed_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)
        # Every writable point and the pair's roles restored.
        self.assertEqual(self.pair.a.line.oos, [False, False])
        self.assertFalse(self.pair.a.line.ack)
        self.assertEqual(self.pair.a.role, 'active')
        self.assertEqual(self.pair.b.role, 'standby')
        self.assertTrue(self.pair.b.tracking)
        self.assertEqual(self.plant.cmd, {100: False, 101: False})
        notes = ' '.join(record['observations'])
        self.assertIn('the rotation alternated', notes)
        self.assertIn('the rotation position carried', notes)

    def test_two_runs_produce_identical_evidence(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        first = {path.name: path.read_bytes()
                 for path in self.evidence.iterdir()}
        pair, plant = self.pair, self.plant
        self.pair, self.plant = RotationPair(RotationPlant()), None
        try:
            second_run = self.run_scenario()
        finally:
            self.pair, self.plant = pair, plant
        self.assertEqual(second_run['outcome'], 'passed', second_run)
        second = {path.name: path.read_bytes()
                  for path in self.evidence.iterdir()}
        self.assertEqual(first, second)

    def test_mirrored_layout_passes(self):
        # The post-failover layout: ctrl-b owns the field, ctrl-a
        # follows — the same legs mirrored, restored b-active.
        self.pair.a.role = 'standby'
        self.pair.a.tracking = True
        self.pair.b.role = 'active'
        self.pair.owner = 'b'
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.pair.b.role, 'active')
        self.assertEqual(self.pair.a.role, 'standby')
        report.validate_scenario(record)

    def test_no_active_peer_fails(self):
        self.pair.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unconverged_pair_is_inconclusive(self):
        self.pair.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never reported tracking convergence',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_missing_wiring_is_inconclusive(self):
        self.pair.bare_signals = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('lacks the rotation leg', record.get('detail', ''))
        report.validate_scenario(record)

    def test_undeclared_inflow_is_inconclusive(self):
        # No ambient inflow declared in the net-flow sum: the held
        # level never rises unopposed past start.
        self.plant.inflow_declared = False
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('rotation-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('never raised the level', record.get('detail', ''))
        report.validate_scenario(record)

    def test_level_never_rising_is_inconclusive(self):
        self.plant.never_rises = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('rotation-nondeterministic',
                      record.get('detail', ''))
        self.assertIn('never raised the level',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_duty_never_moving_is_inconclusive(self):
        # The staged command reaches the field but the served duty
        # output never moves off zero — the rig cannot prove rotation.
        self.pair.duty_mute = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('duty never moved off', record.get('detail', ''))
        report.validate_scenario(record)

    def test_group_never_staging_fails(self):
        self.pair.never_stages = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('never staged a duty pump',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_holder_never_stopping_fails(self):
        self.pair.never_stops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('never stopped at stop', record.get('detail', ''))
        report.validate_scenario(record)

    def test_rotation_never_alternating_fails(self):
        self.pair.no_alternate = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('did not rotate', record.get('detail', ''))
        report.validate_scenario(record)

    def test_holdout_never_banked_fails(self):
        self.pair.no_holdout = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('does not extend past the journaled stop',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_run_hours_never_banked_fails(self):
        self.pair.run_hours_frozen = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('no run-hours banked', record.get('detail', ''))
        report.validate_scenario(record)

    def test_promoted_peer_resetting_fails(self):
        # The promoted peer reinitializes instead of adopting the
        # checkpoint — duty, the in-flight command, and the banked
        # run-hours all reset.
        self.pair.peer_resets = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('lost the rotation position',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_promotion_never_settling_fails(self):
        self.pair.promote_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promotion never settled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_restore_never_completing_fails(self):
        self.pair.restore_never = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not restored', record.get('detail', ''))
        report.validate_scenario(record)

    def test_journal_never_recording_fails(self):
        self.pair.no_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('rotation-failed', record.get('detail', ''))
        self.assertIn('no journaled run-contact stop',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_settlements_never_journaling_fails(self):
        self.pair.no_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journaled evidence missing',
                      record.get('detail', ''))
        self.assertIn('no settled receipt journaled',
                      record.get('detail', ''))
        report.validate_scenario(record)

    def test_unack_latch_never_clearing_fails(self):
        self.pair.ack_stuck = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('unacknowledged latch never cleared',
                      record.get('detail', ''))
        report.validate_scenario(record)


class UnclaimedRearmTests(unittest.TestCase):
    """The unclaimed-rearm scenario against the stubbed pair: a clean
    rig passes with identical digests and evidence, each doctored
    defect reports the named diagnostic, and the unreachable rig is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.plant = RearmPlantPeer(UnclaimedRearmFeed.OWNER)
        self.feed = UnclaimedRearmFeed(self.plant)

    def tearDown(self):
        self.plant.close()
        self.tmp.cleanup()

    def _ctx(self):
        return {'active': 'http://ctrl-a:1',
                'standby': 'http://ctrl-b:2',
                'revised': 'http://ctrl-c:3',
                'plant': self.plant.address,
                'plant_ctl': self.plant.ctl,
                'plant_owner': {'active': self.feed.OWNER,
                                'standby': 424244},
                'evidence_dir': str(self.evidence)}

    def run_scenario(self, feed=None, ctx=None):
        feed = feed or self.feed
        ctx = ctx or self._ctx()
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'UNCLAIMED_REARM_SETTLE', 2), \
                patch.object(scenarios, 'UNCLAIMED_REARM_POLL', 0.001), \
                patch.object(scenarios, 'UNCLAIMED_REARM_DEADLINE', 2), \
                patch.object(scenarios, 'UNCLAIMED_REARM_ROUNDS', 4):
            return scenarios.scenario_unclaimed_rearm(ctx)

    def test_registered(self):
        self.assertIn(scenarios.scenario_unclaimed_rearm,
                      scenarios.SCENARIOS)
        self.assertIs(verify.case_function('unclaimed-rearm'),
                      scenarios.scenario_unclaimed_rearm)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertTrue((self.evidence
                         / 'unclaimed-rearm-pass-1.json').is_file())
        self.assertTrue((self.evidence
                         / 'unclaimed-rearm-pass-2.json').is_file())
        report.validate_scenario(record)

    def test_demoting_window_reports_failed(self):
        self.feed.demote_on_unclaimed = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('role=active', record['detail'])
        report.validate_scenario(record)

    def test_rearm_dropping_write_reports_failed(self):
        self.feed.rearm_drops = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('never landed', record['detail'])
        report.validate_scenario(record)

    def test_phantom_rearm_reports_failed(self):
        self.plant.phantom_rearm = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('conditional claim', record['detail'])
        report.validate_scenario(record)

    def test_open_field_reports_failed(self):
        self.plant.open_field = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('no standing writer claim', record['detail'])
        report.validate_scenario(record)

    def test_ledger_growth_reports_failed(self):
        self.feed.ledger_grows = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('fencing-loss ledger', record['detail'])
        report.validate_scenario(record)

    def test_journaled_claim_loss_reports_failed(self):
        self.feed.claim_lost_journaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('field_claim_lost', record['detail'])
        report.validate_scenario(record)

    def test_journaled_role_change_reports_failed(self):
        self.feed.role_journaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('role transition', record['detail'])
        report.validate_scenario(record)

    def test_peer_role_move_reports_failed(self):
        self.feed.peer_moves = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-failed'), record['detail'])
        self.assertIn('tracking peer', record['detail'])
        report.validate_scenario(record)

    def test_shared_claim_reports_nondeterministic(self):
        self.plant.shared = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-nondeterministic'), record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'writes': 'landed'}, {}, {'pass': 1}),
                       ({'writes': 'stalled'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_unclaimed_rearm_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'unclaimed-rearm-nondeterministic'), record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_release_refusal_reports_inconclusive(self):
        self.plant.release_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('hand-back', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.silent = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_unsettled_pair_reports_inconclusive(self):
        self.feed.peer_wrong_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never settled', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_plant_reports_inconclusive(self):
        self.plant.down = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        report.validate_scenario(record)

    def test_missing_plant_endpoint_reports_inconclusive(self):
        ctx = self._ctx()
        del ctx['plant']
        record = self.run_scenario(ctx=ctx)
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('claim ops', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            plant = RearmPlantPeer(UnclaimedRearmFeed.OWNER)
            feed = UnclaimedRearmFeed(plant)
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.plant, self.evidence = plant, evidence
            try:
                record = self.run_scenario(feed=feed)
            finally:
                plant.close()
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


class DemoteSettlePeer:
    """One endpoint of the settle pair: role, tracking posture, its
    adopted receipt log, the journaled settlements it has already
    recorded, and the served image."""

    def __init__(self, name):
        self.name = name
        self.role = 'standby'  # active | standby | demoting | promoting
        self.tracking = False
        self.tick = 0
        self.attempts = 0      # submission high-water — the adopted
                               # window's coverage
        self.receipts = []     # the peer's served receipt log
        self.journal = []
        self.next_seq = 1
        self.image = {}        # served writable-point values
        self.phantom = {}      # values the fenced image minted — the
                               # phantom-application doctor's residue
        self.journaled = set()  # (command, actor) keys already settled


class DemoteSettleFeed:
    """A stubbed pair for the demote-settle-uniqueness leg: ctrl-a owns
    the field at launch, ctrl-b tracks. Every endpoint call is one
    scan — the owner applies its pending admissions at the boundary
    and journals their settlements, the demote closes the gate so a
    still-accepted admission suspends, the promote's final-sync
    transfer carries the demoted log across, and a tracking peer's
    adoption journals the line's verdict — applied for the admissions
    the carry landed, superseded for the ones the adopted window
    passed. Doctor flags stage each named defect the issue calls
    out."""

    POINTS = (302, 300, 301, 332, 333, 334)
    SIGNALS = [{'point': point,
                'name': 'p101-oos' if point == 302
                        else 'pt-' + str(point),
                'direction': 'in', 'value_type': 'bool',
                'writable': True} for point in POINTS]

    def __init__(self):
        self.a = DemoteSettlePeer('a')
        self.a.role = 'active'
        self.b = DemoteSettlePeer('b')
        self.b.tracking = True
        # The doctors staging each named defect.
        self.double_settle = False    # an admission journals applied
                                      # AND superseded
        self.drop_carry = False       # the last raced admission misses
                                      # the carry — settles superseded
        self.drop_admission = False   # the raced admissions vanish —
                                      # no settle anywhere
        self.phantom_apply = False    # a superseded admission's value
                                      # lands on the demoted image
        self.diverge_logs = False     # the demoted peer's adopted log
                                      # disagrees with its journal
        self.demote_refused = False   # every demote is refused
        self.promote_refused = False  # every promote is refused
        self.no_tracking = False      # the standby never converges
        self.no_active = False        # no peer reports active
        self.unreachable = False      # ctrl-b's monitor never answers
        self._doubled = False

    def _peers(self):
        return {'a': self.a, 'b': self.b}

    def _other(self, peer):
        return self.b if peer.name == 'a' else self.a

    @staticmethod
    def _key(receipt):
        return json.dumps([receipt.get('command'),
                           receipt.get('actor')], sort_keys=True)

    def _mark(self, peer, event):
        peer.journal.append({'seq': peer.next_seq, 'tick': peer.tick,
                             'event': event})
        peer.next_seq += 1

    def _settle(self, peer, receipt):
        """One receipt's terminal journaling — the recorder's
        outcome-diff: journaled once per peer per admission."""
        key = self._key(receipt)
        if key in peer.journaled:
            return
        peer.journaled.add(key)
        self._mark(peer, {'command_settled': {'receipt':
                                              dict(receipt)}})

    def _observe(self, peer):
        """The scan's receipt diff: every terminal outcome the log
        newly carries journals on this peer."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome']:
                self._settle(peer, receipt)

    def _apply(self, peer):
        """The owner's scan boundary: pending admissions apply and
        settle."""
        for receipt in peer.receipts:
            if 'accepted' not in receipt['outcome']:
                continue
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': peer.tick}}
            peer.image[write['point']] = write['value']['bool']
            self._settle(peer, receipt)

    def _adopt(self, peer, other):
        """The tracking pull: the active successor's log replaces the
        peer's own; suspended admissions the carry landed journal the
        line's verdict, the ones the adopted window passed settle
        superseded, and anything beyond the window stays suspended."""
        if other.role != 'active':
            return
        suspended = [receipt for receipt in peer.receipts
                     if 'accepted' in receipt['outcome']]
        peer.receipts = copy.deepcopy(other.receipts)
        peer.attempts = other.attempts
        peer.image = dict(other.image)
        for receipt in suspended:
            if any(self._key(carried) == self._key(receipt)
                   for carried in peer.receipts):
                continue            # carried — the adopted copy
                                    # journals the line's verdict
            if self.drop_admission:
                continue            # the admission vanishes unsettled
            if receipt['index'] < peer.attempts:
                self._settle(peer, {
                    'command': receipt['command'],
                    'actor': receipt.get('actor'),
                    'index': receipt['index'],
                    'outcome': {'rejected': {'reason': {
                        'superseded': {
                            'point': receipt['command']
                            ['write_value']['point']}}}}})
                if self.phantom_apply:
                    # The demoted run's fenced image minted the write
                    # the journal superseded — and keeps showing it
                    # past the adopted image.
                    write = receipt['command']['write_value']
                    peer.phantom[write['point']] = \
                        write['value']['bool']
            else:
                peer.receipts.append(receipt)
        peer.image.update(peer.phantom)
        self._observe(peer)
        if self.double_settle and not self._doubled:
            raced = [receipt for receipt in peer.receipts
                     if 'applied' in receipt['outcome']
                     and str(receipt.get('actor'))
                     .startswith('qa-lane-settle')]
            if raced:
                self._doubled = True
                receipt = raced[0]
                # The #685 defect: the same admission journaled
                # superseded beside its applied settle.
                self._mark(peer, {'command_settled': {'receipt': {
                    'command': receipt['command'],
                    'actor': receipt.get('actor'),
                    'index': receipt.get('index'),
                    'outcome': {'rejected': {'reason': {
                        'superseded': {
                            'point': receipt['command']
                            ['write_value']['point']}}}}}}})
        if self.diverge_logs:
            raced = [receipt for receipt in peer.receipts
                     if str(receipt.get('actor'))
                     .startswith('qa-lane-settle')
                     and 'applied' in receipt['outcome']]
            if raced:
                raced[0]['outcome'] = {'rejected': {'reason': {
                    'superseded': {
                        'point': raced[0]['command']
                        ['write_value']['point']}}}}

    def _advance(self, peer, adopt=True):
        """One completed scan: pending role transitions settle, the
        owner applies its pending admissions, and a tracking peer
        pulls the active successor's checkpoint."""
        peer.tick += 1
        if peer.role == 'demoting':
            peer.role = 'standby'
            peer.tracking = True
            self._mark(peer, {'role_changed': {'from': 'demoting',
                                               'to': 'standby'}})
        elif peer.role == 'promoting':
            peer.role = 'active'
            self._mark(peer, {'role_changed': {'from': 'promoting',
                                               'to': 'active'}})
        if peer.role == 'active':
            self._apply(peer)
        elif peer.role == 'standby' and peer.tracking and adopt:
            self._adopt(peer, self._other(peer))
        self._observe(peer)

    def _raise(self, code, body):
        raise urllib.error.HTTPError(
            'http://pair', code, 'refused', None,
            io.BytesIO(json.dumps(body).encode()))

    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('://', 1)[1].split(':')[0]
        peer = self._peers()[host.split('-', 1)[1]]
        if self.unreachable and peer.name == 'b':
            raise urllib.error.URLError('unreachable')
        path = '/' + url.split('/', 3)[3]
        route, _, query = path.partition('?')
        if (method, route) == ('POST', '/demote'):
            # The gate closes at the request boundary: the demote
            # lands before the quiesced scans that follow it.
            if peer.role != 'active' or self.demote_refused:
                self._raise(409, {'not_active': {}})
            peer.role = 'demoting'
            self._advance(peer, adopt=False)
            return 200, {'role': 'demoting'}
        self._advance(
            peer,
            adopt=not (method == 'POST' and route == '/promote'))
        if (method, route) == ('GET', '/role'):
            role = 'standby' if peer.name == 'a' and self.no_active \
                else peer.role
            report = {'role': role, 'tick': peer.tick}
            if role == 'standby':
                report['sync'] = {'tracking': {'aligned': peer.tick}} \
                    if peer.tracking and not self.no_tracking \
                    else {'unsynchronized': {}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': list(self.SIGNALS),
                         'components': []}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': peer.tick, 'points': [
                {'point': point, 'direction': 'in',
                 'sample': {'value': {'bool': peer.image.get(point,
                                                           False)},
                            'quality': 'good', 'tick': peer.tick}}
                for point in self.POINTS]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(peer.receipts)
        if (method, route) == ('GET', '/journal'):
            since = int(query.split('=', 1)[1]) if '=' in query else 0
            return 200, [dict(entry) for entry in peer.journal
                         if entry['seq'] > since]
        if (method, route) == ('POST', '/command'):
            receipt = {'command': (body or {}).get('command'),
                       'actor': (body or {}).get('actor'),
                       'index': peer.attempts,
                       'outcome': {'accepted': {
                           'apply_tick': peer.tick + 1}}}
            if peer.role != 'active':
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {}}}}
                self._settle(peer, receipt)
            else:
                peer.attempts += 1
                peer.receipts.append(receipt)
            # The wire answer is the admission's snapshot — later
            # scans settling the logged receipt do not rewrite it.
            return 200, copy.deepcopy(receipt)
        if (method, route) == ('POST', '/promote'):
            if peer.role == 'active':
                self._raise(409, {'already_active': {}})
            if not peer.tracking or self.promote_refused:
                self._raise(409, {'not_converged': {
                    'sync': {'unsynchronized': {}}}})
            other = self._other(peer)
            # The promote boundary's final-sync transfer: the demoted
            # peer's whole log — settled receipts and the suspended
            # admissions the carry still owes a verdict.
            peer.receipts = copy.deepcopy(other.receipts)
            peer.attempts = other.attempts
            peer.image = dict(other.image)
            if self.drop_carry or self.drop_admission:
                pending = [receipt for receipt in peer.receipts
                           if 'accepted' in receipt['outcome']]
                doomed = pending if self.drop_admission \
                    else pending[-1:]
                for receipt in doomed:
                    peer.receipts.remove(receipt)
            peer.role = 'promoting'
            return 200, {'role': 'promoting'}
        raise AssertionError('unexpected request %s %s'
                             % (method, url))


class DemoteSettleTests(unittest.TestCase):
    """The demote-settle-uniqueness leg against the stubbed pair: a
    clean rig passes with identical digests and evidence — raced
    admissions settling either legal single outcome — each doctored
    defect reports the named diagnostic, and an unreachable peer is
    inconclusive."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.feed = DemoteSettleFeed()

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1',
               'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence)}
        with patch.object(scenarios, 'http_json', feed.http_json), \
                patch.object(scenarios, 'POLL_INTERVAL', 0.001), \
                patch.object(scenarios, 'DEMOTE_SETTLE_SETTLE', 2.0), \
                patch.object(scenarios, 'DEMOTE_SETTLE_AUDIT', 2.0), \
                patch.object(scenarios, 'DEMOTE_SETTLE_POLL', 0.001):
            return scenarios.scenario_demote_settle_uniqueness(ctx)

    def test_registered(self):
        order = list(scenarios.SCENARIOS)
        # The restored pre-switch window behind the standby-loss case —
        # the settled tracking pair ahead of the tune case's a->b
        # switch.
        self.assertLess(
            order.index(scenarios.scenario_standby_loss),
            order.index(scenarios.scenario_demote_settle_uniqueness))
        self.assertEqual(
            order.index(scenarios.scenario_demote_settle_uniqueness)
            + 1,
            order.index(scenarios.scenario_parameter_tune_carryover))
        self.assertIs(
            verify.case_function('demote-settle-uniqueness'),
            scenarios.scenario_demote_settle_uniqueness)

    def test_clean_pair_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        for name in ('demote-settle-signals.json',
                     'demote-settle-pass-1.json',
                     'demote-settle-pass-2.json'):
            self.assertTrue((self.evidence / name).is_file(), name)
        passes = [json.loads((self.evidence / name).read_text())
                  for name in ('demote-settle-pass-1.json',
                               'demote-settle-pass-2.json')]
        self.assertEqual(passes[0]['digest'], passes[1]['digest'])
        self.assertEqual(passes[0]['digest']['outcomes'], 'single')
        self.assertEqual(passes[0]['digest']['roles'], 'restored')
        report.validate_scenario(record)

    def test_superseded_admission_is_a_legal_single_outcome(self):
        self.feed.drop_carry = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        passed = json.loads(
            (self.evidence / 'demote-settle-pass-1.json').read_text())
        outcomes = {
            scenarios._outcome_key(receipt)
            for audit in passed['audit'] if audit['window']
            for entries in audit['window']['journaled'].values()
            for receipt in entries}
        self.assertIn('rejected:superseded', outcomes, outcomes)
        report.validate_scenario(record)

    def test_double_settled_admission_reports_nondeterministic(self):
        self.feed.double_settle = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        report.validate_scenario(record)

    def test_vanished_admission_reports_failed(self):
        self.feed.drop_admission = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-failed'), record['detail'])
        self.assertIn('terminal journaled outcome', record['detail'])
        report.validate_scenario(record)

    def test_phantom_application_reports_nondeterministic(self):
        self.feed.drop_carry = True
        self.feed.phantom_apply = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        self.assertIn('application the journal never settled',
                      record['detail'])
        report.validate_scenario(record)

    def test_diverged_logs_report_nondeterministic(self):
        self.feed.diverge_logs = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        self.assertIn('adopted log', record['detail'])
        report.validate_scenario(record)

    def test_refused_demote_reports_failed(self):
        self.feed.demote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-failed'), record['detail'])
        self.assertIn('demote', record['detail'])
        report.validate_scenario(record)

    def test_refused_promote_reports_failed(self):
        self.feed.promote_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-failed'), record['detail'])
        self.assertIn('promote', record['detail'])
        report.validate_scenario(record)

    def test_no_active_reports_failed(self):
        self.feed.no_active = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('no peer reports role=active', record['detail'])
        report.validate_scenario(record)

    def test_unconverged_pair_reports_inconclusive(self):
        self.feed.no_tracking = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('tracking standby', record['detail'])
        report.validate_scenario(record)

    def test_unreachable_peer_reports_inconclusive(self):
        self.feed.unreachable = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('unreachable', record['detail'])
        report.validate_scenario(record)

    def test_diverging_digests_report_nondeterministic(self):
        passes = iter([({'outcomes': 'single'}, {}, {'pass': 1}),
                       ({'outcomes': 'diverged'}, {}, {'pass': 2})])
        with patch.object(scenarios, '_demote_settle_pass',
                          lambda *a: next(passes)):
            record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertTrue(record['detail'].startswith(
            'demote-settle-uniqueness-nondeterministic'),
            record['detail'])
        self.assertIn('digests diverged', record['detail'])
        report.validate_scenario(record)

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for _ in range(2):
            feed = DemoteSettleFeed()
            evidence = Path(self.tmp.name) / ('run' + str(len(runs)))
            evidence.mkdir()
            self.evidence = evidence
            record = self.run_scenario(feed=feed)
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


class StandbyLossFeed:
    """A stubbed pair for the standby-loss scenario. ctrl-a owns the
    field — every GET on it is one completed scan: the tick advancing,
    a queued command applying at the boundary, its settlement
    journaled — while ctrl-b is the tracking standby the legs stop,
    lose, and return. Each peer's --journal-file is a real append-only
    record the feed writes: a command_settled echo for every receipt
    each side records — the standby's not_active refusals included —
    and a role_changed entry for each transition. After a start the
    returned standby's cadence converges it to tracking across its
    first /role polls, and POST /promote answers the cadence's
    standing — not_converged before the first applied transfer — so
    the scenario's first answered promote is deterministically the
    refusal. Every transition is call-count keyed — never wall-clock —
    so two scenario runs emit identical evidence. Fault flags stage
    each named failure the issue calls out."""

    def __init__(self, journal_a, journal_b):
        self.tick = 200
        self.point = False
        self.pending = []            # ctrl-a's accepted receipts
        self.receipts_a = []         # ctrl-a's adopted receipt log
        self.receipts_b = []         # ctrl-b's adopted receipt log
        self.a_role = 'active'
        self.a_sync = None           # standing while a is a standby
        self.a_track_left = 0        # /role polls until a demoted a tracks
        self.b_role = 'standby'
        self.b_sync = 'tracking'
        self.b_up = True
        self.b_track_left = 0        # /role polls until a returned b tracks
        self.stops = []
        self.starts = []
        self.seq_a = 1
        self.seq_b = 1
        self.path_a = Path(journal_a)
        self.path_b = Path(journal_b)
        self._append(self.path_a, {'run_boundary': {'run': 1, 'tick': 0}})
        self._append(self.path_b, {'run_boundary': {'run': 1, 'tick': 0}})
        # Fault injection — each named failure the issue calls out.
        self.refusal_wrong = False    # the standby answers not not_active
        self.refusal_applies = False  # the refused write reaches the field
        self.leak_receipts = False    # the refused write enters a receipt log
        self.phantom_journal = False  # the refused write lands on a's journal
        self.misnamed_journal = False # b journals it as something else
        self.stall = False            # a's tick stops while b is down
        self.role_move = False        # a reports non-active while b is down
        self.command_refused = False  # the window command is rejected
        self.command_lost = False     # the window command never settles
        self.command_unjournaled = False  # the settle never journals
        self.spurious_role = False    # a's journal gains role_changed mid-window
        self.stop_leaks = False       # b keeps answering while "down"
        self.stop_fails = False       # the stop action raises
        self.start_fails = False      # the start action raises
        self.never_returns = False    # b's monitor never answers again
        self.never_tracks = False     # b answers but never converges
        self.gate_open = False        # the first promote answers 200
        self.gate_other = False       # the first promote answers another refusal
        self.promote_refuses = False  # a tracking promote answers a refusal
        self.demote_refuses = False   # a demote answers a refusal
        self.never_settles = False    # the pair never settles post-restore

    def _append(self, path, record):
        with path.open('a') as stream:
            stream.write(json.dumps(record) + '\n')

    def _entry_a(self, event):
        entry = {'seq': self.seq_a, 'tick': self.tick, 'event': event}
        self._append(self.path_a, {'entry': entry})
        self.seq_a += 1

    def _entry_b(self, event):
        entry = {'seq': self.seq_b, 'tick': self.tick, 'event': event}
        self._append(self.path_b, {'entry': entry})
        self.seq_b += 1

    def _conflict(self, url, payload):
        return urllib.error.HTTPError(
            url, 409, 'conflict', {},
            io.BytesIO(json.dumps(payload).encode()))

    def _scan_a(self):
        """One completed scan on the field writer — the tick advances
        and the boundary's command phase applies the queued receipts,
        journaling each settlement."""
        if not (self.stall and not self.b_up):
            self.tick += 1
        if self.command_lost:
            return
        for receipt in self.pending:
            write = receipt['command']['write_value']
            receipt['outcome'] = {'applied': {'tick': self.tick}}
            new = write['value']['bool']
            if new != self.point:
                self._entry_a({'point_changed': {
                    'point': write['point'],
                    'from': {'bool': self.point},
                    'to': {'bool': new}}})
                self.point = new
            if not self.command_unjournaled:
                self._entry_a({'command_settled': {'receipt': receipt}})
        self.pending = []

    def _demote_a(self):
        self.a_role = 'standby'
        self.a_sync = 'unsynchronized'
        self.a_track_left = 2
        self._entry_a({'role_changed': {'from': 'active',
                                        'to': 'standby'}})

    def _demote_b(self):
        self.b_role = 'standby'
        self.b_sync = 'unsynchronized'
        self.b_track_left = 10 ** 9 if self.never_settles else 2
        self._entry_b({'role_changed': {'from': 'active',
                                        'to': 'standby'}})

    def _a_poll(self):
        # The demoted writer's paced pulls converge it to tracking —
        # each /role poll models one tracking cadence.
        if self.a_role == 'standby' and self.a_track_left > 0:
            self.a_track_left -= 1
            if self.a_track_left == 0:
                self.a_sync = 'tracking'

    def _b_poll(self):
        if self.b_role == 'standby' and self.b_track_left > 0:
            self.b_track_left -= 1
            if self.b_track_left == 0 and not self.never_tracks:
                self.b_sync = 'tracking'

    def _b_sync_report(self):
        return self.b_sync

    # The runner-owned lifecycle actions — replace ctx['stop_controller']
    # and ctx['start_controller']: the rig launches with --restart no, so
    # the stop holds a real down-window until the start.
    def stop(self, name):
        self.stops.append(name)
        if self.stop_fails:
            raise RuntimeError('docker stop failed: no such container')
        if self.spurious_role:
            self._entry_a({'role_changed': {'from': 'active',
                                            'to': 'standby'}})
        if not self.stop_leaks:
            self.b_up = False

    def start(self, name):
        self.starts.append(name)
        if self.start_fails:
            raise RuntimeError('docker start failed: no such container')
        if self.never_returns:
            return
        self.b_up = True
        self.b_role = 'standby'
        self.b_sync = 'unsynchronized'
        self.b_track_left = 3

    def _a(self, method, route, url, body):
        if method == 'GET':
            self._scan_a()
        if (method, route) == ('GET', '/role'):
            report = {'role': self.a_role, 'tick': self.tick}
            if self.a_role == 'standby':
                self._a_poll()
                report['sync'] = self.a_sync or 'unsynchronized'
            if self.role_move and not self.b_up \
                    and self.a_role == 'active':
                report['role'] = 'standby'
                report['sync'] = {'tracking': {'aligned': self.tick}}
            return 200, report
        if (method, route) == ('GET', '/signals'):
            return 200, {'points': [
                {'point': 10, 'signal': None, 'name': 'p101-oos',
                 'direction': 'in', 'value_type': 'bool',
                 'writable': True}]}
        if (method, route) == ('GET', '/snapshot'):
            return 200, {'tick': self.tick, 'points': [
                {'point': 10, 'sample': {
                    'value': {'bool': self.point},
                    'quality': 'good'}}]}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts_a)
        if (method, route) == ('POST', '/command'):
            if self.command_refused:
                receipt = {'command': body['command'],
                           'outcome': {'rejected': {'reason': {
                               'queue_full': {'depth': 8}}}},
                           'actor': body.get('actor')}
            else:
                receipt = {'command': body['command'],
                           'outcome': {'accepted': {
                               'apply_tick': self.tick + 1}},
                           'actor': body.get('actor')}
                self.pending.append(receipt)
            self.receipts_a.append(receipt)
            return 200, receipt
        if (method, route) == ('POST', '/demote'):
            if self.a_role != 'active':
                raise self._conflict(url, {'not_active': {}})
            self._demote_a()
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if self.a_role == 'active':
                raise self._conflict(url, 'already_active')
            if self.a_sync != 'tracking' or self.promote_refuses:
                raise self._conflict(url, {'not_converged': {
                    'sync': self.a_sync or 'unsynchronized'}})
            self.a_role = 'active'
            self.a_sync = None
            if self.b_role == 'active':
                self._demote_b()
            self._entry_a({'role_changed': {'from': 'standby',
                                            'to': 'active'}})
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))

    def _b(self, method, route, url, body):
        if not self.b_up:
            raise urllib.error.URLError('connection refused')
        if (method, route) == ('GET', '/role'):
            self._b_poll()
            report = {'role': self.b_role, 'tick': self.tick}
            if self.b_role == 'standby':
                report['sync'] = self._b_sync_report()
            return 200, report
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts_b)
        if (method, route) == ('POST', '/command'):
            receipt = {'command': body['command'],
                       'actor': body.get('actor')}
            if self.refusal_wrong:
                receipt['outcome'] = {'accepted': {
                    'apply_tick': self.tick + 1}}
            else:
                receipt['outcome'] = {'rejected': {'reason': {
                    'not_active': {'point': body['command']
                                   ['write_value']['point'],
                                   'role': 'standby'}}}}
            echo = receipt
            if self.misnamed_journal:
                echo = dict(receipt)
                echo['outcome'] = {'applied': {'tick': self.tick}}
            self._entry_b({'command_settled': {'receipt': echo}})
            if self.refusal_applies:
                self.point = body['command']['write_value'] \
                    ['value']['bool']
            if self.leak_receipts:
                self.receipts_a.append(dict(receipt))
            if self.phantom_journal:
                self._entry_a({'command_settled': {'receipt': receipt}})
            return 200, receipt
        if (method, route) == ('POST', '/demote'):
            if self.b_role != 'active' or self.demote_refuses:
                raise self._conflict(url, {'not_active': {}})
            self._demote_b()
            return 200, {'role': 'demoting', 'tick': self.tick}
        if (method, route) == ('POST', '/promote'):
            if self.b_role == 'active':
                raise self._conflict(url, 'already_active')
            if not self.gate_open \
                    and self._b_sync_report() != 'tracking':
                payload = {'no_tracking_source': {}} \
                    if self.gate_other else {'not_converged': {
                        'sync': self._b_sync_report()}}
                raise self._conflict(url, payload)
            if self.promote_refuses:
                raise self._conflict(url, {'not_converged': {
                    'sync': 'unsynchronized'}})
            self.b_role = 'active'
            self._entry_b({'role_changed': {'from': 'standby',
                                            'to': 'active'}})
            if self.a_role == 'active':
                self._demote_a()
            return 200, {'role': 'promoting', 'tick': self.tick}
        raise AssertionError('unexpected request %s %s' % (method, url))

    # The monitor channel — replaces scenarios.http_json.
    def http_json(self, method, url, body=None, timeout=10):
        host = url.split('/')[2]
        path = '/' + url.split('/', 3)[3]
        route, _, _query = path.partition('?')
        peer = {'ctrl-a:1': 'a', 'ctrl-b:2': 'b'}[host]
        if peer == 'b':
            return self._b(method, route, url, body)
        return self._a(method, route, url, body)


class StandbyLossTests(unittest.TestCase):
    """scenario_standby_loss against the stubbed pair: the four legs —
    the named not_active refusal with no field effect, the down-window
    non-interference, the reconvergence wait, and the promotion gate
    ahead of the restored role assignment — plus each named failure
    and inconclusive induction the issue calls out."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.evidence = Path(self.tmp.name) / 'evidence'
        self.evidence.mkdir()
        self.journal_a = Path(self.tmp.name) / 'controllers' / 'a' \
            / 'journal.jsonl'
        self.journal_a.parent.mkdir(parents=True)
        self.journal_b = Path(self.tmp.name) / 'controllers' / 'b' \
            / 'journal.jsonl'
        self.journal_b.parent.mkdir(parents=True)
        self.feed = StandbyLossFeed(self.journal_a, self.journal_b)

    def tearDown(self):
        self.tmp.cleanup()

    def run_scenario(self, feed=None, **ctx_overrides):
        feed = feed or self.feed
        ctx = {'active': 'http://ctrl-a:1', 'standby': 'http://ctrl-b:2',
               'evidence_dir': str(self.evidence),
               'stop_controller': feed.stop,
               'start_controller': feed.start,
               'journal_files': {'active': str(self.journal_a),
                                 'standby': str(self.journal_b)}}
        ctx.update(ctx_overrides)
        defaults = {'POLL_INTERVAL': 0.001, 'STANDBY_LOSS_POLL': 0.001,
                    'STANDBY_LOSS_PROBE': 0.001,
                    'STANDBY_LOSS_RETURN_DEADLINE': 0.3,
                    'STANDBY_LOSS_SETTLE_DEADLINE': 0.5}
        with patch.object(scenarios, 'http_json', feed.http_json):
            for key, value in defaults.items():
                patcher = patch.object(scenarios, key, value)
                patcher.start()
                self.addCleanup(patcher.stop)
            return scenarios.scenario_standby_loss(ctx)

    def test_clean_run_passes_and_validates(self):
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'passed', record)
        self.assertEqual(self.feed.stops, ['standby'])
        self.assertEqual(self.feed.starts, ['standby'])
        # The pair ends on its pre-scenario role assignment.
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')
        self.assertEqual(self.feed.b_sync, 'tracking')
        report.validate_scenario(record)
        for entry in record['evidence']:
            self.assertTrue((self.evidence.parent
                             / entry['ref']).exists(), entry)

    def test_refusal_named_wrong_fails(self):
        # The standby-directed command answering anything but the
        # named not_active rejection is the first named failure.
        self.feed.refusal_wrong = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_active', record['detail'])
        report.validate_scenario(record)

    def test_refused_write_reaching_the_field_fails(self):
        self.feed.refusal_applies = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('reached the field', record['detail'])

    def test_refused_write_in_receipt_log_fails(self):
        self.feed.leak_receipts = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('receipt', record['detail'])

    def test_refused_write_on_active_journal_fails(self):
        self.feed.phantom_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record['detail'])

    def test_refusal_journaled_misnamed_fails(self):
        self.feed.misnamed_journal = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('named', record['detail'])

    def test_stalled_scan_while_standby_down_fails(self):
        self.feed.stall = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('stalled', record['detail'])
        # The induction still unwinds: the peer is started back.
        self.assertEqual(self.feed.starts, ['standby'])

    def test_role_move_while_standby_down_fails(self):
        self.feed.role_move = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('standby', record['detail'])

    def test_refused_window_command_fails(self):
        self.feed.command_refused = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('refused', record['detail'])

    def test_unsettled_window_command_fails(self):
        self.feed.command_lost = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never settled', record['detail'])

    def test_unjournaled_window_command_fails(self):
        self.feed.command_unjournaled = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('journal', record['detail'])

    def test_spurious_role_entry_fails(self):
        self.feed.spurious_role = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('transition', record['detail'])

    def test_stop_not_holding_is_inconclusive(self):
        self.feed.stop_leaks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('did not hold', record['detail'])

    def test_failing_stop_action_is_inconclusive(self):
        self.feed.stop_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_failing_start_action_is_inconclusive(self):
        self.feed.start_fails = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_missing_lifecycle_actions_are_inconclusive(self):
        record = self.run_scenario(stop_controller=None,
                                 start_controller=None)
        self.assertEqual(record['outcome'], 'inconclusive', record)

    def test_standby_never_returns_is_inconclusive(self):
        self.feed.never_returns = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'inconclusive', record)
        self.assertIn('never answered', record['detail'])

    def test_standby_never_tracks_fails(self):
        self.feed.never_tracks = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('never reached tracking', record['detail'])

    def test_promotion_without_gate_refusal_fails(self):
        # The returning standby promoting on its first answered
        # request — no not_converged refusal precedes the switch — is
        # the gate failure the issue names; the pair still restores.
        self.feed.gate_open = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record['detail'])
        self.assertEqual(self.feed.a_role, 'active')
        self.assertEqual(self.feed.b_role, 'standby')

    def test_promotion_gate_other_refusal_fails(self):
        self.feed.gate_other = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('not_converged', record['detail'])

    def test_tracking_promotion_refused_fails(self):
        self.feed.promote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('promote', record['detail'])

    def test_restore_demote_refused_fails(self):
        # The restore's demote on the promoted peer refusing leaves the
        # pair off its pre-scenario role assignment.
        self.feed.demote_refuses = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('restore', record['detail'])

    def test_pair_never_resettling_fails(self):
        self.feed.never_settles = True
        record = self.run_scenario()
        self.assertEqual(record['outcome'], 'failed', record)
        self.assertIn('role assignment', record['detail'])

    def test_two_runs_produce_identical_evidence(self):
        runs = []
        for index in range(2):
            run_dir = Path(self.tmp.name) / ('run' + str(index))
            evidence = run_dir / 'evidence'
            evidence.mkdir(parents=True)
            journal_a = run_dir / 'a' / 'journal.jsonl'
            journal_a.parent.mkdir(parents=True)
            journal_b = run_dir / 'b' / 'journal.jsonl'
            journal_b.parent.mkdir(parents=True)
            feed = StandbyLossFeed(journal_a, journal_b)
            record = self.run_scenario(
                feed=feed, evidence_dir=str(evidence),
                journal_files={'active': str(journal_a),
                               'standby': str(journal_b)})
            runs.append((record, {p.name: p.read_text()
                                  for p in evidence.iterdir()}))
        self.assertEqual(runs[0], runs[1])


class PointAccessorTests(unittest.TestCase):
    """The snapshot point accessors' single contract: _point_sample
    serves the point's sample and answers None when the point is
    absent — the missing-point answer _point_quality shares on the
    diagnostics path instead of raising."""

    SNAPSHOT = {'tick': 7, 'points': [
        {'point': 10, 'sample': {'value': {'bool': True},
                                 'quality': 'good'}},
        {'point': 20, 'sample': {'value': {'float': 1.5},
                                 'quality': {'uncertain':
                                             'substituted'}}}]}

    def test_point_sample_serves_the_present_point(self):
        self.assertEqual(
            scenarios._point_sample(self.SNAPSHOT, 10),
            {'value': {'bool': True}, 'quality': 'good'})
        self.assertEqual(
            scenarios._point_sample(self.SNAPSHOT, 20),
            {'value': {'float': 1.5},
             'quality': {'uncertain': 'substituted'}})

    def test_point_sample_missing_point_answers_none(self):
        self.assertIsNone(scenarios._point_sample(self.SNAPSHOT, 99))
        self.assertIsNone(scenarios._point_sample({}, 10))

    def test_point_quality_serves_the_present_point(self):
        self.assertEqual(
            scenarios._point_quality(self.SNAPSHOT, 10), 'good')
        self.assertEqual(
            scenarios._point_quality(self.SNAPSHOT, 20),
            {'uncertain': 'substituted'})

    def test_point_quality_missing_point_answers_none(self):
        self.assertIsNone(scenarios._point_quality(self.SNAPSHOT, 99))
        self.assertIsNone(scenarios._point_quality({}, 10))


if __name__ == '__main__':
    unittest.main()
