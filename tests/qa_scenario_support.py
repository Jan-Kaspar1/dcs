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
  telemetry stays fresh or degrades past Bad, whose counters never
  advance, whose loss never journals, whose standby leaves tracking,
  whose writer claim never re-arms, whose restart window stands open
  or silent or never observed, whose controller restarted, or whose
  io_health forgets the failures it counted, an
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
contract — and the alarm-rationalization audit: a served registry
that drops an instance's record or prose block, a live parameter
report that omits an instance or drifts off the declared
priority/class/response_ticks, a durable journal silent on a
standing alarm point, a reference and bound port that disagree, a
retune refused or applied-but-never-served, a refused restore, a
re-read that disagrees with the first, and the inconclusive cases
when the served sections omit the records.

Split layout (#940): each leg of the scenario suite
carries its own test module —
tests/test_qa_scenario_NNNN_<slug>.py — holding its
leg-private feed fakes and TestCase classes plus an
EXPECTED_CASES pin of its case contribution. This
module is the shared seam those modules bind through
`from qa_scenario_support import *`: the monitor-feed
and plant-peer fakes, the dcs-plant-ctl wire stubs, and
the flood/pipelining helpers more than one leg's tests
use. A new leg's test module edits no shared file.
"""

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
        self.attempts = 0         # lifetime submissions — the log's high-water
        self.full_rejections = 0
        self.high_water = 0
        self.flood_pending = set()  # receipt indices admitted on the flood channel
        # The receipt log's retention bound: None keeps every receipt —
        # a value evicts the settled prefix past the cap on each append,
        # the served bounded-tail behavior the audit must correlate
        # across.
        self.receipt_cap = None
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
        self.attempts += 1
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
        if self.receipt_cap is not None:
            # The bounded tail's eviction: the settled prefix past the
            # cap leaves the window; pending receipts never evict.
            excess = len(self.receipts) - self.receipt_cap
            settled = next(
                (i for i, entry in enumerate(self.receipts)
                 if 'accepted' in entry['outcome']), len(self.receipts))
            evicted = min(excess, settled)
            if evicted > 0:
                del self.receipts[:evicted]
                self.flood_pending = {i - evicted
                                      for i in self.flood_pending
                                      if i >= evicted}
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
                    'attempts': self.attempts,
                    'full_rejections': self.full_rejections,
                    'capacity': self.capacity,
                    'depth': self._depth(),
                    'high_water': self.high_water}}
        if (method, route) == ('GET', '/receipts'):
            return 200, list(self.receipts)
        if (method, route) == ('GET', '/checkpoint'):
            return 200, {
                'receipts': list(self.receipts),
                'command_admission': {
                    'attempts': self.attempts,
                    'full_rejections': self.full_rejections,
                    'high_water': self.high_water}}
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


class _SocketPeerLifecycle:
    """Shared shutdown for the real loopback peers used by scenario tests."""

    def _start_peer_thread(self):
        self._stop_event = threading.Event()
        self._peer_lock = threading.Lock()
        self.conns = set()
        self._handler_threads = set()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = self.listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with self._peer_lock:
                    if self._stop_event.is_set():
                        conn.close()
                        return
                    self.conns.add(conn)
                    handler = threading.Thread(
                        target=self._handle_tracked, args=(conn,),
                        daemon=True)
                    self._handler_threads.add(handler)
                    handler.start()
        finally:
            self.listener.close()

    def _handle_tracked(self, conn):
        try:
            self._handle(conn)
        finally:
            with self._peer_lock:
                self.conns.discard(conn)
                self._handler_threads.discard(threading.current_thread())

    def close(self):
        self._stop_event.set()
        try:
            # Closing a listening socket from another thread does not wake
            # accept() reliably on Linux. A loopback connection does.
            with socket.create_connection(self.listener.getsockname(),
                                          timeout=0.5):
                pass
        except OSError:
            pass
        self.listener.close()

        with self._peer_lock:
            connections = list(self.conns)
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()

        self.thread.join(timeout=1)
        deadline = time.monotonic() + 1
        with self._peer_lock:
            handlers = list(self._handler_threads)
        for handler in handlers:
            handler.join(timeout=max(0, deadline - time.monotonic()))

        alive = [thread.name for thread in handlers if thread.is_alive()]
        if self.thread.is_alive() or alive:
            raise RuntimeError(
                "socket peer did not stop cleanly: "
                f"server={self.thread.is_alive()}, "
                f"handlers={alive}")


class FakePlantPeer(_SocketPeerLifecycle):
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
        self.listener.settimeout(0.5)
        self.address = '127.0.0.1:' \
            + str(self.listener.getsockname()[1])
        self._start_peer_thread()

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


class ClaimPlantPeer(_SocketPeerLifecycle):
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
        self.listener.settimeout(0.5)
        self.address = ('127.0.0.1:'
                        + str(self.listener.getsockname()[1]))
        self._start_peer_thread()

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
            with self.lock:
                self.conn_ids.pop(id(conn), None)
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
    """A stubbed pair for the demote-settle-uniqueness leg: ctrl-a
    owns the field at launch, ctrl-b tracks. Every endpoint call is one scan — the owner applies
    its pending admissions at the boundary and journals their
    settlements, the demote closes the gate so a still-accepted
    admission suspends, the promote's final-sync transfer carries the
    demoted log across, and a tracking peer's adoption journals the
    line's verdict — applied for the admissions the carry landed,
    superseded for the ones the adopted window passed. Doctor flags
    stage each named defect the issue calls out. An optional
    `journal_files` mapping (ctx keys 'active'/'standby' to paths)
    mirrors every journaled event into real --journal-file records for
    the leg's durable audit."""

    POINTS = (302, 300, 301, 332, 333, 334)
    SIGNALS = [{'point': point,
                'name': 'p101-oos' if point == 302
                        else 'pt-' + str(point),
                'direction': 'in', 'value_type': 'bool',
                'writable': True} for point in POINTS]

    def __init__(self, journal_files=None):
        self.a = DemoteSettlePeer('a')
        self.a.role = 'active'
        self.b = DemoteSettlePeer('b')
        self.b.tracking = True
        # The demote-pending leg's durable audit reads each endpoint's
        # --journal-file: peer 'a' serves ctx key 'active', peer 'b'
        # ctx key 'standby'. A feed without paths mirrors nothing —
        # the demote-settle-uniqueness leg never reads the files.
        self.journal_paths = {}
        for peer, key in ((self.a, 'active'), (self.b, 'standby')):
            path = (journal_files or {}).get(key)
            if path is not None:
                path = Path(path)
                path.write_text(
                    json.dumps({'run_boundary': {'run': 1,
                                                 'tick': 0}}) + '\n')
                self.journal_paths[peer.name] = path
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
        path = self.journal_paths.get(peer.name)
        if path is not None:
            with path.open('a') as stream:
                stream.write(json.dumps(
                    {'entry': {'seq': peer.journal[-1]['seq'],
                               'tick': peer.tick,
                               'event': event}}) + '\n')

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


__all__ = [name for name in globals() if not name.startswith('__')]
