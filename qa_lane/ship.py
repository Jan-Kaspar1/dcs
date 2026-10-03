"""The QA lane's shipped-binary contract.

`ship.json` beside this module is the single record of what the lane's
bounded image build produces out of the revision under test and what
rides inside each image it packages: the compile groups the builder
container runs in `/src`, the binaries those groups put in `release/`,
the two runtime images with their entrypoints and the extra binaries
each carries beside it, and the host-side tools that stay out of both
images. `_build_images` derives its builder command, each image's
payload, and its presence assertions from this document, so a shipped
binary cannot be added without a compile target and an image cannot
lose one silently.

The document is data rather than code because it ships inside the
revision's own `git archive` — `src/qa_lane/ship.json` — while the lane
code is pinned on the host separately from the revision under test
(`qa_lane/deploy/README.md`). That pin is how the recorded contract
failed to reach the run's images: the three exploration runs at cabe3b3
tested a revision carrying both #1368's sim-bus ship list and #654's
`dcs-plant-ctl` precedent, but the deployed lane copy that built their
images predated them, so both images carried their entrypoint alone and
the sim-bus, keyed-interposer, and claim-probing legs fell back to
bind-mounting the host build cache's binaries. `check_lane_copy` is the
half a pinned copy can still own: it reads the contract the tested
revision records and refuses the run by name when the deployed copy
ships less, before a leg spends the run discovering a phantom tool. A
revision predating this document records none, and is left alone.
"""
import json
from pathlib import Path

#: The checked-in contract beside this module.
CONTRACT_PATH = Path(__file__).with_name('ship.json')

#: The revision-relative path a source tree carries the contract at, so
#: a run's own `git archive` is where the tested revision's record of
#: its shipped binaries is read from.
REVISION_PATH = ('qa_lane', 'ship.json')

_KEYS = {'description', 'compile_groups', 'images', 'host_tools'}
_GROUP_KEYS = {'cargo', 'produces'}
_IMAGE_KEYS = {'name', 'tag', 'entrypoint', 'ships'}
#: The image pair the run report's digest channel fixes: `image` is
#: exactly {"controller", "plant"}, so the contract may not name a third
#: image or drop one of these.
IMAGE_NAMES = {'controller', 'plant'}
_BINARY_CHARS = frozenset('abcdefghijklmnopqrstuvwxyz0123456789-_.')
#: Every generated image's install prefix — the runtime stage copies
#: each shipped binary here, which is where the lane execs a `--bin`
#: tool by name inside the container.
INSTALL_PREFIX = '/usr/local/bin/'


class ShipError(ValueError):
    """A shipped-binary contract that cannot be honored — the named
    problem (the ship-map gap, the staged payload, the pinned lane copy
    behind the revision), not a bare parse failure."""


def _read(path, required=True):
    path = Path(path)
    if not path.is_file():
        if required:
            raise ShipError('cannot read shipped-binary contract '
                            + str(path))
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise ShipError('cannot read shipped-binary contract '
                        + str(path) + ': ' + str(exc))


def _binary_name(name, where):
    if not isinstance(name, str) or not name or not set(name) \
            <= _BINARY_CHARS or name[0] in '-_.':
        raise ShipError(where + ' names no usable binary: '
                        + json.dumps(name))
    return name


def load(path=None):
    """Read and shape-check the shipped-binary contract (defaults to
    the checked-in `ship.json`), returning it unchanged. The checks are
    the ones the contract's own consumers depend on: cargo argument
    lists the builder can run, unique binary names per compile group,
    the two report-fixed image names each with a tag, an entrypoint,
    and a `ships` list that never repeats itself or the entrypoint, and
    host tools. A malformed document fails here by name rather than
    producing a builder command or an image that cannot carry it."""
    contract = _read(path or CONTRACT_PATH)
    if not isinstance(contract, dict) or not set(contract) <= _KEYS \
            or not {'compile_groups', 'images', 'host_tools'} \
            <= set(contract):
        raise ShipError('shipped-binary contract keys must be a subset '
                        'of ' + str(sorted(_KEYS)) + ' and must declare '
                        'compile_groups, images, and host_tools')
    groups = contract['compile_groups']
    if not isinstance(groups, list) or not groups:
        raise ShipError('shipped-binary contract compile_groups must be '
                        'a non-empty list')
    for index, group in enumerate(groups):
        where = 'compile_groups[' + str(index) + ']'
        if not isinstance(group, dict) or set(group) != _GROUP_KEYS:
            raise ShipError(where + ' must declare exactly cargo and '
                            'produces')
        if not isinstance(group['cargo'], str) or not group['cargo'].strip():
            raise ShipError(where + ' cargo must be a non-empty argument '
                            'list')
        if not isinstance(group['produces'], list) or not group['produces']:
            raise ShipError(where + ' produces must name at least one '
                            'binary')
        for name in group['produces']:
            _binary_name(name, where + ' produces')
        if len(set(group['produces'])) != len(group['produces']):
            raise ShipError(where + ' produces names a binary twice')
    images = contract['images']
    if not isinstance(images, list) or not images:
        raise ShipError('shipped-binary contract images must be a '
                        'non-empty list')
    seen_names, seen_entrypoints = set(), set()
    for index, image in enumerate(images):
        where = 'images[' + str(index) + ']'
        if not isinstance(image, dict) or set(image) != _IMAGE_KEYS:
            raise ShipError(where + ' must declare exactly name, tag, '
                            'entrypoint, and ships')
        name = image['name']
        if name not in IMAGE_NAMES:
            raise ShipError(where + ' names unknown image ' + repr(name)
                            + ' — the run report fixes the digest pair '
                            + str(sorted(IMAGE_NAMES)))
        if name in seen_names:
            raise ShipError('shipped-binary contract names image '
                            + repr(name) + ' twice')
        seen_names.add(name)
        if not isinstance(image['tag'], str) or not image['tag'].strip():
            raise ShipError(where + ' tag must be a non-empty string')
        _binary_name(image['entrypoint'], where)
        if image['entrypoint'] in seen_entrypoints:
            raise ShipError('shipped-binary contract gives image '
                            + repr(name) + ' the entrypoint another '
                            'image already serves: ' + image['entrypoint'])
        seen_entrypoints.add(image['entrypoint'])
        if not isinstance(image['ships'], list):
            raise ShipError(where + ' ships must be a list of the extra '
                            'binaries beside the entrypoint')
        for shipped in image['ships']:
            _binary_name(shipped, where + ' ships')
        if len(set(image['ships'])) != len(image['ships']) \
                or image['entrypoint'] in image['ships']:
            raise ShipError(where + ' ships a binary twice or its own '
                            'entrypoint')
    if seen_names != IMAGE_NAMES:
        raise ShipError('shipped-binary contract records no image for '
                        + ', '.join(sorted(IMAGE_NAMES - seen_names)))
    tools = contract['host_tools']
    if not isinstance(tools, list):
        raise ShipError('shipped-binary contract host_tools must be a list')
    for tool in tools:
        _binary_name(tool, 'host_tools')
    if len(set(tools)) != len(tools):
        raise ShipError('shipped-binary contract names a host tool twice')
    return contract


def compile_command(contract):
    """The bounded builder's cargo chain: one `cargo build --release
    --locked` invocation per recorded group, in recorded order, joined
    so a failing group stops the rest."""
    return ' && '.join('cargo build --release --locked ' + group['cargo']
                       for group in contract['compile_groups'])


def compiled(contract):
    """Every binary name the recorded compile groups produce — the set
    the contract's images and host tools must draw from."""
    return {name for group in contract['compile_groups']
            for name in group['produces']}


def payload(contract):
    """{image name: [entrypoint, *shipped]} — the binaries each built
    image must carry, its entrypoint first."""
    return {image['name']: [image['entrypoint']] + list(image['ships'])
            for image in contract['images']}


def assert_complete(contract):
    """Refuse a contract naming a binary no compile group produces: the
    ship-map gap. Carrying a binary in an image (or handing it to a
    host-side case) without a target that builds it is a payload no run
    can produce, so it fails here by name instead of leaving a leg to
    discover the phantom tool."""
    built = compiled(contract)
    gaps = []
    for name, binaries in sorted(payload(contract).items()):
        gaps += [name + ' carries ' + binary for binary in binaries
                 if binary not in built]
    gaps += ['host tool ' + tool for tool in contract['host_tools']
             if tool not in built]
    if gaps:
        raise ShipError('no recorded compile target produces '
                        + '; '.join(gaps))


def assert_staged(context, image):
    """Refuse a staged image context that does not carry every binary
    `image`'s contract records, naming each one missing. Read back from
    the context directory and the Dockerfile generated beside it rather
    than from the copy loop that wrote them, so a staging change that
    drops a binary — or an entrypoint the payload names but the image
    would not serve — fails the run here instead of producing an image
    a leg later finds empty."""
    context = Path(context)
    names = [image['entrypoint']] + list(image['ships'])
    staged = {path.name for path in context.iterdir() if path.is_file()}
    dockerfile = (context / 'Dockerfile').read_text()
    missing = [name for name in names if name not in staged
               or ('COPY ' + name + ' ' + INSTALL_PREFIX + name)
               not in dockerfile]
    if missing:
        raise ShipError(image['name'] + ' image stages no '
                        + ', '.join(missing))


def revision_contract(src):
    """The shipped-binary contract the revision under test records at
    `src/qa_lane/ship.json`, or None when that source tree carries none
    — a revision predating the document, or an archive without the lane
    tree. Absence is not a defect: the deployed copy's own contract is
    then the only record there is."""
    if src is None:
        return None
    return _read(Path(src).joinpath(*REVISION_PATH), required=False)


def check_lane_copy(src, contract=None):
    """Refuse the run when the deployed lane copy ships less than the
    revision under test records. The lane code is pinned on the host
    separately from the revision under test, so a revision can carry a
    ship list its testing lane copy never learned; the images that copy
    builds then hold their entrypoints alone and every leg that execs a
    shipped binary reports the tooling absent, at a revision that has
    it. Comparing against the contract inside the tested revision's own
    archive catches that before the build, by name. A revision recording
    no contract, and a deployed copy shipping a superset of one that
    does, are both left alone."""
    recorded = revision_contract(src)
    if recorded is None:
        return
    deployed = payload(contract if contract is not None else load())
    behind = []
    for image in recorded['images']:
        have = deployed.get(image['name'])
        if have is None:
            behind.append('no ' + image['name'] + ' image')
            continue
        want = [image['entrypoint']] + list(image['ships'])
        behind += [image['name'] + ' ships no ' + binary
                   for binary in want if binary not in have]
    if behind:
        raise ShipError('the deployed qa_lane copy predates the '
                        'shipped-binary contract the revision under '
                        'test records: ' + '; '.join(behind))