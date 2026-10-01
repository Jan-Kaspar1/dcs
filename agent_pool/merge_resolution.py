"""Mechanical resolution for publish-merge conflicts on recorded paths.

When ``git merge --no-edit origin/main`` fails during publish integration,
the supervisor consults the resolver table below before spending a repair
invocation: if every conflicted path git reported is registered, each
path's resolver regenerates its artifact in-process, the resolved paths
are staged, and the merge commit completes — no repair dispatch. A
conflict touching any unregistered path, or a resolver that fails, falls
through to the bounded merge-conflict repair unchanged.
"""
import os
from pathlib import Path
import re
import subprocess

from .merge_flow import MECHANICAL_RESOLUTION_KIND
from .runtime import Runtime


RELEASE_CONTRACT_PATH = 'docs/release-contract.md'

# A diagnostics-table row whose leading cell is exactly one backticked
# name — the `<leg>-failed` / `<leg>-nondeterministic` rows each QA leg
# appends to the contract's tail table. Unkeyed lines (separator rows,
# prose) and rows whose first cell mixes a name with further text (the
# artifact table's "`name` crate" cells) do not match, so a conflict
# touching them is never a pure keyed-row append.
_ROW_KEY = re.compile(r'^\|\s*`(?P<key>[^`|]+)`\s*\|.*\|\s*$')


def _keyed_row(line):
    """The row's leading name key, or None when the line is not a keyed row."""
    match = _ROW_KEY.match(line)
    return match.group('key').strip() if match else None


def _conflict_segments(lines):
    """Split conflicted file lines into ('text', lines) and ('conflict', ours, theirs).

    The ``|||||||`` base section of a diff3-style hunk is skipped — only
    the two sides' content decides whether the hunk is a pure append.
    Raises ValueError on unterminated markers so a shape this resolver
    does not understand stays repair-bound; nested or stray inner markers
    land inside a side's lines and fail the row check the same way.
    """
    segments = []
    text = []
    index = 0
    while index < len(lines):
        if not lines[index].startswith('<<<<<<<'):
            text.append(lines[index])
            index += 1
            continue
        index += 1
        ours = []
        while index < len(lines) \
                and not lines[index].startswith(('=======', '|||||||')):
            ours.append(lines[index])
            index += 1
        if index < len(lines) and lines[index].startswith('|||||||'):
            index += 1
            while index < len(lines) and not lines[index].startswith('======='):
                index += 1
        if index >= len(lines) or not lines[index].startswith('======='):
            raise ValueError('unterminated conflict hunk')
        index += 1
        theirs = []
        while index < len(lines) and not lines[index].startswith('>>>>>>>'):
            theirs.append(lines[index])
            index += 1
        if index >= len(lines):
            raise ValueError('unterminated conflict hunk')
        index += 1
        segments.append(('text', text))
        segments.append(('conflict', ours, theirs))
        text = []
    if text or not segments:
        segments.append(('text', text))
    return segments


def _resolve_release_contract(clone):
    """Union both sides' appended diagnostic rows under one deterministic order.

    Applies only when every conflicted hunk is a pure keyed-row append:
    each side holds only ``| `name` | ... |`` rows, and no hunk row names
    a key the merge-base revision or the surrounding merged text already
    carries — an existing row inside a conflict means a modification or
    deletion wrapped it, not an append. The resolution replaces each hunk
    with the union of both sides' rows, identical rows deduplicated,
    ordered by the leading name cell; the surrounding table and document
    text outside the markers is git's own merge, kept verbatim. A hunk
    holding a non-row line, an empty side, an existing row, or the same
    key under divergent text raises — ``resolve_mechanical`` then falls
    through to the bounded merge-conflict repair with the merge in
    progress exactly as the failed merge left it.
    """
    clone = Path(clone)
    path = clone / RELEASE_CONTRACT_PATH
    text = path.read_text()
    segments = _conflict_segments(text.splitlines())
    if not any(kind == 'conflict' for kind, *_ in segments):
        raise RuntimeError(RELEASE_CONTRACT_PATH + ' carries no conflict hunk')
    base = Runtime.run_git(clone, 'show', ':1:' + RELEASE_CONTRACT_PATH)
    base_keys = {key for line in base.splitlines()
                 for key in (_keyed_row(line),) if key is not None}
    outside_keys = {key for kind, *payload in segments if kind == 'text'
                    for line in payload[0]
                    for key in (_keyed_row(line),) if key is not None}
    resolved = []
    emitted = {}
    for kind, *payload in segments:
        if kind == 'text':
            resolved.extend(payload[0])
            continue
        ours, theirs = payload
        if not ours or not theirs:
            raise RuntimeError('conflict hunk is not a two-sided row append')
        union = {}
        for line in ours + theirs:
            key = _keyed_row(line)
            if key is None:
                raise RuntimeError('conflict hunk holds a non-keyed line')
            if key in base_keys or key in outside_keys:
                raise RuntimeError('conflict hunk holds existing row ' + key)
            if key in union and union[key] != line:
                raise RuntimeError('conflict rows diverge for ' + key)
            union.setdefault(key, line)
        for key in sorted(union):
            if key in emitted:
                if emitted[key] != union[key]:
                    raise RuntimeError('conflict rows diverge for ' + key)
                continue
            emitted[key] = union[key]
            resolved.append(union[key])
    path.write_text('\n'.join(resolved) + ('\n' if text.endswith('\n') else ''))


def _regenerate_cargo_lockfile(clone):
    """Re-resolve the workspace lockfile from the merged manifests.

    Either side's lockfile bytes are a generated artifact, not intent: the
    incoming main side only clears the conflict markers, and
    ``cargo generate-lockfile`` then re-resolves the workspace so the
    committed result is regenerated — never spliced. The workspace's
    locked CI build (``cargo clippy --locked``) verifies the result. The
    regeneration runs under the same shared build limits verify.py holds:
    a serialized per-user build slot and bounded cargo parallelism. Kept
    lazy so this module stays importable where scripts/ is not on the
    path; only the resolution path needs it.
    """
    from scripts.verify import build_slot

    clone = Path(clone)
    Runtime.run_git(clone, 'checkout', '--theirs', '--', 'Cargo.lock')
    env = dict(os.environ, CARGO_BUILD_JOBS='4',
               CARGO_TARGET_DIR=str(clone / 'target'))
    with build_slot():
        subprocess.run(['cargo', 'generate-lockfile'], cwd=str(clone), env=env,
                       check=True, capture_output=True, text=True, timeout=600)


class Resolver:
    """A named mechanical resolver for one recorded conflicted path."""

    def __init__(self, name, resolve):
        self.name = name
        self.resolve = resolve


# The table is seeded from recorded work-ledger evidence, not guessed:
# 'repair' rows with cause 'merge-conflict' carry the conflicted paths git
# reported (the per-path attribution recorded in work_events since #1097),
# and the rolling merge comparison named in #1122 measured merge-conflict
# as the top repair cause (22 repairs; merges 183 vs 294, -37.8%
# window-over-window) with Cargo.lock the recurring regenerable path —
# its content is a pure function of the merged manifests, so cargo
# re-resolves it rather than merging it as text. #1246 added the second
# entry on the same evidence chain: first-parent churn shows
# docs/release-contract.md touched by 57 of the 224 current-window merges
# because every QA leg appends named diagnostic rows to the same tail
# tables, so two in-flight legs conflict on that tail on every publish
# merge — a pure append whose union is deterministic. A path earns a
# resolver only when its resolution is verifiable mechanically; every
# other conflicted path stays repair-bound.
MECHANICAL_RESOLVERS = {
    'Cargo.lock': Resolver('cargo-lockfile', _regenerate_cargo_lockfile),
    RELEASE_CONTRACT_PATH: Resolver('release-contract-union',
                                    _resolve_release_contract),
}


def resolvers_for(paths):
    """The {path: Resolver} map when every conflicted path is registered.

    Returns None when any reported path has no recorded resolver — the
    all-or-nothing rule that keeps non-registered conflicts on the bounded
    repair path.
    """
    resolvers = {}
    for path in paths or ():
        resolver = MECHANICAL_RESOLVERS.get(path)
        if resolver is None:
            return None
        resolvers[path] = resolver
    return resolvers or None
