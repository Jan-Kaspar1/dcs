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
import subprocess

from .merge_flow import MECHANICAL_RESOLUTION_KIND
from .runtime import Runtime


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
# re-resolves it rather than merging it as text. A path earns a resolver
# only when its regeneration is verifiable by the workspace's locked CI
# build; every other conflicted path stays repair-bound.
MECHANICAL_RESOLVERS = {
    'Cargo.lock': Resolver('cargo-lockfile', _regenerate_cargo_lockfile),
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
