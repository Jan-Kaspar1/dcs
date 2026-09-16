"""The checked-in rolling model-revision recipe.

`revision.json` beside this module is the recipe the lane applies to a
run's mounted plant model to produce the revised document the third
controller rolls in on: a recorded, additive change — one writable
internal point plus its monitoring signal — so the derived document
stays reviewable and identical across runs, and the `--revised` peer's
carryover report names exactly the added point under `initialized`.

The recipe is deliberately additive-only and internal-only: it cannot
remove or retype an element and it cannot bind a channel, so a clean
derivation can never touch the run's field wiring — the promoted
peer's field outputs continue the run's. Added point ids sit below the
mounted model's maximum declared id so assembly's synthesized-link
allocation base does not shift and no unrelated element is renamed
across the boundary.

`lint` re-checks the derived document against the structural rules the
recipe can break — the subset of the model validator covering unique
ids, signal source resolution, and the internal-point
`initial`/`writable`/`journaled` rules — so a broken recipe or a
surprising base model fails the derivation with a named problem rather
than shipping a document the third controller would reject at load.
"""
import copy
import json
from pathlib import Path

#: The checked-in recipe beside this module.
RECIPE_PATH = Path(__file__).with_name('revision.json')

_RECIPE_KEYS = {'description', 'add_io_points', 'add_signals'}
_POINT_KEYS = {'id', 'direction', 'value_type', 'channel', 'initial',
               'writable', 'journaled', 'stale_after_ticks'}
_SIGNAL_KEYS = {'id', 'name', 'source', 'unit', 'description', 'group'}
_VALUE_KINDS = ('bool', 'int', 'float')
_MODEL_SECTIONS = ('version', 'devices', 'io_points', 'signals',
                   'components', 'connections')


class RevisionError(ValueError):
    """A recipe or derived document that cannot roll — the named
    problem, not a bare parse failure."""


def load_recipe(path=None):
    """Read and shape-check the revision recipe (defaults to the
    checked-in `revision.json`)."""
    path = Path(path or RECIPE_PATH)
    try:
        recipe = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise RevisionError('cannot read revision recipe ' + str(path)
                            + ': ' + str(exc))
    if not isinstance(recipe, dict) or not set(recipe) <= _RECIPE_KEYS:
        raise RevisionError('revision recipe ' + str(path) + ' keys '
                            'must be a subset of '
                            + str(sorted(_RECIPE_KEYS)))
    for name in ('add_io_points', 'add_signals'):
        if not isinstance(recipe.get(name, []), list):
            raise RevisionError('revision recipe ' + str(path) + ': '
                                + name + ' must be a list')
    return recipe


def _check_point(point, known_ids):
    """Shape-check one recipe io_point against the model rules the
    recipe could break and against the mounted model's ids."""
    if not isinstance(point, dict) or not set(point) <= _POINT_KEYS:
        raise RevisionError('recipe io_point must be an object with '
                            'io_point fields only: '
                            + json.dumps(point, sort_keys=True)[:200])
    pid = point.get('id')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise RevisionError('recipe io_point id must be a positive '
                            'integer')
    if pid in known_ids:
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' collides with the mounted model')
    if 'channel' in point:
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' binds a channel — the recipe may only '
                            'add internal points')
    direction = point.get('direction')
    if direction not in ('in', 'out'):
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' direction must be in|out')
    value_type = point.get('value_type')
    if value_type not in _VALUE_KINDS:
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' value_type must be bool|int|float')
    initial = point.get('initial')
    if not isinstance(initial, dict) or set(initial) != {value_type}:
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' must declare exactly one {' + value_type
                            + ': ...} initial value')
    value = initial[value_type]
    valid = ((value_type == 'bool' and isinstance(value, bool))
             or (value_type == 'int' and isinstance(value, int)
                 and not isinstance(value, bool))
             or (value_type == 'float'
                 and isinstance(value, (int, float))
                 and not isinstance(value, bool)))
    if not valid:
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' initial value is not a ' + value_type)
    if point.get('writable') and direction != 'in':
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' declares writable on a non-in point')
    if point.get('journaled') and value_type == 'float':
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' declares journaled on a float point')
    if 'stale_after_ticks' in point:
        raise RevisionError('recipe io_point ' + str(pid)
                            + ' declares stale_after_ticks — the '
                            'freshness budget only exists on field '
                            'inputs')


def _check_signal(signal, known_ids, known_names, point_ids):
    """Shape-check one recipe signal."""
    if not isinstance(signal, dict) or not set(signal) <= _SIGNAL_KEYS:
        raise RevisionError('recipe signal must be an object with '
                            'signal fields only: '
                            + json.dumps(signal, sort_keys=True)[:200])
    sid = signal.get('id')
    if not isinstance(sid, int) or isinstance(sid, bool) or sid <= 0:
        raise RevisionError('recipe signal id must be a positive '
                            'integer')
    if sid in known_ids:
        raise RevisionError('recipe signal ' + str(sid)
                            + ' collides with the mounted model')
    name = signal.get('name')
    if not isinstance(name, str) or not name.strip():
        raise RevisionError('recipe signal ' + str(sid)
                            + ' must declare a non-empty name')
    if name in known_names:
        raise RevisionError('recipe signal name ' + repr(name)
                            + ' collides with the mounted model')
    source = signal.get('source')
    if source not in point_ids:
        raise RevisionError('recipe signal ' + str(sid)
                            + ' sources unknown io point '
                            + str(source))
    for key in ('unit', 'description', 'group'):
        if key in signal and not isinstance(signal[key], str):
            raise RevisionError('recipe signal ' + str(sid) + ' ' + key
                                + ' must be a string')


def derive(document, recipe):
    """Apply `recipe` to `document` (the parsed mounted model) and
    return the revised document.

    Additive-only: every named point and signal is appended after
    shape-checks; nothing existing is mutated or removed. Raises
    RevisionError on any collision or rule break; the input document is
    left untouched.
    """
    if not isinstance(document, dict):
        raise RevisionError('the mounted model is not a JSON object')
    points = document.get('io_points')
    signals = document.get('signals')
    if not isinstance(points, list) or not isinstance(signals, list):
        raise RevisionError('the mounted model lacks io_points/signals '
                            'lists')
    revised = copy.deepcopy(document)
    point_ids = {p.get('id') for p in points if isinstance(p, dict)}
    signal_ids = {s.get('id') for s in signals if isinstance(s, dict)}
    names = {s.get('name') for s in signals if isinstance(s, dict)}
    for point in recipe.get('add_io_points', []):
        _check_point(point, point_ids)
        point_ids.add(point['id'])
        revised['io_points'].append(copy.deepcopy(point))
    for signal in recipe.get('add_signals', []):
        _check_signal(signal, signal_ids, names, point_ids)
        signal_ids.add(signal['id'])
        names.add(signal['name'])
        revised['signals'].append(copy.deepcopy(signal))
    findings = lint(revised)
    if findings:
        raise RevisionError('the derived document fails validation: '
                            + '; '.join(findings[:5]))
    return revised


def lint(document):
    """The lane's structural check on a derived document — the subset
    of the model validator's rules covering everything the recipe can
    touch: required sections, unique ids, signal source resolution,
    and the internal-point initial/writable/journaled rules. Returns a
    list of problem strings; empty means clean."""
    problems = []
    if not isinstance(document, dict) \
            or not set(_MODEL_SECTIONS) <= set(document):
        return ['document lacks a required top-level section '
                + str(sorted(_MODEL_SECTIONS))]
    for collection in ('devices', 'io_points', 'signals', 'components'):
        seen = set()
        for element in document[collection]:
            eid = element.get('id') if isinstance(element, dict) else None
            if eid in seen:
                problems.append('duplicate ' + collection + ' id '
                                + str(eid))
            seen.add(eid)
    point_ids = {p.get('id') for p in document['io_points']
                 if isinstance(p, dict)}
    for point in document['io_points']:
        if not isinstance(point, dict):
            problems.append('io_points entry is not an object')
            continue
        pid = point.get('id')
        if 'channel' not in point:
            initial = point.get('initial')
            if not isinstance(initial, dict) or len(initial) != 1:
                problems.append('internal io point ' + str(pid)
                                + ' declares no initial value')
            elif next(iter(initial)) != point.get('value_type'):
                problems.append('internal io point ' + str(pid)
                                + ' initial kind '
                                + str(next(iter(initial)))
                                + ' mismatches value_type '
                                + str(point.get('value_type')))
        if point.get('writable') and point.get('direction') == 'out':
            problems.append('io point ' + str(pid)
                            + ' declares writable on an out point')
        if point.get('journaled') and point.get('value_type') == 'float':
            problems.append('io point ' + str(pid)
                            + ' declares journaled on a float point')
    for signal in document['signals']:
        if not isinstance(signal, dict):
            problems.append('signals entry is not an object')
            continue
        if signal.get('source') not in point_ids:
            problems.append('signal ' + str(signal.get('id'))
                            + ' sources unknown io point '
                            + str(signal.get('source')))
    return problems


def derive_revised_model(model_path, out_path, recipe_path=None):
    """Read the run's mounted model, apply the checked-in recipe, and
    write the revised document to `out_path` with deterministic
    serialization. Returns the derivation summary the scenario and
    timeline need."""
    recipe = load_recipe(recipe_path)
    try:
        document = json.loads(Path(model_path).read_text())
    except (OSError, ValueError) as exc:
        raise RevisionError('cannot read the mounted model '
                            + str(model_path) + ': ' + str(exc))
    revised = derive(document, recipe)
    Path(out_path).write_text(json.dumps(revised, indent=1,
                                         sort_keys=True) + '\n')
    return {'document': str(out_path),
            'added_points': [p['id']
                             for p in recipe.get('add_io_points', [])],
            'added_signals': [s['id']
                              for s in recipe.get('add_signals', [])]}
