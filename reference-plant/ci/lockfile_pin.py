#!/usr/bin/env python3
"""The consumer lockfile leg: the committed `Cargo.lock` must record
the pin `Cargo.toml` declares, at the revision that pin names.

The committed lockfile is this repository's reproducibility artifact:
a tag pin resolves once and the lockfile records the commit it landed
on, so a fresh clone must resolve under `cargo fetch --locked` without
a resolver quietly repairing the file first. Cargo refuses a lockfile
whose recorded query disagrees with the manifest's pin at all, so this
leg compares the lockfile against the declared pin *before* any fetch
can rewrite it — the state a consumer's own CI could otherwise never
see, because `ci/check.sh`'s `resolve` stage's re-resolve fallback
used to absorb it.

What the leg requires of the lockfile:

- every `dcs-*` package block is recorded with a git source — a block
  carrying no `source` line at all resolves from a `path` into some
  checkout and records no source whatsoever, and a non-git source is
  equally unsatisfiable by a git pin: both are
  `path-dependency-leak`;
- every release crate — `dcs-build`, `dcs-core`, `dcs-model` — has a
  package block at all, and every one of those blocks agrees on one
  source: this repository's remote, the manifest's own `tag`/`rev`
  fragment, and one precise 40-hex revision;
- that precise revision is the one the declared pin names *now*:

  - a `tag` pin names a tag, read back off the remote through
    `git ls-remote` (peeled first, so an annotated tag lands on its
    commit);
  - a full-sha `rev` names one immutable commit, compared literally;
  - a short-sha `rev` — a prefix Cargo resolves against the fetched
    history — is held to be a prefix of the recorded revision, which is
    the only check possible without cloning the remote's history;
  - every other `rev` spelling — a branch name, a tag name, any other
    ref Cargo resolves dynamically — names whatever the remote serves
    under that name at check time, so it is read back off the remote
    exactly like the `tag` path and compared the same way. The
    reported defect (`lockfile-leg-rev-compare-skipped-for-non-full-
    sha`): the full-sha gate skipped the comparison for every other
    spelling, so a lockfile recording a commit the branch had moved
    past passed the stage — the exact staleness class the stage exists
    to report, silently accepted;
  - the release record's filled `Commit` field, when one was supplied,
    names the same revision.

A pin the remote does not serve at all — a tag or branch name that
does not exist yet — is not this leg's finding: an unresolvable pin is
`pin-unresolvable`'s, and a remote that cannot be reached at all
leaves the query comparison above holding. The note says so out loud
rather than passing silently.

Usage:

    lockfile_pin.py [--manifest Cargo.toml] [--lock Cargo.lock] \
        --remote <url> [--record <release-record.md>]

On success one summary line prints and the exit status is 0. Exit
status 2 reports `path-dependency-leak` — a release crate recorded
from a non-git source, or from no source at all. Exit status 1 reports
every other disagreement between the lockfile and the declared pin,
which is `lockfile-stale`'s. `ci/check.sh`'s `lockfile` stage names
the status; this leg never names a diagnostic itself.
"""

import argparse
import re
import subprocess
import sys

RELEASE = ("dcs-build", "dcs-core", "dcs-model")
PINNED = ("dcs-build", "dcs-model")

# A full commit sha, and the abbreviated prefix Cargo resolves against
# the fetched history (git requires at least four hex digits).
FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
SHORT_SHA = re.compile(r"[0-9a-fA-F]{4,39}")


def eprint(*args):
    print(*args, file=sys.stderr)


class Refusal(Exception):
    """A lockfile that does not record the declared pin. `status` is 2
    for `path-dependency-leak`, 1 for `lockfile-stale`."""

    def __init__(self, message, status=1):
        super().__init__(message)
        self.status = status


def die(message, status=1):
    raise Refusal(message, status)


def declared_pin(manifest):
    """The one pin both release crates declare, as `(url, kind, value)`
    — the manifest's own remote and the `tag`/`rev` fragment."""
    declared = {}
    for name in PINNED:
        match = re.search(
            re.escape(name) + r' = \{ git = "([^"]+)",\s*(tag|rev) = "([^"]+)"', manifest
        )
        if match is None:
            sys.exit(f"{name} declares no `git = ..., tag|rev = ...` pin in Cargo.toml")
        declared[name] = (match.group(1), match.group(2), match.group(3))
    if len(set(declared.values())) != 1:
        sys.exit(f"the release crates declare different pins: {sorted(declared.values())}")
    return declared["dcs-build"]


def recorded_packages(lock, lock_path):
    """Every package block the lockfile records, as `(name, source)`
    pairs — the block, not the source-bearing line, is the unit: Cargo
    writes a path package with no `source` key at all, so a released
    crate reaching a checkout through one leaves no line for a per-name
    dict built out of source-bearing lines to collect, and a second
    same-name record at another pin disappears into a last-wins one.
    Every record is kept, so neither hides behind another."""
    packages = []
    for block in lock.split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.M)
        if name is None:
            sys.exit(f"{lock_path} records a package block with no name")
        source = re.search(r'^source = "([^"]+)"', block, re.M)
        packages.append((name.group(1), None if source is None else source.group(1)))
    return packages


def remote_refs(url):
    """Every ref the remote serves, as `{ref: sha}`. An unreachable
    remote and a remote serving no refs are the same empty answer: the
    leg's query comparison above it already holds, and the caller
    reports an unresolvable pin rather than a stale lockfile."""
    listing = subprocess.run(
        ["git", "ls-remote", url],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    served = {}
    for line in listing.splitlines():
        sha, _, ref = line.partition("\t")
        if ref:
            served[ref] = sha
    return served


def served_name(served, value):
    """The refs the remote serves under `value` — the shapes git's own
    `rev-parse` resolves a bare name through (`refs/heads/<value>`,
    `refs/remotes/<value>`, `refs/tags/<value>`, the bare `HEAD`), and
    failing those any other namespace's entry of that name — as
    `{ref: sha}`, with an annotated tag's peeled `^{}` entry preferred
    over the tag object it peels to."""
    named = {
        ref: served[ref]
        for ref in (f"refs/heads/{value}", f"refs/remotes/{value}", value)
        if ref in served
    }
    tag = f"refs/tags/{value}"
    peeled = served.get(f"{tag}^{{}}") or served.get(tag)
    if peeled is not None:
        named[tag] = peeled
    if not named:
        named = {
            ref: sha
            for ref, sha in served.items()
            if ref == value or ref.endswith(f"/{value}")
        }
    return named


def check_precise(lock_path, url, precise, kind, value, served):
    """Hold the revision the lockfile records against the revision the
    declared pin names. `served` is the remote's ref listing; the tag
    and the ref-name `rev` spellings read the pin's current target off
    it, while a full sha and a short sha name their revision
    themselves."""
    if kind == "tag":
        target = served.get(f"refs/tags/{value}^{{}}") or served.get(f"refs/tags/{value}")
        if target is None:
            print(f"  {value} is not published on {url} yet — an unresolvable pin is "
                  "pin-unresolvable's finding")
        elif target != precise:
            die(f"{lock_path} records {precise}, but {value} lands on {target}")
        return
    if FULL_SHA.fullmatch(value):
        if precise.lower() != value.lower():
            die(f"{lock_path} records {precise} for rev {value}")
        return
    # A `rev` spelling Cargo resolves dynamically: a ref name first —
    # git's own resolution prefers a served ref over an abbreviated
    # object name — and the abbreviation only when the remote serves
    # nothing under that name.
    named = served_name(served, value)
    if named:
        moved = sorted(f"{ref} at {sha}" for ref, sha in named.items() if sha != precise)
        if moved:
            die(f"{lock_path} records {precise}, but the declared rev {value} names "
                + ", ".join(moved))
        return
    if SHORT_SHA.fullmatch(value):
        if not precise.lower().startswith(value.lower()):
            abbreviates, seen = [], set()
            for ref, sha in sorted(served.items()):
                if not sha.lower().startswith(value.lower()) or ref.endswith("^{}"):
                    continue
                if sha not in seen:
                    seen.add(sha)
                    abbreviates.append(f"{ref} at {sha}")
            die(f"{lock_path} records {precise}, which the declared short rev {value} "
                "does not abbreviate"
                + (f" — {', '.join(abbreviates)} does" if abbreviates else ""))
        return
    print(f"  the declared rev {value} names no revision {url} serves — an "
          "unresolvable pin is pin-unresolvable's finding")


def read(path):
    with open(path) as handle:
        return handle.read()


def check(manifest_path, lock_path, remote, record_path):
    """The leg over one tree's declared pin and one lockfile, returning
    the summary line a passing run prints."""
    manifest = read(manifest_path)
    lock = read(lock_path)
    url, kind, value = declared_pin(manifest)
    if url != remote:
        sys.exit(f"Cargo.toml pins {url} while this check resolves {remote} — "
                 "repin the manifest, or drop the DCS_REMOTE substitution")
    query = f"{kind}={value}"

    packages = recorded_packages(lock, lock_path)
    for name, source in packages:
        if not name.startswith("dcs-"):
            continue
        if source is None:
            die(f"{name} is recorded with no source in {lock_path} — a path into some "
                "checkout", 2)
        if not source.startswith("git+"):
            die(f"{name} resolved from {source} — only a git source satisfies a git "
                "pin", 2)
    missing = [name for name in RELEASE if name not in {seen for seen, _ in packages}]
    if missing:
        die(f"release crates missing from {lock_path}: {sorted(missing)}")
    sources = {source for name, source in packages if name in RELEASE}
    if len(sources) != 1:
        die(f"the release crates record different sources: {sorted(sources)}")
    source = sources.pop()
    prefix = f"git+{url}?{query}#"
    if not source.startswith(prefix):
        die(f"{lock_path} records {source} for the release crates, but "
            f"{manifest_path} declares {url} at {query}")
    precise = source[len(prefix):]
    if not re.fullmatch(r"[0-9a-f]{40}", precise):
        die(f"{lock_path} records no precise revision for {url} at {query}: {source}")

    check_precise(lock_path, url, precise, kind, value, remote_refs(url))

    # The release record's Commit field names the same release when it
    # is filled: the pin's target and the record must not diverge, or
    # the shipped artifact pins a commit the record does not claim.
    if record_path:
        commit = re.search(r"^\| Commit \| `([0-9a-f]{40})`", read(record_path), re.M)
        if commit and commit.group(1) != precise:
            die(f"{lock_path} records {precise}, but {record_path} records "
                f"commit {commit.group(1)}")

    return f"  the committed {lock_path} records {query} at {precise}"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Hold a committed Cargo.lock against the pin Cargo.toml declares."
    )
    parser.add_argument("--manifest", default="Cargo.toml",
                        help="the manifest declaring the release-crate pin")
    parser.add_argument("--lock", default="Cargo.lock",
                        help="the lockfile to read")
    parser.add_argument("--remote", required=True,
                        help="the git remote the release crates resolve from")
    parser.add_argument("--record", default="",
                        help="the release record's record.md, when one was substituted")
    options = parser.parse_args(argv)
    try:
        print(check(options.manifest, options.lock, options.remote, options.record))
    except Refusal as refusal:
        eprint(refusal)
        return refusal.status
    return 0


if __name__ == "__main__":
    sys.exit(main())
