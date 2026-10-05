"""`ci/lockfile.py` — the reference plant's committed-lockfile leg —
run against a seeded local remote: the leg holds the committed
`Cargo.lock` against the pin `Cargo.toml` declares, at the revision that
pin names, and Cargo resolves *any* git rev in `rev =` — so every
spelling is covered here. The remote is seeded with two commits, `main`
moved past the first and a tag `movable-tag` on the second, and a
branch name, an abbreviated sha, a tag name spelled through `rev`, and
the `tag =` spelling itself are each read back off the remote and
compared, while a full sha is compared literally. The reported defect
(`lockfile-leg-rev-compare-skipped-for-non-full-sha`) gated that
comparison on a 40-hex `rev`, so every other spelling was admitted and
then checked not at all: a lockfile recording a commit the branch had
moved past passed the leg that exists to report exactly that. A pin the
remote serves nothing for stays `pin-unresolvable`'s finding and says so
out loud rather than passing silently, and a remote that cannot be
queried at all is refused as unverifiable. The leak and stale halves are
covered with it: a release crate recorded with no `source` at all — a
`path` package — carries its own exit status, a missing release crate
and a divergent query are stale, and the release record's filled
`Commit` field must name the recorded revision.

The leg is a script beside the other `ci/check.sh` stage legs — it reads
its tree's manifest through `cargo metadata`, so it resolves in the
working directory — which is why each case here runs it as a subprocess
rooted in a scratch tree of its own. `ci/check.sh`'s `lockfile` stage
runs this same script, which the last case holds to.
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

_CI = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
_LEG = _CI / "lockfile.py"
_CHECK = _CI / "check.sh"


class Remote:
    """A local stand-in for the published origin: a bare repository the
    tests seed with two commits, `main` moved past the first and a tag
    `movable-tag` on the second, so every movable spelling names the
    revision the tests compare against. No network is involved."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dcs-lockfile-"))
        self.url = f"file://{self.dir / 'remote.git'}"
        self.git("init", "--quiet", "--bare", str(self.dir / "remote.git"))
        self.git("init", "--quiet", "-b", "main", str(self.dir / "seed"))
        seed = ["-C", str(self.dir / "seed")]
        self.git(*seed, "config", "user.email", "ci@example.invalid")
        self.git(*seed, "config", "user.name", "reference plant ci")
        self.git(*seed, "remote", "add", "origin", self.url)
        self.git(*seed, "commit", "--quiet", "--allow-empty", "-m", "base")
        self.git(*seed, "push", "--quiet", "origin", "main")
        self.base = self.rev("HEAD")
        self.git(*seed, "commit", "--quiet", "--allow-empty", "-m", "moved")
        self.git(*seed, "push", "--quiet", "origin", "main")
        self.tip = self.rev("HEAD")
        self.git(*seed, "tag", "movable-tag")
        self.git(*seed, "push", "--quiet", "origin", "movable-tag")
        # The bare repository's HEAD points at the seeded branch, so
        # `git ls-remote` reports it — a bare repository left on the
        # ambient default branch serves a dangling HEAD, which the
        # `rev = "HEAD"` case below would then read as no ref at all.
        self.git("-C", str(self.dir / "remote.git"), "symbolic-ref",
                 "HEAD", "refs/heads/main")
        # An abbreviation of the tip the recorded base commit does not
        # share, so the short-sha case is decided by construction
        # rather than by an assumed hex-prefix collision.
        self.short = next(
            self.tip[:width]
            for width in range(4, 41)
            if not self.base.startswith(self.tip[:width])
        )

    def git(self, *args):
        subprocess.run(
            ["git", *args],
            check=True,
            capture_output=True,
            text=True,
            env={
                "GIT_AUTHOR_NAME": "reference plant ci",
                "GIT_AUTHOR_EMAIL": "ci@example.invalid",
                "GIT_COMMITTER_NAME": "reference plant ci",
                "GIT_COMMITTER_EMAIL": "ci@example.invalid",
                "HOME": str(self.dir),
                "PATH": "/usr/bin:/bin:/usr/local/bin",
            },
        )

    def rev(self, ref):
        return subprocess.run(
            ["git", "-C", str(self.dir / "seed"), "rev-parse", ref],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tree(self, kind, value, recorded, name="dcs-model", source=None, remote=None):
        """A scratch tree whose manifest declares `kind = "<value>"`
        on this remote and whose lockfile records `recorded` for every
        release crate — `source` replaces each recorded source whole,
        for the leak and divergent-query cases — `remote` pins another
        URL in the manifest and the lockfile alike, for the
        unreachable-remote case — and `name` renaming the third block,
        for the absent-crate case. The scratch root is under the
        remote's, so one `rmtree` takes every case with it."""
        directory = Path(tempfile.mkdtemp(dir=self.dir))
        url = self.url if remote is None else remote
        pin = f'{{ git = "{url}", {kind} = "{value}" }}'
        # A manifest cargo can read: `cargo metadata` — how the leg
        # resolves the declared pin — refuses a package with no target.
        (directory / "src").mkdir()
        (directory / "src" / "main.rs").write_text("fn main() {}\n")
        (directory / "Cargo.toml").write_text(
            '[package]\nname = "movable-pin"\nversion = "0.1.0"\nedition = "2021"\n'
            "\n[dependencies]\n"
            f"dcs-build = {pin}\ndcs-model = {pin}\n"
        )
        line = f'source = "git+{url}?{kind}={value}#{recorded}"'
        if source is not None:
            line = source.format(url=self.url, query=f"{kind}={value}", sha=recorded)
        blocks = [
            f'[[package]]\nname = "{crate}"\nversion = "0.10.0"\n{line}\n'
            for crate in ("dcs-build", "dcs-core", name)
        ]
        (directory / "Cargo.lock").write_text("version = 4\n\n" + "\n".join(blocks))
        return directory

    def run(self, directory, remote=None, record=""):
        """The leg's own exit status over one scratch tree, with its
        stdout and stderr captured. The manifest resolves in the
        working directory, so the run is rooted in `directory`."""
        finished = subprocess.run(
            [
                sys.executable, str(_LEG), "Cargo.lock",
                self.url if remote is None else remote,
                str(record) if record else "",
            ],
            cwd=directory,
            capture_output=True,
            text=True,
        )
        return finished.returncode, finished.stdout, finished.stderr


REMOTE = None


def setUpModule():
    global REMOTE
    REMOTE = Remote()


def tearDownModule():
    shutil.rmtree(REMOTE.dir, ignore_errors=True)


class MovablePinTests(unittest.TestCase):
    """Every `rev =` spelling Cargo resolves dynamically is compared
    against what the remote serves — the reported defect accepted a
    stale lockfile for each of them."""

    def assert_refused(self, status, err, recorded, named):
        self.assertEqual(status, 1, f"the leg passed a stale lockfile: {err}")
        self.assertIn(recorded, err, f"the refusal named no recorded revision: {err}")
        self.assertIn(named, err, f"the refusal named no revision the pin names: {err}")

    def test_a_branch_pin_recording_a_commit_it_moved_past_is_refused(self):
        # The reported reproduction: `rev = "main"` against a lockfile
        # recording the commit the branch has moved past. The refusal
        # names the revision read back off the remote, so it is the
        # `tag` path's ls-remote comparison, not a silent pass.
        status, _, err = REMOTE.run(REMOTE.tree("rev", "main", REMOTE.base))
        self.assert_refused(status, err, REMOTE.base, REMOTE.tip)
        self.assertIn("refs/heads/main", err)

    def test_a_branch_pin_recording_its_current_target_passes(self):
        status, out, _ = REMOTE.run(REMOTE.tree("rev", "main", REMOTE.tip))
        self.assertEqual(status, 0, out)
        self.assertIn(f"records rev=main at {REMOTE.tip}", out)

    def test_a_short_sha_pin_abbreviating_another_revision_is_refused(self):
        status, _, err = REMOTE.run(REMOTE.tree("rev", REMOTE.short, REMOTE.base))
        self.assert_refused(status, err, REMOTE.base, REMOTE.tip)
        self.assertIn(REMOTE.short, err)

    def test_a_short_sha_pin_abbreviating_the_recorded_revision_passes(self):
        status, out, _ = REMOTE.run(REMOTE.tree("rev", REMOTE.short, REMOTE.tip))
        self.assertEqual(status, 0, out)
        self.assertIn(f"records rev={REMOTE.short} at {REMOTE.tip}", out)

    def test_a_tag_name_spelled_through_rev_is_read_back_off_the_remote(self):
        status, _, err = REMOTE.run(REMOTE.tree("rev", "movable-tag", REMOTE.base))
        self.assert_refused(status, err, REMOTE.base, REMOTE.tip)
        self.assertIn("refs/tags/movable-tag", err)

    def test_a_tag_name_spelled_through_rev_at_its_target_passes(self):
        status, out, _ = REMOTE.run(REMOTE.tree("rev", "movable-tag", REMOTE.tip))
        self.assertEqual(status, 0, out)

    def test_a_full_sha_rev_pin_is_compared_literally(self):
        status, _, err = REMOTE.run(REMOTE.tree("rev", REMOTE.tip, REMOTE.base))
        self.assertEqual(status, 1)
        self.assertIn(f"records {REMOTE.base} for rev {REMOTE.tip}", err)
        status, out, _ = REMOTE.run(REMOTE.tree("rev", REMOTE.tip, REMOTE.tip))
        self.assertEqual(status, 0, out)

    def test_the_remote_head_is_a_served_ref_like_any_other(self):
        status, _, err = REMOTE.run(REMOTE.tree("rev", "HEAD", REMOTE.base))
        self.assert_refused(status, err, REMOTE.base, REMOTE.tip)
        self.assertIn("names HEAD at", err)

    def test_a_tag_pin_with_a_moved_target_is_refused(self):
        status, _, err = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.base))
        self.assert_refused(status, err, REMOTE.base, REMOTE.tip)

    def test_a_tag_pin_at_its_target_passes(self):
        status, out, _ = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.tip))
        self.assertEqual(status, 0, out)
        self.assertIn(f"records tag=movable-tag at {REMOTE.tip}", out)

    def test_a_pin_the_remote_serves_nothing_for_is_named_unresolvable(self):
        status, out, _ = REMOTE.run(REMOTE.tree("rev", "no-such-branch", REMOTE.base))
        self.assertEqual(status, 0, out)
        self.assertIn("unresolvable pin is pin-unresolvable's finding", out)

    def test_an_unreachable_remote_leaves_the_target_unverifiable(self):
        # The pin's target cannot be read, which is not the same as the
        # pin being absent: nothing downstream re-checks it, so the leg
        # refuses on its own status rather than passing. The manifest
        # declares the unreachable remote, so the leg reaches the query
        # rather than the remote-mismatch refusal.
        absent = f"file://{REMOTE.dir / 'absent.git'}"
        for kind, value in (("tag", "movable-tag"), ("rev", "main")):
            status, _, err = REMOTE.run(
                REMOTE.tree(kind, value, REMOTE.base, remote=absent), remote=absent
            )
            self.assertEqual(status, 3, f"the {kind} pin's target went unchecked: {err}")
            self.assertIn("could not be queried", err)


class RecordedSourceTests(unittest.TestCase):
    """The lockfile's own record: what it records must be a git source
    on the declared pin, every release crate's, at one revision."""

    def test_a_release_crate_recorded_with_no_source_is_the_leak_status(self):
        directory = REMOTE.tree("rev", "main", REMOTE.tip, source="")
        status, _, err = REMOTE.run(directory)
        self.assertEqual(status, 2, f"the leak status was not carried: {err}")
        self.assertIn("path into some checkout", err)

    def test_a_non_git_source_is_the_leak_status(self):
        directory = REMOTE.tree(
            "rev", "main", REMOTE.tip,
            source='source = "registry+https://example.invalid/{sha}"',
        )
        status, _, err = REMOTE.run(directory)
        self.assertEqual(status, 2, err)
        self.assertIn("only a git source satisfies a git pin", err)

    def test_release_crates_recording_two_revisions_are_stale(self):
        directory = REMOTE.tree("rev", "main", REMOTE.tip)
        lock = directory / "Cargo.lock"
        lock.write_text(lock.read_text().replace(f"#{REMOTE.tip}", f"#{REMOTE.base}", 1))
        status, _, err = REMOTE.run(directory)
        self.assertEqual(status, 1)
        self.assertIn("record different sources", err)

    def test_a_release_crate_with_no_package_block_is_stale(self):
        directory = REMOTE.tree("rev", "main", REMOTE.tip, name="dcs-absent")
        status, _, err = REMOTE.run(directory)
        self.assertEqual(status, 1)
        self.assertIn("release crates missing", err)

    def test_a_lockfile_recording_another_query_is_stale(self):
        directory = REMOTE.tree(
            "rev", "main", REMOTE.tip,
            source='source = "git+{url}?tag=movable-tag#{sha}"',
        )
        status, _, err = REMOTE.run(directory)
        self.assertEqual(status, 1)
        self.assertIn("declares", err)
        self.assertIn("rev=main", err)

    def test_the_release_records_filled_commit_must_name_the_recorded_revision(self):
        record = Path(tempfile.mkdtemp(dir=REMOTE.dir)) / "record.md"
        record.write_text(f"# Release\n\n| Commit | `{REMOTE.base}` |\n")
        status, _, err = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.tip), record=record)
        self.assertEqual(status, 1)
        self.assertIn(f"records commit {REMOTE.base}", err)
        record.write_text(f"# Release\n\n| Commit | `{REMOTE.tip}` |\n")
        status, out, _ = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.tip), record=record)
        self.assertEqual(status, 0, out)


class CheckStageTests(unittest.TestCase):
    """`ci/check.sh`'s `lockfile` stage runs this script and proves the
    movable-pin comparison on a scratch remote it seeds itself."""

    def test_the_stages_leg_is_the_extracted_script(self):
        text = _CHECK.read_text()
        leg = text.split("lockfile_leg() {", 1)[1].split("}", 1)[0]
        self.assertIn("ci/lockfile.py", leg)

    def test_the_stage_declares_no_refusal_it_stands_down_on(self):
        # The reported defect's other face: the stage read a `rev` that
        # names no immutable target as having no served-target
        # comparison to exercise, so the movable spellings were never
        # compared at all. Every spelling now names a revision, and
        # each is exercised below on a scratch remote the stage seeds.
        text = _CHECK.read_text()
        self.assertNotIn('marker = ""', text)
        self.assertNotIn("no served-target comparison to exercise", text)

    def test_the_stage_checks_every_movable_rev_spelling_twice(self):
        text = _CHECK.read_text()
        for case in ("main", '"$PIN_SHORT"', "movable-tag"):
            for want in ("refuse", "accept"):
                self.assertIn(
                    f'movable_pin_case {case} "$PIN_{"BASE" if want == "refuse" else "TIP"}" {want}',
                    text,
                )


if __name__ == "__main__":
    unittest.main()
