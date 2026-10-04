"""The lane-owned derived rig models: the run-local plant documents the
QA lane derives from a checked-in fixture so a leg can exercise a
capability the fixture does not declare.

`revision.py` covers the *rolling revision* recipe — an additive,
internal-only change a promoted peer rolls in, deliberately barred from
touching the run's field wiring so the promoted peer's field outputs
continue the run's. The legs that need a richer field than the pinned
fixtures declare need the opposite: a **per-run variant the lane
mounts in the run's place**. That is a different seam with a different
safety rule, so it is a different module.

Two variants, both derived here rather than checked in:

`two_station_cyclic(document)`
    the rig's `sim-cyclic` document with a **second station** added to
    the declared station map, so a station-attributed short exchange
    can degrade one station's points while the other station's stay
    fresh. One station cannot express the distinction: every point
    belongs to it, so a station shortfall and a whole-bus shortfall
    read identically. This is what makes decision 78's attribution
    clause observable rather than vacuous on the rig.

`writable_field_point(document, ...)`
    a document plus one **channel-bound writable `In` point** — a
    field input an operator write is forwarded to the driver at, and
    which the same scan's input phase reads back (the model's
    documented operator substitution of the input image, holding until
    the field side asserts a different value). Every writable point in
    the pinned fixtures is internal and channel-less, so a receipted
    write to one never reaches a driver write and the applied-mint plus
    `DriverRejected` verdict path is unexercisable on this plant.

Why derived and not checked in: the fixtures are emitted artifacts,
byte-pinned by the build tests and re-emitted identically by
`dcs-build`, so neither can be edited. Deriving keeps the derivation
reviewable as data-in-code while the pinned artifact stays untouched,
which is the same trade `revision.py` makes for the revision recipe.

Neither variant removes or retypes an element: both are additive, and
both are re-checked against `revision.lint`'s structural rules before
they are returned, so a broken derivation fails with a named problem
rather than shipping a document the controller would reject at load.
`writable_field_point` additionally keeps the added point `In`-only —
`revision._check_point`'s rule, asserted here — because a writable `Out`
point has no field side to observe a driver write on.
"""
import copy
import json

from . import revision

#: The prefix a derived station's name carries, so a derived station is
#: recognizable in a report and in a device server's own station map.
DERIVED_STATION = '750-354-b'

#: The channel and point a two-station cyclic derivation adds: a third
#: digital input on the added station, so the added station owns at
#: least one point a station-attributed shortfall can degrade while the
#: original station's stay fresh.
EXTRA_CYCLIC_CHANNEL = 'di3'


class RigModelError(ValueError):
    """A derivation the lane cannot make from the mounted fixture — the
    named problem, not a bare parse failure."""


def _device(document, device_id):
    """The mounted model's device `device_id`, or the named reason it is
    not there."""
    for declared in document.get('devices') or []:
        if isinstance(declared, dict) and declared.get('id') == device_id:
            return declared
    raise RigModelError('the mounted model declares no device '
                        + str(device_id))


def _max_id(elements):
    """The largest declared id in `elements`, or 0 when it declares
    none — derived ids start above it so assembly's synthesized-link
    allocation base does not shift."""
    ids = [element.get('id') for element in elements
           if isinstance(element, dict) and isinstance(element.get('id'), int)]
    return max(ids) if ids else 0


def _input_channel_kind(device):
    """The value kind of an input channel the device already declares
    — the kind a derived input channel mirrors. None when the device
    declares no input channel at all."""
    for entry in (device.get('channels') or {}).values():
        if isinstance(entry, dict) and entry.get('direction') == 'in':
            kind = entry.get('value_type')
            if kind in revision._VALUE_KINDS:
                return kind
    return None


def _require(document):
    """The mounted document's shape the derivations read, checked once
    so both report the same named problem."""
    if not isinstance(document, dict):
        raise RigModelError('the mounted model is not a JSON object')
    for section in ('devices', 'io_points', 'signals'):
        if not isinstance(document.get(section), list):
            raise RigModelError('the mounted model lacks a ' + section
                                + ' list')
    return document


def _finish(revised):
    """Re-check a derived document against the structural rules a
    derivation can break, so the caller gets a document or a named
    problem."""
    findings = revision.lint(revised)
    if findings:
        raise RigModelError('the derived document fails validation: '
                            + '; '.join(findings[:5]))
    return revised


def two_station_cyclic(document, device_id=1):
    """The mounted cyclic document with a second station on `device_id`.

    The added station gets one digital input channel, its own io_point,
    and its own monitoring signal; nothing existing moves. Returns the
    derived document with the input point id under
    `'station_point'` so a caller can address the station the
    attribution clause degrades.

    The mounted document's first station — the coupler station the rig
    manifest records — is left exactly as declared, so its points are
    the ones a station-attributed shortfall must *not* degrade.
    """
    _require(document)
    device = _device(document, device_id)
    if device.get('kind') != 'sim-cyclic':
        raise RigModelError('the mounted device ' + str(device_id)
                            + ' is a ' + repr(device.get('kind'))
                            + ', not sim-cyclic — a station-attributed '
                            'short exchange only exists on a cyclic '
                            'device')
    stations = ((device.get('parameters') or {}).get('stations') or {})
    if not stations:
        raise RigModelError('the mounted sim-cyclic device declares no '
                            'stations map to extend')
    if DERIVED_STATION in stations:
        raise RigModelError('the mounted model already declares the '
                            'derived station ' + DERIVED_STATION)
    channels = device.get('channels') or {}
    if EXTRA_CYCLIC_CHANNEL in channels:
        raise RigModelError('the mounted device already declares the '
                            'channel ' + EXTRA_CYCLIC_CHANNEL)
    point_id = _max_id(document['io_points']) + 1
    signal_id = _max_id(document['signals']) + 1

    derived = copy.deepcopy(document)
    target = _device(derived, device_id)
    target['channels'][EXTRA_CYCLIC_CHANNEL] = {
        'direction': 'in', 'value_type': 'bool'}
    # A fresh register offset: the derived station answers registers of
    # its own, so a shortfall on it leaves the coupler station's
    # registers untouched.
    registers = [value for station in stations.values()
                 for value in station.values()
                 if isinstance(value, int) and not isinstance(value, bool)]
    target['parameters']['stations'][DERIVED_STATION] = {
        EXTRA_CYCLIC_CHANNEL: (max(registers) + 1) if registers else 0}
    derived['io_points'].append({
        'id': point_id, 'direction': 'in', 'value_type': 'bool',
        'channel': {'device': device_id, 'name': EXTRA_CYCLIC_CHANNEL}})
    derived['signals'].append({
        'id': signal_id, 'name': 'rig-' + EXTRA_CYCLIC_CHANNEL,
        'source': point_id, 'unit': '',
        'description': 'lane-derived second-station digital input — '
                       'exists so a station-attributed short exchange '
                       'can degrade one station while the coupler '
                       'station stays fresh; not a rig channel',
        'group': 'wago-rig'})
    _finish(derived)
    return {'document': derived,
            'station': DERIVED_STATION,
            'station_point': point_id,
            'station_channel': EXTRA_CYCLIC_CHANNEL,
            'device': device_id}


def writable_field_point(document, device_id, channel, point_id=None,
                         signal_id=None, name=None):
    """The mounted document plus one channel-bound writable `In` point.

    `device_id`/`channel` name an input channel. When the mounted model
    declares that channel and nothing binds it, the derivation binds
    it as it stands. When it is already bound — the pinned fixtures bind
    every channel their devices declare — the derivation **adds** the
    channel to the same device instead, so the added point rides the
    driver the pair already scans rather than a device of its own. A
    driver rejects a channel bound twice, so a derived point cannot
    share one.

    The added point is `In`-only: a writable `Out` point has no field
    side to observe a driver write on, and the model's contract writes
    a writable field input back through the same scan's input phase.
    """
    _require(document)
    device = _device(document, device_id)
    channels = device.get('channels') or {}
    bound = set()
    for point in document['io_points']:
        if not isinstance(point, dict):
            continue
        bound_channel = point.get('channel') or {}
        bound.add((bound_channel.get('device'),
                   bound_channel.get('name')))
    declared = channels.get(channel)
    added_channel = False
    if isinstance(declared, dict):
        if declared.get('direction') != 'in':
            raise RigModelError('the mounted channel ' + repr(channel)
                                + ' is an ' + repr(declared.get('direction'))
                                + ' — a writable field point must ride '
                                'an input channel, whose field side the '
                                'write is observable on')
        value_type = declared.get('value_type')
        if (device_id, channel) in bound:
            added_channel = True
    elif channel in channels:
        raise RigModelError('the mounted channel ' + repr(channel)
                            + ' declares no usable shape')
    else:
        # Not declared at all: add it, mirroring an input channel of the
        # same value kind the device already carries so the derived
        # document adds nothing the device's own kind does not accept.
        value_type = _input_channel_kind(device)
        added_channel = True
    if value_type not in revision._VALUE_KINDS:
        raise RigModelError('the mounted channel ' + repr(channel)
                            + ' declares no usable value_type')
    taken = {point.get('id') for point in document['io_points']
             if isinstance(point, dict)}
    pid = point_id if point_id is not None else _max_id(document['io_points']) + 1
    if pid in taken:
        raise RigModelError('the derived writable field point id '
                            + str(pid) + ' collides with the mounted model')
    sid = signal_id if signal_id is not None else _max_id(document['signals']) + 1
    signal_name = name or ('qa-writable-' + str(channel))
    names = {signal.get('name') for signal in document['signals']
             if isinstance(signal, dict)}
    if signal_name in names:
        raise RigModelError('the derived signal name ' + repr(signal_name)
                            + ' collides with the mounted model')

    derived = copy.deepcopy(document)
    if added_channel:
        _device(derived, device_id)['channels'][channel] = {
            'direction': 'in', 'value_type': value_type}
    point = {'id': pid, 'direction': 'in', 'value_type': value_type,
             'channel': {'device': device_id, 'name': channel},
             'writable': True}
    derived['io_points'].append(point)
    derived['signals'].append({
        'id': sid, 'name': signal_name, 'source': pid, 'unit': '',
        'description': 'lane-derived command-writable field input — a '
                       'receipted write on this point is forwarded to the '
                       'driver and read back through the same scan; not '
                       'a rig channel',
        'group': 'qa-lane'})
    _finish(derived)
    return {'document': derived, 'point': pid, 'signal': sid,
            'name': signal_name, 'channel': channel, 'device': device_id,
            'value_type': value_type, 'added_channel': added_channel}


def describe(derivation):
    """The derivation's report record: what it added and where, so a
    report's evidence names the lane-owned document rather than an
    opaque path."""
    return {key: value for key, value in derivation.items()
            if key != 'document'}