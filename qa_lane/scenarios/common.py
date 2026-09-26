"""Shared seam for the QA acceptance legs: the report Case, the HTTP/plant probe helpers, the settle/judge machinery, and
the patch-point tunables every leg module binds through
`from .common import *`. patch.object(qa_lane.scenarios, name) writes
through the package facade into this module — and into every leg module
binding the name — so the pool tests' module-attribute seam resolves
exactly as it did on the pre-split monolith.
"""
import json
import math
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

SCENARIO_TIMEOUT = 120  # per-scenario wall clock bound
POLL_INTERVAL = 2.0
RESTART_JOURNAL_DEADLINE = 30  # bound on the run-boundary record landing


class Case:
    """One scenario's accumulating report record."""

    def __init__(self, key, title, expected):
        self.record = {'key': key, 'title': title, 'expected': expected,
                       'outcome': 'inconclusive', 'observations': [],
                       'evidence': []}

    def observe(self, text):
        self.record['observations'].append(text)

    def evidence(self, kind, ref, detail=None):
        entry = {'kind': kind, 'ref': ref}
        if detail:
            entry['detail'] = detail
        self.record['evidence'].append(entry)

    def finish(self, outcome, detail=None):
        self.record['outcome'] = outcome
        if detail:
            self.record['detail'] = detail
        return self.record


def http_json(method, url, body=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read() or b'null')


def wait_for(check, deadline, interval=POLL_INTERVAL):
    """Poll `check` until it returns a truthy value or the deadline passes."""
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    return last


def save_evidence(evidence_dir, name, payload):
    path = Path(evidence_dir) / name
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + '\n')
    return 'evidence/' + name


def _role(ctx, base):
    _, body = http_json('GET', base + '/role')
    return body


def _snapshot(ctx, base):
    _, body = http_json('GET', base + '/snapshot')
    return body


def _point_value(snapshot, point):
    for entry in snapshot.get('points', []):
        if entry.get('point') == point and entry.get('sample'):
            value = entry['sample'].get('value')
            if isinstance(value, dict):
                return next(iter(value.values()), None)
            return value
    return None


def _receipt_list(payload):
    """The receipt records out of either wire shape — a bare list or a
    `{"receipts": [...]}` envelope."""
    if isinstance(payload, list):
        return payload
    return payload.get('receipts', [])


def _journal_list(payload):
    """The journal records out of either wire shape — a bare list or an
    `entries`/`journal` envelope."""
    if isinstance(payload, list):
        return payload
    return payload.get('entries', payload.get('journal', []))


def _journal_covers(journal, point):
    """Whether a `GET /journal` payload recorded the scenario command's
    settlement."""
    for entry in _journal_list(journal):
        receipt = entry.get('event', {}).get('command_settled', {}) \
            .get('receipt', {})
        write = receipt.get('command', {}).get('write_value', {})
        if write.get('point') == point:
            return True
    return False


def _settled_active(ctx):
    """The ctx endpoint key whose peer currently reports role=active,
    or None while the pair is mid-transition. The model-revision
    case's third endpoint answers on 'revised' once its action ran —
    an unlaunched key reads as a refused connection and is skipped."""
    for name in ('active', 'standby', 'revised'):
        if ctx.get(name) is None:
            continue
        try:
            if _role(ctx, ctx[name]).get('role') == 'active':
                return name
        except Exception:
            pass
    return None


def _keyed_subject(ctx):
    """The pair ctx the keyed announced-source contract legs exercise
    (#1058): the deployed pair while the run config keys it —
    ctx['pair_token'] set — else the lane-staged probe pair the
    ctx['probe'] subject carries, or None when no keyed pair is
    staged.

    The probe subject is ctx-shaped: the same endpoint keys and
    runner actions bound to the probe pair's own containers and
    plant, its own claim-token pins, and the probe pair's always-set
    --pair-token — so a leg that rebinds ctx to the returned subject
    exercises the keyed contract for real instead of reporting
    inconclusive on the deployed pair's unkeyed posture.
    """
    if ctx.get('pair_token'):
        return ctx
    probe = ctx.get('probe')
    if probe and probe.get('pair_token'):
        return probe
    return None


def _writable_bool_point(signals):
    """The scenarios' command target out of a SignalIndex: the
    pump-station 'p101-oos' writable bool in-point when the model
    declares it, else any writable bool input."""
    target = None
    for entry in signals.get('points', []):
        if entry.get('name') == 'p101-oos' and entry.get('writable'):
            return entry
        if target is None and entry.get('writable') \
                and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool':
            target = entry
    return target


def _parameter_value(snapshot, component, name):
    """The live value a snapshot's `parameters` section reports for one
    descriptor-declared parameter — the section the checkpoint's
    component state reports through — or None when absent."""
    for entry in snapshot.get('parameters') or []:
        if entry.get('name') == component:
            value = (entry.get('values') or {}).get(name)
            if isinstance(value, dict):
                return next(iter(value.values()), None)
            return value
    return None


def _float_tune_plan(schema, snapshot):
    """The scenario's tune target out of the served registry and live
    parameter report: (component, parameter, current, tuned, outside)
    for the first descriptor-declared Float whose inclusive [min, max]
    bounds hold a changed finite in-range value and admit a finite
    out-of-range submission. Returns None when no served parameter can
    exercise both legs."""
    reported = {entry.get('name'): entry.get('values') or {}
                for entry in snapshot.get('parameters') or []}
    for entry in schema.get('interfaces') or []:
        component = entry.get('name')
        values = reported.get(component)
        if not isinstance(values, dict):
            continue
        for prop in (entry.get('interface') or {}) \
                .get('configuration') or []:
            if prop.get('kind') != 'float' \
                    or prop.get('capability', 'tunable') != 'tunable':
                continue
            bounds = prop.get('range') or {}
            lo = (bounds.get('min') or {}).get('float')
            hi = (bounds.get('max') or {}).get('float')
            name = prop.get('name')
            raw = values.get(name)
            current = raw.get('float') if isinstance(raw, dict) \
                else None
            if lo is None or hi is None or name is None \
                    or current is None or not lo < hi:
                continue
            tuned = next(
                (c for c in (lo + 1.0, current + 1.0, hi - 1.0,
                             (lo + hi) / 2.0, lo, hi)
                 if math.isfinite(c) and lo <= c <= hi
                 and c != current), None)
            if tuned is None:
                continue
            below = lo - 1.0
            if not below < lo:
                below = math.nextafter(lo, -math.inf)
            above = hi + 1.0
            if not above > hi:
                above = math.nextafter(hi, math.inf)
            outside = below if below < lo \
                else above if above > hi else None
            if outside is None or not math.isfinite(outside):
                continue
            return component, name, current, tuned, outside
    return None


# --------------------------------------------------------------------
# The rolling model-revision contract (WW-LCM-001's deployment-update
# clause, the rolling model-revision decision): a site deploys an
# updated plant model by launching a third controller on the revised
# document with `--standby <active> --revised`; once the peer reports
# the named `reinitialized` convergence carrying its carryover report,
# the documented demote-then-promote order moves the field writer onto
# the revised model's fingerprint while field writes, receipts, and
# the durable journal continue across the boundary — and the demoted
# peer settles rather than resuming writes (the plant's single-writer
# fence meets any leaked attempt).

REVISION_CONVERGE_DEADLINE = 120  # bound on the revised peer's crossing
REVISION_FIELD_ROUNDS = 6         # field/telemetry reads after the roll
REVISION_POLL = 0.5               # cadence watching the roll's peers


def _field_sample(ctx, point):
    """The plant's stored sample for `point` — `{'value', 'quality',
    'tick'}` — or None when the read dropped; a lost observation, never
    the leg's verdict. The shipped tool's `read` serves it."""
    body = _try_plant_ctl(ctx, 'read', str(point))
    if not isinstance(body, dict):
        return None
    return body.get('sample')


def _field_out_points(ctx):
    """Every field `out` point the simulated plant serves — the points
    a field-owning scan writes — from the plant's own census, listed by
    the shipped tool."""
    body = _try_plant_ctl(ctx, 'list')
    points = (body or {}).get('points') or []
    return [entry['point'] for entry in points
            if isinstance(entry, dict)
            and entry.get('direction') == 'out']


def _journal_entries(path):
    """The parsed records of a `--journal-file` with their full bodies:
    `{'run_boundary': {...}}` markers and `{'entry': {'seq', 'tick',
    'event'}}` lines. A torn final line — a crash mid-append — is
    skipped; any earlier unparseable or unrecognized line raises."""
    items = []
    lines = Path(path).read_text().splitlines()
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if index == len(lines) - 1:
                continue
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1) + ' does not parse')
        if isinstance(record, dict) \
                and ('run_boundary' in record or 'entry' in record):
            items.append(record)
        else:
            raise ValueError('journal file ' + str(path) + ' line '
                             + str(index + 1)
                             + ' is not a journal record')
    return items


def _journal_settled(item):
    """The receipt a journal record's `command_settled` event carries,
    or None — for a `--journal-file` record (`{'entry': {...}}`) and a
    served `GET /journal` entry alike."""
    body = item.get('entry') if isinstance(item.get('entry'), dict) \
        else item
    event = (body or {}).get('event') or {}
    return (event.get('command_settled') or {}).get('receipt')


def _journal_observation(item):
    """(kind, point, from, to) when the record is a `quality_changed`
    or `point_changed` entry, else None — the observation record the
    restart-census leg folds."""
    body = item.get('entry') if isinstance(item.get('entry'), dict) \
        else item
    event = (body or {}).get('event') or {}
    for kind in ('quality_changed', 'point_changed'):
        if isinstance(event.get(kind), dict):
            change = event[kind]
            return kind, change.get('point'), \
                change.get('from'), change.get('to')
    return None


def _probe_sample(ctx, point):
    """The plant's stored sample for `point` through the shipped
    tool's `read` — `{'value', 'quality', 'tick'}` — or None when the
    read dropped; a lost observation, never the leg's verdict."""
    body = _try_plant_ctl(ctx, 'read', str(point))
    if not isinstance(body, dict):
        return None
    return body.get('sample')


def _probe_error(response):
    """The named refusal a plant answer carries — 'fenced',
    'unclaimed' — or None on a success or malformed answer."""
    error = (response or {}).get('error')
    return error.get('kind') if isinstance(error, dict) else None


def _tick_rewind(ticks, floor, jitter):
    """The first served-tick sample that rewound the tick domain — a
    drop below `floor`, or more than the pull-cadence `jitter` below
    the running peak — or None. Returns the offending sample index."""
    peak = floor
    for index, tick in enumerate(ticks):
        if not isinstance(tick, int):
            return index
        if tick < peak - jitter:
            return index
        peak = max(peak, tick)
    return None


def _history_disorder(payload, jitter):
    """The first newest-last violation in a `/history` answer — the
    point id plus the offending ordering — or None when every point's
    rows are strictly-increasing seqs whose ticks never rewind past
    the pull-cadence `jitter` below the running peak."""
    if not isinstance(payload, list):
        return 'unrecognized history payload'
    for entry in payload:
        rows = entry.get('samples') or []
        seqs = [row.get('seq') for row in rows]
        ticks = [(row.get('sample') or {}).get('tick') for row in rows]
        if seqs != sorted(seqs) or len(set(seqs)) != len(seqs):
            return 'point ' + str(entry.get('point')) \
                + ' seqs not strictly increasing: ' + str(seqs[:20])
        if any(not isinstance(tick, int) for tick in ticks):
            return 'point ' + str(entry.get('point')) \
                + ' carries a sample without an integer tick'
        rewound = _tick_rewind(ticks, ticks[0] if ticks else 0, jitter)
        if rewound is not None:
            return 'point ' + str(entry.get('point')) \
                + ' ticks rewound at sample ' + str(rewound) \
                + ': ' + str(ticks[:20])
    return None


def _tracking_aligned(report):
    """The aligned stream tick a standby's RoleReport carries, or None
    while it is unsynchronized, degraded, diverged, or field-owning."""
    sync = (report or {}).get('sync')
    if not isinstance(sync, dict):
        return None
    aligned = (sync.get('tracking') or {}).get('aligned')
    return aligned if isinstance(aligned, int) else None


def _plant_probe(ctx, request, timeout=5):
    """One request/response against the run's plant server on a fresh
    connection — the `dcs-sim-net` wire protocol on the published
    endpoint ctx['plant'] carries. The link-loss scenario uses it for
    the field's own evidence: `list_points` is the census of points the
    boundary fails at once, and a `step` mutation is the fencing probe
    — answered `fenced` while any attachment holds the plant's
    single-writer claim, `unclaimed` while none does: the field fails
    closed across a restart, so `unclaimed` means "waiting on the
    owner's re-arm", not an open window. Each probe takes a new
    connection because the outage it watches is exactly a dead
    listener; the probe never sends `claim_writer` — claiming from
    here would preempt the field owner it is checking for. The probe
    stays on the raw client rather than `dcs-plant-ctl`'s `step`
    subcommand for the same reason: the tool wraps its mutation in its
    own conditional claim — `ensure_writer` under the tool token, then
    `release_writer` — so an unclaimed field would answer `stepped`
    under a fleeting tool claim instead of the `unclaimed` verdict the
    leg watches for, and that claim window could fence the field
    owner's re-arm it polls for. The bare `step` is a request shape
    the tool does not expose."""
    stream = _plant_connect(ctx, timeout=timeout)
    try:
        stream.settimeout(timeout)
        return _plant_request(stream, request)
    finally:
        stream.close()


def _try_plant(ctx, request):
    """`_plant_probe` or None — a refused probe is one lost poll, not
    the leg's verdict."""
    try:
        return _plant_probe(ctx, request)
    except Exception:
        return None


def _fenced(response):
    """Whether a fencing probe's answer says a writer claim stands —
    the shared field refused a third attachment's mutation."""
    return (response or {}).get('error', {}).get('kind') == 'fenced'


def _sample_quality(snapshot, point):
    """The point's latest served quality flattened for comparison —
    'good', 'uncertain:stale', 'bad:communication_fault' — or None when
    no sample exists."""
    for entry in snapshot.get('points', []):
        if entry.get('point') == point and entry.get('sample'):
            quality = entry['sample'].get('quality')
            if isinstance(quality, dict):
                kind, reason = next(iter(quality.items()))
                return kind + ':' + str(reason)
            return quality
    return None


def _command_for_spec(component, spec):
    """The receipted-path command a served `commands` entry denotes,
    rebuilt from the entry's declared provenance: `declared` entries
    submit as `invoke` with the declared request schema's arguments,
    `write_value` entries as the point write against the entry's bound
    point, and `set_parameter` entries as the parameter tune. Returns
    None for an entry this driver cannot translate."""
    defaults = {'bool': {'bool': True}, 'int': {'int': 1},
                'float': {'float': 1.0}}
    request = spec.get('request') or []
    adapted = spec.get('adapted')
    if adapted == 'declared':
        arguments = {}
        for argument in request:
            value = defaults.get(argument.get('kind'))
            if value is None:
                return None
            arguments[argument['name']] = value
        return {'invoke': {'component': component,
                           'command': spec.get('name'),
                           'arguments': arguments}}
    if adapted == 'set_parameter' and ':' in str(spec.get('name')):
        kind = request[0].get('kind') if request else None
        value = defaults.get(kind)
        if value is None:
            return None
        return {'set_parameter': {'component': component,
                                  'name': str(spec['name']).split(':', 1)[1],
                                  'value': value}}
    if adapted == 'write_value' and spec.get('point') is not None:
        kind = request[0].get('kind') if request else None
        value = defaults.get(kind)
        if value is None:
            return None
        return {'write_value': {'point': spec['point'], 'kind': kind,
                                'value': value}}
    return None


def _pick_declared_command(interfaces, signals):
    """The scenario's probe command out of the served `commands`
    collections, in preference order: a kind-declared (`declared`-
    provenance) entry a kind offers natively; then the `write_value`
    adapted entry bound to the run's preferred writable bool point —
    'p101-oos', the target the other command scenarios use; then any
    writable bool point's entry; then any remaining translated entry
    (a `set_parameter` tune, or a refused point write — a rejection is
    still a receipted, journaled answer). Returns
    (component, spec, submission) or None."""
    writable = {entry.get('point')
                for entry in signals.get('points', [])
                if entry.get('writable')
                and entry.get('direction') == 'in'
                and entry.get('value_type') == 'bool'}
    preferred = {entry.get('point')
                 for entry in signals.get('points', [])
                 if entry.get('name') == 'p101-oos'} & writable
    best = None
    for entry in interfaces:
        component = entry.get('name')
        for spec in (entry.get('interface') or {}).get('commands') or []:
            submission = _command_for_spec(component, spec)
            if submission is None:
                continue
            adapted = spec.get('adapted')
            if adapted == 'declared':
                rank = 0
            elif adapted == 'write_value' \
                    and spec.get('point') in preferred:
                rank = 1
            elif adapted == 'write_value' \
                    and spec.get('point') in writable:
                rank = 2
            else:
                rank = 3
            if best is None or rank < best[0]:
                best = (rank, component, spec, submission)
    if best is None:
        return None
    return best[1], best[2], best[3]


# --------------------------------------------------------------------
# Receipted point forcing and release (WW-OPS-003's substituted
# quality, WW-FND-004's settled receipts): `force_point` pins a
# writable `In` point at Uncertain(Substituted) across scans and badges
# it in the snapshot's `forces` list; `unforce_point` lifts it at a
# scan boundary. The rig's target is its writable internal `In` point
# p101-oos — the executor's force path accepts writable internal
# points, the operator-setpoint surface, so the model declares no
# writable loopback field point (a channel-bound `writable` mark is
# exactly what the model lint names). Releasing an internal point
# resumes the held-value rule — and the release boundary itself
# re-stamps the held image Good (finding #498's merged fix): no
# channel rewrites a held internal point, so without that re-stamp
# the force's last Substituted mark would stand forever, tainting the
# downstream p101-oos-ok cone and the tracking standby's adopted
# snapshot. The recovery legs therefore observe the release before
# any restamp write — the held-value write afterward is the suite's
# restore step, not what proves the release took.

FORCE_DEADLINE = 30  # bound on each boundary/settlement wait


def _point_sample(snapshot, point):
    """The served sample of one point in a /snapshot payload, or None
    when the point is absent — callers test or guard the None rather
    than assume a dict."""
    for entry in (snapshot or {}).get('points', []):
        if entry.get('point') == point:
            return entry.get('sample')
    return None


def _point_quality(snapshot, point):
    """The served quality of one point, or None when the point is
    absent — the missing-point answer a diagnostics leg reports
    instead of raising."""
    sample = _point_sample(snapshot, point)
    return sample.get('quality') if sample is not None else None


def _forced_entry(snapshot, point):
    """The snapshot's `forces` badge for `point`, or None."""
    for entry in (snapshot or {}).get('forces', []):
        if entry.get('point') == point:
            return entry
    return None


def _settled_receipts(journal):
    """The receipts the journal settled — `command_settled` payloads."""
    return [entry.get('event', {}).get('command_settled', {})
            .get('receipt') or {}
            for entry in _journal_list(journal)]


# --------------------------------------------------------------------
# The consumer-failure schedule (WW-FND-004, decision 83): the lane's
# per-revision proof that a slow, disconnected, malformed, or restarted
# consumer can never reach the control loop. Every leg measures the
# same signature — one publication per completed scan, an honest
# seq-cursor history stream, identical receipted command outcomes —
# while one consumer behavior overlaps it, and each interference leg's
# signature must equal the bracketing no-consumer reference legs'.

LEG_TICKS = 8       # completed scans each leg's window spans
LEG_POLL = 0.1      # measurement cadence inside a leg's window
LEG_DEADLINE = 30   # bound on one leg's window or a settlement wait

# The command-admission flood (decision 83's bounded-ingress half):
# each pipelined burst submits twice the served queue bound on one
# keep-alive connection — the whole batch lands inside the server's
# read buffer faster than a scan boundary can drain pending entries —
# and rounds repeat until the named queue_full rejection appears. A
# trickle keeps validated submissions arriving inside the measured leg.
ADMISSION_ROUNDS = 8        # pipelined bursts before the flood is 'insufficient'
ADMISSION_TRICKLE = 8       # submissions per poll round inside a flood leg

# The malformed set the consumer schedules declare, each with the
# status the documented endpoints answer: unparseable bodies and bad
# queries are 400, unknown paths and refused verbs 404, a valid
# POST /scan meets the paced monitor's named 409, and POST /promote on
# the settled active the named already_active 409. No well-formed
# command appears — a receipted command is a run input, not
# interference.
MALFORMED_PROBES = (
    ('GET', '/nonexistent', None, 404),
    ('POST', '/snapshot', None, 404),
    ('DELETE', '/receipts', None, 404),
    ('PUT', '/scan', None, 404),
    ('GET', '/history?point=abc', None, 400),
    ('GET', '/history?since=-1', None, 400),
    ('GET', '/journal?since=soon', None, 400),
    ('POST', '/command', '{', 400),
    ('POST', '/command', '{"command":{"bogus":1}}', 400),
    ('POST', '/command', '{"actor":3}', 400),
    ('POST', '/command',
     '{"write_value":{"point":10,"kind":"float","value":"high"}}', 400),
    ('POST', '/scan', '{', 400),
    ('POST', '/scan', '{"scans":-1}', 400),
    ('POST', '/scan', '{"scans":1}', 409),
    ('POST', '/promote', None, 409),
)


def _request_status(method, url, body=None, timeout=10):
    """(status, raw body) — unlike http_json, a non-2xx answer returns
    instead of raising: the malformed probes' refusals are the data."""
    if isinstance(body, str):
        data = body.encode()
    elif body is not None:
        data = json.dumps(body).encode()
    else:
        data = None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, None


def _connect(base, timeout=5):
    """A raw TCP connection to a monitor base URL — the transport the
    held/churning/raw-probe consumers speak below the HTTP layer."""
    host = base.split('://', 1)[-1]
    hostname, _, port = host.rpartition(':')
    return socket.create_connection(
        (hostname or '127.0.0.1', int(port or 80)), timeout=timeout)


def _parse_responses(raw):
    """Split `raw` into as many complete HTTP responses as it holds.
    Returns ([(status, json-body-or-None)], leftover) — a truncated or
    unframed answer stays in leftover for the next chunk."""
    replies = []
    while raw:
        head, sep, rest = raw.partition(b'\r\n\r\n')
        if not sep:
            break
        lines = head.split(b'\r\n')
        try:
            status = int(lines[0].split(None, 2)[1])
        except (IndexError, ValueError):
            status = None
        length = None
        for line in lines[1:]:
            name, colon, value = line.partition(b':')
            if colon and name.strip().lower() == b'content-length':
                try:
                    length = int(value.strip())
                except ValueError:
                    length = None
        if length is None or len(rest) < length:
            break
        payload, raw = rest[:length], rest[length:]
        try:
            replies.append((status, json.loads(payload)))
        except ValueError:
            replies.append((status, None))
    return replies, raw


def _pipelined_commands(base, commands, timeout=15):
    """POST every command envelope on one keep-alive connection, all
    requests sent back-to-back before the first answer is read — the
    flood channel: submissions land inside the server's read buffer
    faster than a scan boundary can drain the pending queue. Returns
    [(status, receipt-or-None)] in submission order, one entry per
    command; a missing or unparseable answer reads (None, None) — the
    no-receipt case the admission contract forbids."""
    bodies = [json.dumps(command).encode() for command in commands]
    request = b''
    for index, body in enumerate(bodies):
        tail = b'Connection: close\r\n' if index == len(bodies) - 1 else b''
        request += (b'POST /command HTTP/1.1\r\nHost: qa\r\n'
                    b'Content-Type: application/json\r\nContent-Length: '
                    + str(len(body)).encode() + b'\r\n' + tail + b'\r\n'
                    + body)
    stream = _connect(base, timeout=timeout)
    try:
        stream.sendall(request)
        stream.settimeout(timeout)
        replies, raw = [], b''
        deadline = time.monotonic() + timeout
        while len(replies) < len(commands) \
                and time.monotonic() < deadline:
            try:
                chunk = stream.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            raw += chunk
            found, raw = _parse_responses(raw)
            replies += found
        found, raw = _parse_responses(raw)
        replies += found
        return (replies + [(None, None)] * len(commands))[:len(commands)]
    finally:
        stream.close()


def _publication(snapshot):
    """A snapshot's `publication` section — the store's overload
    counters as of that publish: produced, coalesced, retained depth,
    configured window."""
    return snapshot.get('publication') or {}


def _history_seqs(payload, point):
    """One point's served history seqs out of a `/history` answer."""
    if isinstance(payload, list):
        for entry in payload:
            if entry.get('point') == point:
                return [sample.get('seq')
                        for sample in entry.get('samples', [])]
    return []


def _history_cursor(ctx, base, point):
    """A consumer's `?since=` cursor on the point's history stream —
    the newest served seq, or 0 while the stream is empty."""
    _, body = http_json('GET', base + '/history?point=' + str(point)
                        + '&since=0')
    seqs = _history_seqs(body, point)
    return seqs[-1] if seqs else 0


def _journal_seqs(payload):
    return [entry.get('seq') for entry in _journal_list(payload)]


def _journal_cursor(ctx, base):
    """The newest served journal seq — a consumer's `?since=` cursor."""
    _, body = http_json('GET', base + '/journal?since=0')
    seqs = _journal_seqs(body)
    return seqs[-1] if seqs else 0


def _outcome_key(receipt):
    """A receipt's normalized verdict — 'accepted', 'applied', or
    'rejected:<reason>' — the cross-leg comparable."""
    outcome = (receipt or {}).get('outcome')
    if not isinstance(outcome, dict) or not outcome:
        return 'unknown'
    name = next(iter(outcome))
    if name == 'rejected':
        body = outcome.get('rejected')
        reason = body.get('reason') if isinstance(body, dict) else {}
        return 'rejected:' + (next(iter(reason))
                              if isinstance(reason, dict) and reason
                              else '?')
    return name


def _receipt_window(ctx, base):
    """The retained receipt tail and the absolute submission index of
    its first entry — the (log, high-water) pair the bounded-log audit
    correlates by.

    `GET /receipts` serves the bounded tail alone; its place in the
    submission sequence comes from the admission counters the
    checkpoint carries beside the same log under one capture —
    `command_admission.attempts` counts every submission and each
    appends exactly one receipt, so `attempts - len(receipts)` is the
    evicted prefix's length. A monitor whose checkpoint predates the
    receipts section cannot speak for the log's place in the
    sequence; the bare receipts read answers with base 0 — the
    never-evicted log's genuine numbering.
    """
    try:
        _, body = http_json('GET', base + '/checkpoint')
        carried = body.get('receipts')
        if isinstance(carried, list):
            receipts = _receipt_list(carried)
            attempts = (body.get('command_admission') or {}) \
                .get('attempts')
            if isinstance(attempts, int) \
                    and not isinstance(attempts, bool):
                return receipts, max(0, attempts - len(receipts))
            return receipts, 0
    except Exception:
        pass
    _, body = http_json('GET', base + '/receipts')
    return _receipt_list(body), 0


def _next_receipt_index(ctx, base):
    """The absolute submission index the next POST /command receipt
    takes — the log's high-water `base + len(receipts)`."""
    receipts, base_index = _receipt_window(ctx, base)
    return base_index + len(receipts)


def _settled_outcome(ctx, base, index):
    """The outcome key of the receipt logged at absolute submission
    `index` once its verdict is final — None while it still reads
    `accepted` or the log cannot be read, 'evicted' when the bounded
    tail already dropped the settled entry. `index` is the submission
    sequence the window slides under — never a position in the tail."""
    try:
        receipts, base_index = _receipt_window(ctx, base)
    except Exception:
        return None
    position = index - base_index
    if position < 0:
        return 'evicted'
    if position >= len(receipts):
        return None
    outcome = _outcome_key(receipts[position])
    return None if outcome == 'accepted' else outcome


def _try_snapshot(ctx, base):
    """`/snapshot` or None — for wait loops a dropped read is one lost
    sample, not the leg's verdict."""
    try:
        return _snapshot(ctx, base)
    except Exception:
        return None


def _signal_targets(signals):
    """The scenario's probe points out of the SignalIndex: a writable
    bool command point (the operator-command target 'p101-oos' when
    present), any non-writable point for the named rejection, and the
    point whose history stream the legs watch."""
    write = reject = None
    for entry in signals.get('points', []):
        if entry.get('name') == 'p101-oos' and entry.get('writable'):
            write = entry.get('point')
        elif write is None and entry.get('writable') \
                and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool':
            write = entry.get('point')
        if reject is None and not entry.get('writable'):
            reject = entry.get('point')
    if write is None or reject is None:
        return None
    return {'write': write, 'reject': reject, 'watch': write}


class _Overlay:
    """One leg's consumer behavior, driven inline: `start` runs before
    the leg's tick window, `poll` once per measurement round inside it,
    `finish` after the leg's probes. The scenario stays single-threaded
    and deterministic, and its own measurement requests are never the
    interference under test."""

    def __init__(self, kind, base, watch, flood=None):
        self.kind = kind
        self.base = base
        self.statuses = []    # every HTTP status the consumer read back
        self.errors = []      # transport failures the consumer met
        self.probes = 0       # raw socket probes that became no request
        self.held = None      # the stalled reader's held (status, body)
        self.malformed = {}   # 'METHOD path' -> status
        self._held_socket = None
        self._index = 0
        self.surfaces = ['/snapshot', '/receipts', '/journal?since=0',
                         '/history?point=' + str(watch) + '&since=0',
                         '/checkpoint', '/role', '/signals', '/']
        # The command-flood leg's admission record: flood carries the
        # served bound and the probe point; submissions logs every
        # (status, normalized outcome, absolute submission index) the
        # flood met.
        self.flood = flood
        self.submissions = []
        self._receipts_base = None

    def _send(self, method, path, body=None):
        try:
            status, _ = _request_status(method, self.base + path, body)
        except Exception as exc:
            self.errors.append(str(exc)[:200])
            return None
        self.statuses.append(status)
        return status

    def _next_surface(self):
        path = self.surfaces[self._index % len(self.surfaces)]
        self._index += 1
        return path

    def _raw_probes(self):
        # Garbage bytes and a half-sent request — traffic that never
        # becomes a request at all.
        for payload, how in (
                (b'\x89not-an-http-request\x90\r\n\r\n', socket.SHUT_WR),
                (b'GET /snapshot HTT', socket.SHUT_RDWR)):
            try:
                stream = _connect(self.base)
            except OSError as exc:
                self.errors.append(str(exc)[:200])
                continue
            try:
                stream.sendall(payload)
                stream.shutdown(how)
                stream.settimeout(0.25)
                try:
                    stream.recv(4096)
                except OSError:
                    pass
            except OSError as exc:
                self.errors.append(str(exc)[:200])
            finally:
                stream.close()
            self.probes += 1

    def _churn(self):
        # Connect, issue a read, take some or none of the response,
        # drop — the disconnect mid-session.
        path = self._next_surface()
        try:
            stream = _connect(self.base)
        except OSError as exc:
            self.errors.append(str(exc)[:200])
            return
        try:
            stream.sendall(('GET ' + path + ' HTTP/1.1\r\nHost: x\r\n'
                            'Connection: close\r\n\r\n').encode())
            stream.settimeout(0.25)
            try:
                head = stream.recv(512)
            except OSError:
                head = b''
            if head.startswith(b'HTTP'):
                try:
                    self.statuses.append(int(head.split(None, 2)[1]))
                except (ValueError, IndexError):
                    pass
        except OSError as exc:
            self.errors.append(str(exc)[:200])
        finally:
            stream.close()

    @property
    def queue_fulls(self):
        """The flood submissions the named queue_full rejection met."""
        return sum(1 for submission in self.submissions
                   if submission['outcome'] == 'rejected:queue_full')

    def _flood_command(self):
        return {'command': {'write_value': {
            'point': self.flood['point'], 'kind': 'bool',
            'value': {'bool': True}}},
            'actor': 'qa-lane'}

    def _record_submission(self, status, receipt):
        """One flood submission's verdict: its HTTP status, its
        receipt's normalized outcome, and the absolute submission index
        the log assigns it — every POST /command appends exactly one
        receipt, in submission order, so the index counts from the
        admission high-water the flood started at."""
        if status is not None:
            self.statuses.append(status)
        self.submissions.append({
            'status': status,
            'outcome': _outcome_key(receipt) if isinstance(receipt, dict)
            else 'none',
            'index': self._receipts_base + len(self.submissions)
            if self._receipts_base is not None else None})

    def _flood_burst(self, count):
        for status, receipt in _pipelined_commands(
                self.base, [self._flood_command()] * count):
            self._record_submission(status, receipt)

    def start(self, ctx):
        if self.kind == 'stalled-reader':
            # Issue the request, then go silent without reading a byte
            # of the response until the leg ends — the held-connection
            # case the publication split exists for.
            try:
                stream = _connect(self.base)
                stream.sendall(b'GET /snapshot HTTP/1.1\r\nHost: x\r\n'
                               b'Connection: close\r\n\r\n')
                self._held_socket = stream
            except OSError as exc:
                self.errors.append(str(exc)[:200])
        elif self.kind == 'malformed-and-flood':
            for method, path, body, _expected in MALFORMED_PROBES:
                status = self._send(method, path, body)
                if status is not None:
                    self.malformed[method + ' ' + path] = status
        elif self.kind == 'command-flood':
            # The bounded admission flood: pipelined bursts of twice the
            # served queue bound until the named queue_full rejection
            # appears — the whole batch lands inside one server read
            # buffer, faster than a scan boundary drains pending entries.
            # The audit tracks each submission's absolute index — the
            # receipt log is a bounded tail that can roll under the
            # flood, so a position read at start would chase the window
            # instead of the entry.
            try:
                self._receipts_base = _next_receipt_index(ctx, self.base)
            except Exception as exc:
                self.errors.append('receipts base: ' + str(exc)[:150])
            rounds = 0
            while rounds < ADMISSION_ROUNDS and not self.queue_fulls:
                self._flood_burst(2 * self.flood['capacity'])
                rounds += 1

    def poll(self):
        if self.kind == 'polling':
            self._send('GET', self._next_surface())
        elif self.kind == 'disconnect-reconnect':
            self._churn()
        elif self.kind == 'malformed-and-flood':
            self._send('GET', self._next_surface())
            if self._index % 4 == 1:
                self._raw_probes()
        elif self.kind == 'command-flood':
            # A small pipelined trickle each measurement round — the
            # admission path stays loaded through the leg's scan window.
            self._flood_burst(ADMISSION_TRICKLE)

    def finish(self, ctx):
        """Drains held resources and returns the leg's named evidence
        failures — interference that never happened, or a consumer that
        met a server fault."""
        failures = []
        if self._held_socket is not None:
            stream, self._held_socket = self._held_socket, None
            try:
                stream.settimeout(5)
                chunks = []
                while True:
                    try:
                        chunk = stream.recv(65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                text = b''.join(chunks).decode(errors='replace')
                try:
                    status = int(text.split(None, 2)[1]) \
                        if text.startswith('HTTP') else 0
                except (ValueError, IndexError):
                    status = 0
                self.held = (status, text[:2000])
            finally:
                stream.close()
        if self.kind == 'stalled-reader':
            if self.held is None:
                failures.append('the stalled reader never held a '
                                'response')
            elif self.held[0] != 200:
                failures.append('the held response answered '
                                + str(self.held[0]))
            elif '"tick"' not in self.held[1]:
                failures.append('the held response was not a complete '
                                'snapshot')
        if self.kind in ('polling', 'disconnect-reconnect') \
                and not self.statuses:
            failures.append('the ' + self.kind + ' consumers never ran')
        if self.kind == 'malformed-and-flood':
            if not self.probes:
                failures.append('no raw probes reached the socket')
            expected = {method + ' ' + path: want
                        for method, path, _body, want
                        in MALFORMED_PROBES}
            wrong = {key: [self.malformed.get(key), want]
                     for key, want in expected.items()
                     if self.malformed.get(key) != want}
            if wrong:
                failures.append('malformed probes answered outside the '
                                'declared limits: '
                                + json.dumps(wrong, sort_keys=True)[:600])
        if self.kind == 'command-flood':
            if not self.submissions:
                failures.append('the command flood never ran')
            no_receipt = sum(1 for submission in self.submissions
                             if submission['status'] is None)
            if no_receipt:
                failures.append(str(no_receipt) + ' flood submissions '
                                'returned no receipt')
            http_errors = sorted({submission['status']
                                  for submission in self.submissions
                                  if submission['status'] is not None
                                  and submission['status'] != 200})
            if http_errors:
                failures.append('flood submissions met HTTP-layer '
                                'errors: ' + str(http_errors))
            outside = sorted({submission['outcome']
                              for submission in self.submissions
                              if submission['status'] == 200
                              and submission['outcome'] not in
                              ('accepted', 'applied',
                               'rejected:queue_full')})
            if outside:
                failures.append('flood receipts answered outside the '
                                'admission vocabulary: ' + str(outside))
            if self.submissions and not self.queue_fulls:
                failures.append('the named queue_full rejection never '
                                'appeared under '
                                + str(len(self.submissions))
                                + ' submissions against the served '
                                'capacity ' + str(self.flood['capacity']))
            elif not any(submission['outcome'] == 'accepted'
                         for submission in self.submissions):
                failures.append('no flood submission was admitted')
            if self._receipts_base is None:
                failures.append('the receipt log was unreadable at '
                                'flood start — settlement cannot be '
                                'audited')
            elif not (no_receipt or http_errors or outside):
                # Every admitted command's receipt must settle applied
                # at its scan boundary. The receipt log is a bounded
                # tail a flood can out-roll, so each submission is
                # tracked by absolute index against the checkpoint's
                # (log, high-water) pair; an index the window already
                # passed is a settled entry the tail coalesced — pending
                # receipts never evict.
                pending = [submission['index']
                           for submission in self.submissions
                           if submission['outcome'] == 'accepted']

                def drained():
                    try:
                        receipts, base_index = _receipt_window(
                            ctx, self.base)
                    except Exception:
                        return None
                    for index in pending:
                        position = index - base_index
                        if position < 0:
                            continue
                        if position >= len(receipts) \
                                or _outcome_key(receipts[position]) \
                                != 'applied':
                            return None
                    return True

                if pending and not wait_for(
                        drained, time.monotonic() + LEG_DEADLINE,
                        interval=LEG_POLL):
                    failures.append('admitted flood commands never '
                                    'settled applied at a scan '
                                    'boundary')
        if any(status >= 500 for status in self.statuses):
            failures.append('a consumer saw a server fault: '
                            + str(sorted(set(self.statuses))))
        if self.errors:
            failures.append('consumer transport errors: '
                            + '; '.join(self.errors[:3]))
        return failures


def _consumer_leg(ctx, base, targets, overlay, want):
    """One measured leg: LEG_TICKS completed scans under the overlay's
    consumer behavior, then the leg's two receipted probe commands.

    Returns (signature, failures) — signature is None when the leg's
    scan outputs stopped advancing; that failure is named in failures.
    """
    failures = []
    start = _snapshot(ctx, base)
    tick0 = start.get('tick') or 0
    published0 = _publication(start).get('published') or 0
    h0 = _history_cursor(ctx, base, targets['watch'])
    deadline = time.monotonic() + LEG_DEADLINE
    try:
        overlay.start(ctx)
    except Exception as exc:
        overlay.errors.append('start: ' + str(exc)[:150])
    end = None
    while time.monotonic() < deadline:
        try:
            overlay.poll()
        except Exception as exc:
            overlay.errors.append('poll: ' + str(exc)[:150])
        try:
            snap = _snapshot(ctx, base)
        except Exception:
            snap = None
        if snap and (snap.get('tick') or 0) >= tick0 + LEG_TICKS:
            end = snap
            break
        time.sleep(LEG_POLL)
    if end is None:
        failures.append('scan outputs stopped advancing under '
                        + overlay.kind + ' at tick ' + str(tick0))
        failures += overlay.finish(ctx)
        return None, failures

    # The leg's receipted probes: one writable write (the leg's
    # alternating value), one statically invalid write — the two
    # command-path outcomes every leg must reproduce identically.
    index = _next_receipt_index(ctx, base)
    http_json('POST', base + '/command',
              {'command': {'write_value': {
                  'point': targets['write'], 'kind': 'bool',
                  'value': {'bool': want}}},
               'actor': 'qa-lane'})
    _, rejected = http_json('POST', base + '/command',
                            {'command': {'write_value': {
                                'point': targets['reject'],
                                'kind': 'bool',
                                'value': {'bool': True}}},
                             'actor': 'qa-lane'})
    settled = wait_for(lambda: _settled_outcome(ctx, base, index),
                       time.monotonic() + LEG_DEADLINE, interval=LEG_POLL)
    applied = wait_for(
        lambda: _point_value(_try_snapshot(ctx, base) or {},
                             targets['write']) == want or None,
        time.monotonic() + LEG_DEADLINE, interval=LEG_POLL)
    _, history_body = http_json(
        'GET', base + '/history?point=' + str(targets['watch'])
        + '&since=' + str(h0))
    seqs = _history_seqs(history_body, targets['watch'])
    failures += overlay.finish(ctx)

    tick1 = end.get('tick') or 0
    published1 = _publication(end).get('published') or 0
    if not seqs or any(not isinstance(seq, int) or seq <= h0
                       for seq in seqs):
        honesty = 'stale'
    elif seqs != list(range(seqs[0], seqs[0] + len(seqs))):
        honesty = 'dishonest'
    else:
        honesty = 'gapped' if seqs[0] > h0 + 1 else 'contiguous'
    signature = {
        'scan': tick1 > tick0 and tick1 - tick0 == published1 - published0,
        'history': honesty,
        'write': settled or 'never-settled',
        'reject': _outcome_key(rejected),
        'applied': bool(applied),
    }
    return signature, failures
PLANT_MAX_MESSAGE = 64 * 1024  # the protocol's documented line bound


def _quality_key(quality):
    """A served quality's comparable form: 'good', or
    'bad:<reason>'/'uncertain:<reason>' for a degraded stamp."""
    if quality == 'good':
        return 'good'
    if isinstance(quality, dict) and quality:
        name = next(iter(quality))
        return str(name) + ':' + str(quality[name])
    return 'unknown'


def _plant_connect(ctx, timeout=5):
    """A TCP connection to the run's published plant-protocol endpoint —
    ctx['plant'] carries the published 'host:port'."""
    host, _, port = str(ctx['plant']).rpartition(':')
    return socket.create_connection(
        (host or '127.0.0.1', int(port or 0)), timeout=timeout)


def _plant_request(stream, request):
    """One plant-protocol round trip: write the request object plus the
    newline delimiter, read back exactly one response line, enforcing
    the protocol's message bound."""
    stream.sendall(json.dumps(request).encode() + b'\n')
    line = b''
    while not line.endswith(b'\n'):
        chunk = stream.recv(PLANT_MAX_MESSAGE)
        if not chunk:
            raise ConnectionError('the plant server closed the '
                                  'connection mid-request')
        line += chunk
        if len(line) > PLANT_MAX_MESSAGE:
            raise ConnectionError('a plant response exceeded the '
                                  'protocol message bound')
    return json.loads(line)


def _plant_ctl(ctx, *args):
    """One `dcs-plant-ctl` invocation through the run's `plant_ctl`
    seam — the shipped plant-side tool, exec'd inside the plant
    container against its loopback listener, so the lane drives the
    binary the image carries rather than a second Python
    implementation of the wire protocol. The answer is the same
    response object the raw wire produced (`{"result":"done"}`,
    `{"result":"sample","sample":{...}}`, ...); a refused request or a
    failed invocation comes back error-shaped, `detail` carrying the
    tool's stderr."""
    run = ctx.get('plant_ctl')
    if run is None:
        raise ConnectionError('the run context carries no plant_ctl '
                              'seam for the shipped plant tool')
    result = run(*args)
    if result.returncode == 0:
        try:
            answer = json.loads(result.stdout)
        except ValueError:
            raise ConnectionError('dcs-plant-ctl printed an '
                                  'unparseable answer: '
                                  + str(result.stdout)[:300])
        if not isinstance(answer, dict):
            raise ConnectionError('dcs-plant-ctl printed a non-object '
                                  'answer: ' + str(result.stdout)[:300])
        return answer
    return {'result': 'error',
            'error': {'kind': 'tool_failed',
                      'detail': (result.stderr or '').strip()[:400],
                      'exit': result.returncode}}


def _try_plant_ctl(ctx, *args):
    """`_plant_ctl` or None — a refused invocation is one lost poll,
    not the leg's verdict (the `_try_plant` contract applied to the
    shipped tool's subcommands)."""
    try:
        answer = _plant_ctl(ctx, *args)
    except Exception:
        return None
    return answer if answer.get('result') != 'error' else None


def _plant_read(ctx, point):
    """The stored field sample for `point` — the shipped tool's
    `read`, answered as a `sample` result."""
    response = _plant_ctl(ctx, 'read', str(point))
    if response.get('result') != 'sample':
        raise ConnectionError('plant read on point ' + str(point)
                              + ' answered '
                              + json.dumps(response)[:300])
    return response.get('sample') or {}


def _field_inputs(ctx):
    """{point: PointInfo entry} for every 'in'-direction point the
    plant serves — the shipped tool's `list`, the field side's own
    account of what the scenario may fault."""
    response = _plant_ctl(ctx, 'list')
    if response.get('result') != 'points':
        raise ConnectionError('plant list answered '
                              + json.dumps(response)[:300])
    return {entry.get('point'): entry
            for entry in response.get('points') or []
            if entry.get('direction') == 'in'}


def _journal_point_changes(journal):
    """{point: [to-value, ...]} the point_changed entries a served
    `GET /journal` payload carries — the durable transition record the
    declared-journaled points land."""
    changes = {}
    for entry in _journal_list(journal):
        change = (entry.get('event') or {}).get('point_changed')
        if isinstance(change, dict):
            changes.setdefault(change.get('point'), []) \
                .append(change.get('to'))
    return changes


def _descriptor_ports(snapshot, kind, required):
    """(instance name, {port: bound point}) for the first served
    snapshot descriptor of `kind` whose ports all carry a bound point —
    the bound-point-annotated wiring the serving layer joins each port
    to — or (None, {}) when no instance serves the leg's contract."""
    for entry in snapshot.get('descriptors') or []:
        if entry.get('kind') != kind:
            continue
        ports = {port.get('name'): port.get('point')
                 for port in entry.get('ports') or []}
        if all(ports.get(name) is not None for name in required):
            return entry.get('name'), ports
    return None, {}


def _component_instance(schema, kind, alarm_point=None):
    """The served name of the `kind` component instance — when several
    instances share the kind, the one whose `alarm` resource binds
    `alarm_point` — or None. The `alarm` port is Status-role, so it
    lands in the interface's `state` collection, not `measurements`."""
    for entry in schema.get('interfaces') or []:
        interface = entry.get('interface') or {}
        if interface.get('kind') != kind:
            continue
        if alarm_point is None:
            return entry.get('name')
        resources = (interface.get('measurements') or []) \
            + (interface.get('state') or [])
        for resource in resources:
            if resource.get('name') == 'alarm' \
                    and resource.get('point') == alarm_point:
                return entry.get('name')
    return None


def _history_transitions(payload, point, since_tick):
    """[(tick, value)] — the value changes one point's /history samples
    carry at tick >= since_tick, in seq order; the first served sample
    anchors the pre-window value and never counts."""
    if not isinstance(payload, list):
        return []
    for entry in payload:
        if entry.get('point') != point:
            continue
        transitions = []
        previous = None
        anchored = False
        for sample in entry.get('samples') or []:
            body = sample.get('sample') or {}
            value = body.get('value')
            if isinstance(value, dict):
                value = next(iter(value.values()), None)
            if not anchored:
                previous, anchored = value, True
                continue
            if value != previous:
                tick = body.get('tick')
                if isinstance(tick, int) and tick >= since_tick:
                    transitions.append((tick, value))
                previous = value
        return transitions
    return []


def _nth_transition(transitions, value, n=1):
    """The tick of the n-th transition landing on `value`, or None."""
    seen = 0
    for tick, landed in transitions:
        if landed == value:
            seen += 1
            if seen == n:
                return tick
    return None
CLAIM_ROGUE = 0xF00E     # the rogue claim's token — likewise never an owner


def _submitted_receipt(ctx, base, index, command):
    """The receipt logged at absolute submission `index` once its
    outcome is terminal — None while it still reads `accepted` or the
    log cannot be read. `index` is the submission sequence the bounded
    window slides under — never a position in the tail."""
    try:
        receipts, base_index = _receipt_window(ctx, base)
    except Exception:
        return None
    position = index - base_index
    if not 0 <= position < len(receipts):
        return None
    receipt = receipts[position]
    if receipt.get('command') != command:
        return None
    if _outcome_key(receipt) == 'accepted':
        return None
    return receipt


def _tracking_peer(ctx, active):
    """The ctx endpoint key reporting role=standby with tracking
    convergence — the parity check's peer — or None when the pair
    has no tracking standby."""
    for name in ('active', 'standby', 'revised'):
        if name == active or ctx.get(name) is None:
            continue
        try:
            report = _role(ctx, ctx[name])
        except Exception:
            continue
        if report.get('role') == 'standby' \
                and 'tracking' in (report.get('sync') or {}):
            return name
    return None


# --------------------------------------------------------------------
# The peer-wait seams the monitoring-isolation contract owes the
# surviving monitor (WW-FND-004, decision 83's schedule extended to
# the two dead-peer findings): while the peer a standby pulls
# checkpoints from is unreachable, every request on the survivors —
# the sampled reads, the receipted command path, and a driven
# POST /scan batch whose per-scan pulls each wait the documented pull
# bound — must answer inside a declared bound rather than serialize
# behind the fetch. The run's third monitor is launched --driven
# through the runner-owned start_driven/stop_driven actions: an
# externally paced standby whose pulls happen only inside POST /scan,
# so a batch on it occupies exactly the worker the finding pinned.
# The pair-health surface is the page's own pairHealth rule applied
# to the /role reports the scenario polls — the unreachable peer, the
# degraded convergence, and the still-unsynchronized-past-grace
# standby are all named faults, never "redundant pair healthy".

LATENCY_BOUND = 2.0      # the declared per-request bound — under the
                         # ~3s dead-peer serialization the fixes removed


def _timed(call):
    """Run call(); return (result, elapsed-seconds)."""
    started = time.monotonic()
    return call(), time.monotonic() - started


def _try_role(ctx, base):
    """GET /role on a monitor, or None when the peer does not answer."""
    try:
        return _role(ctx, base)
    except Exception:
        return None


def _pair_active(ctx):
    """The launched pair's field-owning endpoint key, or None —
    _settled_active scoped to the pair: a third endpoint's active is
    not this leg's race target."""
    for name in ('active', 'standby'):
        if ctx.get(name) is None:
            continue
        try:
            if _role(ctx, ctx[name]).get('role') == 'active':
                return name
        except Exception:
            pass
    return None


def _tracking_standby(ctx, name):
    """The endpoint's report while it is a tracking standby — the
    promotable posture a raced switch needs — else None."""
    try:
        report = _role(ctx, ctx[name])
    except Exception:
        return None
    if report.get('role') == 'standby' \
            and 'tracking' in (report.get('sync') or {}):
        return report
    return None


def _settle_call(url):
    """POST a control-plane request — /demote, /promote — answering
    (status, body) with a refused call's named outcome decoded rather
    than raised."""
    try:
        return http_json('POST', url)
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b'null')
        except ValueError:
            body = None
        finally:
            exc.close()
        return exc.code, body


def _writable_bool_points(signals, count):
    """Up to `count` distinct writable bool `in` points a SignalIndex
    serves — the raced submissions' targets, the scenarios' p101-oos
    point first when the model declares it."""
    ranked = []
    for entry in signals.get('points', []):
        if entry.get('writable') and entry.get('direction') == 'in' \
                and entry.get('value_type') == 'bool' \
                and isinstance(entry.get('point'), int):
            ranked.append((0 if entry.get('name') == 'p101-oos' else 1,
                           entry['point']))
    return [point for _, point in sorted(ranked)][:count]


def _admission_hit(receipt, admission):
    """Whether a served or journaled receipt is the raced admission —
    the (command, actor) pair is unique per raced submission."""
    return isinstance(receipt, dict) \
        and receipt.get('command') == admission['command'] \
        and receipt.get('actor') == admission['actor']


def _settle_window(ctx, admission, floors):
    """One polled audit snapshot for a raced admission: each launched
    peer's journaled command_settled receipts, adopted-log receipts,
    and served image value for the admission's point since the pass's
    journal floors. None while either peer's monitor drops a read — a
    lost observation, never the audit's verdict."""
    window = {'journaled': {}, 'logged': {}, 'image': {}}
    for name in ('active', 'standby'):
        try:
            _, journal = http_json('GET', ctx[name] + '/journal?since='
                                   + str(floors[name]))
            _, receipts = http_json('GET', ctx[name] + '/receipts')
            snapshot = _snapshot(ctx, ctx[name])
        except Exception:
            return None
        window['journaled'][name] = [
            receipt for receipt in
            (_journal_settled(entry) for entry in _journal_list(journal))
            if _admission_hit(receipt, admission)]
        window['logged'][name] = [
            receipt for receipt in _receipt_list(receipts)
            if _admission_hit(receipt, admission)]
        window['image'][name] = _point_value(snapshot, admission['point'])
    return window


def _settle_resolved(window, admission):
    """Whether the audit snapshot is terminal for the admission: the
    demoted peer's journal has reached an outcome and the promoted
    peer's evidence agrees — or a contradiction is already showing,
    which no further wait can heal."""
    demoted, promoted = admission['demoted'], admission['promoted']
    counts = {name: len(entries)
              for name, entries in window['journaled'].items()}
    outcomes = {_outcome_key(receipt)
                for entries in window['journaled'].values()
                for receipt in entries}
    if not outcomes:
        return False
    if len(outcomes) != 1:
        return True           # two terminal outcomes — the defect
    outcome = next(iter(outcomes))
    if outcome not in ('applied', 'rejected:superseded'):
        return True           # an out-of-contract outcome is terminal
    if any(_outcome_key(receipt) not in (outcome, 'accepted')
           for entries in window['logged'].values()
           for receipt in entries):
        return True           # the adopted logs already contradict
                              # the journaled outcome
    if outcome == 'applied':
        # Applied journals once on each peer — the settler's own
        # boundary plus the adopted record — and each adopted log
        # carries the receipt settled. Over-journaling is terminal
        # too: a settle a peer journaled twice no wait heals.
        if counts[demoted] > 1 or counts[promoted] > 1:
            return True
        return counts[demoted] == 1 and counts[promoted] == 1 \
            and len(window['logged'][demoted]) == 1 \
            and len(window['logged'][promoted]) == 1 \
            and all(_outcome_key(receipt) == 'applied'
                    for name in (demoted, promoted)
                    for receipt in window['logged'][name])
    # Superseded journals once on the demoted peer alone — the
    # promoted run never held the admission — while no adopted log may
    # still hold the admission pending.
    if counts[promoted] > 0 or counts[demoted] > 1:
        return True
    return counts[demoted] == 1 \
        and not any(_outcome_key(receipt) == 'accepted'
                    for entries in window['logged'].values()
                    for receipt in entries)


def _settle_judge(window, admission, note, check_image=True):
    """The admission's audit verdict once its window resolved —
    records a violation for every clause the evidence breaks and
    answers 'single' or 'diverged' for the digest."""
    demoted = admission['demoted']
    label = 'admission ' + str(admission['actor']) + ' (point ' \
        + str(admission['point']) + ')'
    outcomes = {_outcome_key(receipt)
                for entries in window['journaled'].values()
                for receipt in entries}
    if len(outcomes) != 1:
        note('contradictory-' + admission['actor'],
             'demote-settle-uniqueness-nondeterministic',
             label + ' journaled ' + json.dumps(sorted(outcomes))
             + ' — one admission, never more than one terminal '
             'outcome')
        return 'diverged'
    outcome = next(iter(outcomes))
    if outcome not in ('applied', 'rejected:superseded'):
        note('outcome-' + admission['actor'],
             'demote-settle-uniqueness-nondeterministic',
             label + ' settled ' + outcome + ' — neither applied nor '
             'the named superseded rejection')
        return 'diverged'
    verdict = 'single'
    # The journal coverage: the demoted peer journals the settle once
    # either way; the promoted peer journals it once only when the
    # line applied it — it never held a superseded admission.
    for name in ('active', 'standby'):
        count = len(window['journaled'][name])
        want = 1 if name == demoted or outcome == 'applied' else 0
        if count != want:
            verdict = 'diverged'
            note('journal-' + admission['actor'] + '-' + name,
                 'demote-settle-uniqueness-nondeterministic',
                 label + ' journaled ' + str(count)
                 + ' command_settled records on ' + name
                 + ' — the contract settles exactly ' + str(want)
                 + ' there')
    # The adopted logs: an applied admission's receipt settles applied
    # in both peers' logs; a superseded admission carries no pending
    # copy anywhere — the same single outcome in each peer's log.
    for name in ('active', 'standby'):
        logged = window['logged'][name]
        if len(logged) > 1:
            verdict = 'diverged'
            note('log-count-' + admission['actor'] + '-' + name,
                 'demote-settle-uniqueness-nondeterministic',
                 name + "'s adopted log carries " + str(len(logged))
                 + ' receipts for ' + label)
        for receipt in logged:
            if _outcome_key(receipt) != outcome:
                verdict = 'diverged'
                note('log-outcome-' + admission['actor'] + '-' + name,
                     'demote-settle-uniqueness-nondeterministic',
                     name + "'s adopted log carries "
                     + _outcome_key(receipt) + ' for ' + label
                     + ' where the journal settled ' + outcome)
        if outcome == 'applied' and not logged:
            verdict = 'diverged'
            note('log-missing-' + admission['actor'] + '-' + name,
                 'demote-settle-uniqueness-nondeterministic',
                 name + "'s adopted log lost " + label
                 + ' the journal settled applied')
    # The image: the applied admission's value present on both peers,
    # the superseded admission's value on neither — at most one
    # application per raced admission.
    if check_image:
        for name in ('active', 'standby'):
            value = window['image'][name]
            if outcome == 'applied' and value != admission['value']:
                verdict = 'diverged'
                note('image-missing-' + admission['actor'] + '-'
                     + name,
                     'demote-settle-uniqueness-nondeterministic',
                     name + "'s image never took " + label
                     + "'s applied write — serves "
                     + json.dumps(value))
            if outcome == 'rejected:superseded' \
                    and value == admission['value']:
                verdict = 'diverged'
                note('image-phantom-' + admission['actor'] + '-'
                     + name,
                     'demote-settle-uniqueness-nondeterministic',
                     name + "'s image carries " + label
                     + "'s superseded value — an application the "
                     'journal never settled')
    return verdict

__all__ = [name for name in globals() if not name.startswith('__')]
