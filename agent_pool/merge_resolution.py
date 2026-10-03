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
QA_PLAN_PATH = 'docs/lenovo-hardware-qa-plan.md'

# A diagnostics-table row whose leading cell is exactly one backticked
# name — the `<leg>-failed` / `<leg>-nondeterministic` rows each QA leg
# appends to the contract's tail table. Unkeyed lines (separator rows,
# prose) and rows whose first cell mixes a name with further text (the
# artifact table's "`name` crate" cells) do not match, so a conflict
# touching them is never a pure keyed-row append.
_ROW_KEY = re.compile(r'^\|\s*`(?P<key>[^`|]+)`\s*\|.*\|\s*$')

# Any ATX heading line, and the subset the QA plan's ledger appends:
# `## Landed 2026-09-15 (HQ-1, …)` opens the ledger and every later entry
# is a `### Landed <date> (<title>, #NNNN)` block. The heading line is the
# entry's key, and the lines under it are its body, so the whole block is
# one appendable unit — the block-shaped twin of the contract's keyed row.
_HEADING = re.compile(r'^#{1,6}\s')
_LANDED_HEADING = re.compile(r'^#{2,6}\s+Landed\s+\S')


def _keyed_row(line):
    """The row's leading name key, or None when the line is not a keyed row."""
    match = _ROW_KEY.match(line)
    return match.group('key').strip() if match else None


def _row_entries(lines):
    """Every appended line as a one-line keyed entry; raise on any other.

    Strict on purpose: an append carrying prose, a separator, or a table
    cell mixing a name with further text is not an append of keyed rows, so
    the resolution falls through to the bounded repair instead of guessing.
    """
    entries = []
    for line in lines:
        key = _keyed_row(line)
        if key is None:
            raise RuntimeError('appended residue holds a non-keyed line')
        entries.append((key, [line]))
    return entries


def _row_keys(lines):
    """The keyed-row names any text carries, ignoring every other line."""
    return {key for line in lines
            for key in (_keyed_row(line),) if key is not None}


def _landed_entries(lines):
    """An appended residue split into whole ``Landed`` blocks; raise otherwise.

    Each entry opens with a ``Landed`` heading — its key — and runs to the
    next heading or the end of the residue, so an entry is a complete block
    rather than a line. A residue opening with body text, carrying a heading
    that is not a ``Landed`` ledger entry, or splitting one block's body
    across a non-``Landed`` heading raises: the append is not the recorded
    block append, so the merge stays repair-bound.
    """
    entries = []
    for line in lines:
        if _HEADING.match(line):
            if not _LANDED_HEADING.match(line):
                raise RuntimeError('appended residue holds a non-Landed heading')
            entries.append((line.strip(), [line]))
            continue
        if not entries:
            raise RuntimeError('appended residue opens with no Landed heading')
        entries[-1][1].append(line)
    return entries


def _landed_keys(lines):
    """The ``Landed`` block keys any text carries, ignoring every other line.

    Tolerant by design: the base revision carries the plan's other headings
    and prose, none of which is an entry key. A ``#``-prefixed line inside a
    fenced snippet is read as a heading here, which can only add a key no real
    append carries — the union then refuses, never resolves wrongly.
    """
    keys = set()
    for line in lines:
        if _LANDED_HEADING.match(line):
            keys.add(line.strip())
    return keys


class KeyedRegion:
    """One recorded append-only region: how its entries key, and how they parse.

    ``label`` names the unit in the resolver's refusal messages (``row`` for
    the contract's table rows, ``section`` for the QA plan's Landed blocks),
    ``entries`` strictly parses one side's appended residue into
    ``(key, lines)`` units, and ``keys`` collects the keys a document already
    carries.
    """

    def __init__(self, label, entries, keys):
        self.label = label
        self.entries = entries
        self.keys = keys


RELEASE_CONTRACT_REGION = KeyedRegion('row', _row_entries, _row_keys)
QA_PLAN_REGION = KeyedRegion('section', _landed_entries, _landed_keys)


def _carries_conflict_markers(lines):
    """Whether the worktree copy of a conflicted path still carries markers.

    A cheap guard that git really conflicted on this path, so a resolver
    never rewrites a file the merge had already resolved.
    """
    return any(line.startswith('<<<<<<<') for line in lines)


def _pure_insertion(base, side):
    """The ``(index, inserted lines)`` one contiguous insertion of ``side``.

    ``side`` must be ``base`` with lines inserted at one index and nothing
    else changed: the common prefix and the common suffix are peeled off and
    the residue must be a nonempty insertion against an empty base residue.
    Returns None for anything else — a modification or deletion, a
    replacement, two edits, or no change at all — which is what keeps every
    non-append conflict on the bounded repair path.
    """
    prefix = 0
    limit = min(len(base), len(side))
    while prefix < limit and base[prefix] == side[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < len(base) - prefix and suffix < len(side) - prefix
           and base[len(base) - 1 - suffix] == side[len(side) - 1 - suffix]):
        suffix += 1
    if prefix + suffix != len(base):
        return None
    inserted = side[prefix:len(side) - suffix]
    return (prefix, inserted) if inserted else None


def _resolve_keyed_append(clone, path, region):
    """Union both sides' appended keyed entries under one deterministic order.

    Applies only when the merge stages prove a pure append on both sides:
    each side's revision is the merge-base revision plus one contiguous
    insertion of whole region entries (``| `name` | ... |`` rows for the
    contract, complete ``Landed`` blocks for the QA plan) at the same anchor,
    and no inserted entry names a key the base revision already carries. The
    resolution writes the base document with both sides' entries inserted at
    that anchor, identical entries deduplicated, ordered by key — so the
    result is byte-identical whichever side git labels ours, and independent
    of how git chose to present the conflict in the worktree (git trims a
    conflict hunk's common prefix and suffix out of the markers, which can
    move an entry's own continuation lines outside them; the stages are the
    unreduced record of what each side appended).

    A side that is not a pure append, insertions at different anchors, a
    nonempty residue the region's parser does not recognize, an existing
    key, or the same key under divergent text raises — ``resolve_mechanical``
    then falls through to the bounded merge-conflict repair with the merge in
    progress exactly as the failed merge left it.
    """
    clone = Path(clone)
    file = clone / path
    text = file.read_text()
    if not _carries_conflict_markers(text.splitlines()):
        raise RuntimeError(path + ' carries no conflict hunk')
    base = Runtime.run_git(clone, 'show', ':1:' + path).splitlines()
    sides = [Runtime.run_git(clone, 'show', ':%d:' % stage + path).splitlines()
             for stage in (2, 3)]
    base_keys = region.keys(base)
    insertions = [_pure_insertion(base, side) for side in sides]
    if any(insertion is None for insertion in insertions):
        raise RuntimeError('conflict side is not a pure append of ' + path)
    if len({insertion[0] for insertion in insertions}) > 1:
        raise RuntimeError('conflict sides appended at different anchors')
    union = {}
    for _, inserted in insertions:
        for key, entry in region.entries(inserted):
            if key in base_keys:
                raise RuntimeError('conflict append holds existing %s %s'
                                   % (region.label, key))
            if key in union and union[key] != entry:
                raise RuntimeError('conflict entries diverge for ' + key)
            union.setdefault(key, entry)
    anchor = insertions[0][0]
    resolved = base[:anchor]
    for key in sorted(union):
        resolved.extend(union[key])
    resolved.extend(base[anchor:])
    file.write_text('\n'.join(resolved) + ('\n' if text.endswith('\n') else ''))


def _resolve_release_contract(clone):
    """Union both sides' appended keyed diagnostic rows in one order.

    The contract's diagnostics tail table, keyed by each row's leading
    backticked name: ``_resolve_keyed_append``'s row rule applied to the
    recorded append shape.
    """
    _resolve_keyed_append(clone, RELEASE_CONTRACT_PATH, RELEASE_CONTRACT_REGION)


def _resolve_qa_plan(clone):
    """Union both sides' appended ``Landed`` ledger blocks in one order.

    The QA plan's implementation ledger, keyed by each block's
    ``## Landed <date> (<title>)`` heading: ``_resolve_keyed_append``'s
    section rule applied to the block append every QA leg lands at the
    tail of that ledger.
    """
    _resolve_keyed_append(clone, QA_PLAN_PATH, QA_PLAN_REGION)


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
#
# #1420 added the QA-plan ledger entry from the same evidence chain, and
# recorded why the contract resolver still lands nothing. The window it
# read (68 merge-conflict repairs against 208 merges up / 152 completed,
# mechanical_resolutions at 0, the conflict load dominated by three
# append-mostly docs plus 41-44 detail-less rows) carries the recorded
# suspect — a supervisor predating #1246, which the repin #1253 covers — so
# the install's own record settles it: the live release
# (releases/ce43f01d, repointed 2026-10-01T18:18, the process running
# since 2026-10-02T06:12) contains #1246, and supervisor.log holds one
# "mechanical merge resolution failed" line for each of the window's ten
# docs/release-contract.md repairs. Every refusal names
# "conflict hunk holds existing row <leg>-unchecked" (once
# `pair-legs-invalid`, once `rig-invalid`). The cause is the contract's own
# convention: a leg-adding issue's diff is two hunks a few lines apart — its
# two new keyed rows appended before the table's trailing rig rows, and the
# `<leg>-unchecked` convention row edited in place to add the new leg's name
# to that row's emitted-set cell — so both sides edit that row and the
# conflict is not a pure append. The union was right to refuse: resolving it
# would mean merging two edits of one enumeration cell, which is an intent
# question rather than a mechanical union. The entry stays as it is, and the
# stage-based check below now refuses such a side earlier, as not a pure
# append at all.
#
# docs/lenovo-hardware-qa-plan.md qualifies: every landed leg appends one
# whole `### Landed <date> (<title>, #NNNN)` block at the tail of the same
# implementation ledger, so concurrent legs collide at one anchor.
# Reconstructing two such real appends (the #1345 and #1321 blocks off their
# shared base) reproduces the window's conflict: one hunk whose sides are two
# complete, distinct blocks, with the previous block's closing lines and the
# `## Outcome` heading untouched around it. Across the file's whole recorded
# history 42 heading lines were added and none was ever removed or edited,
# so the appended-block shape holds rather than merely holding this once; a
# side that also edits the ledger, or appends at another anchor, still falls
# through to the repair.
MECHANICAL_RESOLVERS = {
    'Cargo.lock': Resolver('cargo-lockfile', _regenerate_cargo_lockfile),
    RELEASE_CONTRACT_PATH: Resolver('release-contract-union',
                                    _resolve_release_contract),
    QA_PLAN_PATH: Resolver('qa-plan-landed-union', _resolve_qa_plan),
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
