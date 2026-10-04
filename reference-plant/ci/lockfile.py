#!/usr/bin/env python3
"""The committed Cargo.lock's agreement with the manifest's declared pin.

The committed Cargo.lock is this repository's reproducibility
artifact: README §2's promise is that a tag pin resolves once and the
committed lockfile records the commit it landed on, so a fresh clone
must resolve under `cargo fetch --locked` without a resolver quietly
repairing the file first. Cargo refuses a lockfile whose recorded
query disagrees with the manifest's pin at all, so `ci/check.sh` runs
this leg against the committed lockfile *before* its resolve stage can
rewrite it — the state a consumer's own CI could otherwise never see,
because the resolve stage's re-resolve fallback used to absorb it.

Both documents are read structurally, never by a positional spelling.
The manifest is read through `cargo metadata --no-deps`: Cargo's own
TOML dialect decides which declaration is the pin — inline-table key
order, line wrapping, and comments are inert, and a commented-out pin
declares nothing — while `--no-deps` leaves the committed lockfile
unread and unwritten. The lockfile is read through `tomllib`, the
stdlib TOML parser, so a `[[package]]` entry's field order is the
same record however it was merged or edited.

Usage, from the consumer tree's root:

    python3 ci/lockfile.py [LOCKFILE [REMOTE [RECORD]]]

`LOCKFILE` is the lockfile to read: `Cargo.lock` itself in the positive
leg, a doctored scratch copy in the stage's self-checks. `REMOTE` is
the remote this check resolves — `DCS_REMOTE`, the stand-in the
workspace-side proofs substitute — and `RECORD` the release record's
`record.md`: the one `ci/check.sh` fetches at the pinned rev in the
contract's own shape, the substituted one under `DCS_RECORD_DIR`, or
the empty string for the stage's doctored legs. Exit
status 2 is a release crate recorded from a non-git source —
`path-dependency-leak`'s finding — 3 a tag query the remote could
not answer at all — the served target unverifiable rather than
absent, `pin-unresolvable`'s finding — and 1 every other
disagreement, `lockfile-stale`'s.
"""

import json
import re
import subprocess
import sys

try:
    import tomllib
except ImportError:  # python < 3.11: tomli is the same parser, backported
    import tomli as tomllib

arguments = sys.argv[1:] + ["", "", ""]
lock_path = arguments[0] or "Cargo.lock"
fetch_remote, record_path = arguments[1], arguments[2]
release = ("dcs-build", "dcs-core", "dcs-model")

# `leak` is the non-git-source finding — a recorded `path` into some
# checkout, or a released crate recorded with no source at all — and
# carries its own exit status so the shell reports `path-dependency-leak`
# rather than the stale-pin diagnostic.
def leak(message):
    print(message, file=sys.stderr)
    sys.exit(2)


# The declared pin, as Cargo resolves the manifest: `cargo metadata
# --no-deps` answers each dependency's canonical source —
# `git+<url>?<tag|rev>=<value>` for a git pin — without resolving the
# graph, so the committed lockfile is neither read nor rewritten. A
# dependency carrying no source, or a source that is not the pinned
# remote's git query, declares no pin this leg can hold the lockfile
# to.
try:
    metadata = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps", "--offline"],
        capture_output=True, text=True, check=False,
    )
except OSError as error:
    sys.exit(f"cargo metadata could not run: {error}")
if metadata.returncode != 0:
    sys.exit(f"Cargo.toml does not resolve as a cargo manifest: {metadata.stderr.strip()}")
document = json.loads(metadata.stdout)
git_pin = re.compile(r"^git\+[^?]+\?(tag|rev)=")
declared = {}
for name in ("dcs-build", "dcs-model"):
    pins = {
        dependency["source"]
        for package in document.get("packages", [])
        for dependency in package.get("dependencies", [])
        if dependency.get("name") == name
        and isinstance(dependency.get("source"), str)
        and git_pin.match(dependency["source"])
    }
    if not pins:
        sys.exit(f"{name} declares no `git = ..., tag|rev = ...` pin in Cargo.toml")
    if len(pins) != 1:
        sys.exit(f"{name} declares conflicting git pins in Cargo.toml: {sorted(pins)}")
    declared[name] = pins.pop()
if len(set(declared.values())) != 1:
    sys.exit(f"the release crates declare different pins: {sorted(declared.values())}")
declared_source = declared["dcs-build"]
url, _, query = declared_source[len("git+"):].partition("?")
kind, _, value = query.partition("=")
if url != fetch_remote:
    sys.exit(f"Cargo.toml pins {url} while this check resolves {fetch_remote} — "
             "repin the manifest, or drop the DCS_REMOTE substitution")

try:
    with open(lock_path, "rb") as handle:
        lock = tomllib.load(handle)
except (OSError, tomllib.TOMLDecodeError) as error:
    sys.exit(f"{lock_path} does not parse as a TOML lockfile: {error}")

# Every package entry the lockfile records, as (name, source) pairs.
# The entry — not the source-bearing field — is the unit: Cargo writes
# a path package with no `source` key at all, so a released crate
# reaching a checkout through one leaves no field for a per-name map
# built out of sourced entries to collect, and a second same-name
# record at another pin disappears into a last-wins one. Every record
# is kept, so neither hides behind another.
packages = []
for package in lock.get("package", []):
    if not isinstance(package, dict) or not isinstance(package.get("name"), str):
        sys.exit(f"{lock_path} records a package block with no name")
    source = package.get("source")
    packages.append((package["name"], source if isinstance(source, str) else None))

# Every release crate's recorded source — git only, one source, on the
# manifest's own remote and query, at one precise revision. A `dcs-*`
# entry carrying no `source` field at all resolves from a path into
# some checkout, `path-dependency-leak`'s finding, as does a recorded
# non-git source; a crate with no package entry is absent entirely,
# `lockfile-stale`'s: nothing is recorded, let alone a path source.
for name, source in packages:
    if not name.startswith("dcs-"):
        continue
    if source is None:
        leak(f"{name} is recorded with no source in {lock_path} — a path into some checkout")
    if not source.startswith("git+"):
        leak(f"{name} resolved from {source} — only a git source satisfies a git pin")
missing = [name for name in release if name not in {seen for seen, _ in packages}]
if missing:
    sys.exit(f"release crates missing from {lock_path}: {sorted(missing)}")
# Every record of every release crate must agree on the one source: a
# second same-name record at another remote or pin is the stale-pin
# finding, not a record the first entry may stand in for.
sources = {source for name, source in packages if name in release}
if len(sources) != 1:
    sys.exit(f"the release crates record different sources: {sorted(sources)}")
source = sources.pop()
prefix = f"{declared_source}#"
if not source.startswith(prefix):
    sys.exit(f"{lock_path} records {source} for the release crates, but Cargo.toml "
             f"declares {url} at {query}")
precise = source[len(prefix):]
if not re.fullmatch(r"[0-9a-f]{40}", precise):
    sys.exit(f"{lock_path} records no precise revision for {url} at {query}: {source}")

# The recorded revision must be the one the declared pin names. A tag
# the remote answers without serving — the ref genuinely absent — is
# not this leg's finding: an unresolvable pin is `pin-unresolvable`'s.
# A query the remote cannot answer is a different shape: a transport
# failure, an auth refusal, a safe.directory rejection, or a corrupt
# repository all exit nonzero, which stdout alone cannot tell from
# "not published" — yet the tag's target is then unverifiable, never
# absent. The leg refuses on that path (status 3) because nothing
# downstream rescues it: `cargo fetch --locked` resolves the recorded
# sha through libgit2 and cargo's git cache without re-checking the
# tag's target, so a lockfile recording a commit the tag does not
# land on would otherwise pass green on the ambient CLI's health.
if kind == "tag":
    try:
        refs = subprocess.run(
            ["git", "ls-remote", url,
             f"refs/tags/{value}", f"refs/tags/{value}^{{}}"],
            capture_output=True, text=True, check=False,
        )
    except OSError as error:
        print(f"git ls-remote could not run: {error}", file=sys.stderr)
        sys.exit(3)
    if refs.returncode != 0:
        print(f"{url} could not be queried for {value}: git ls-remote "
              f"exited {refs.returncode}: {refs.stderr.strip()}",
              file=sys.stderr)
        sys.exit(3)
    served = {}
    for line in refs.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        served[ref] = sha
    target = served.get(f"refs/tags/{value}^{{}}") or served.get(f"refs/tags/{value}")
    if target is None:
        print(f"  {value} is not published on {url} yet — an unresolvable pin is "
              "pin-unresolvable's finding")
    elif target != precise:
        sys.exit(f"{lock_path} records {precise}, but {value} lands on {target}")
elif re.fullmatch(r"[0-9a-f]{40}", value) and precise != value:
    sys.exit(f"{lock_path} records {precise} for rev {value}")

# The release record's Commit field names the same release when it is
# filled: the tag's target and the record must not diverge, or the
# shipped artifact pins a commit the record does not claim.
if record_path:
    commit = re.search(r"^\| Commit \| `([0-9a-f]{40})`", open(record_path).read(), re.M)
    if commit and commit.group(1) != precise:
        sys.exit(f"{lock_path} records {precise}, but {record_path} records "
                 f"commit {commit.group(1)}")

print(f"  the committed {lock_path} records {query} at {precise}")
