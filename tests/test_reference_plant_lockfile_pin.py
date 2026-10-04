"""`ci/lockfile_pin.py` — the reference plant's committed-lockfile leg
— unit-tested against a seeded local remote: the leg compares the
committed `Cargo.lock` against the pin `Cargo.toml` declares, at the
revision that pin names, and Cargo resolves any git object in `rev =`
— so every spelling is covered here against a remote seeded with two
commits, a `main` moved past the first and a tag on the second: a
branch name, an abbreviated sha, a tag name spelled through `rev`, and
the `tag =` spelling itself are each read back off the remote and
compared, while a full sha is compared literally. The reported defect
(`lockfile-leg-rev-compare-skipped-for-non-full-sha`) skipped the
comparison for every spelling but a full sha, so a lockfile recording a
commit the branch had moved past passed the leg that exists to report
exactly that; a pin the remote serves nothing for stays
`pin-unresolvable`'s finding and says so out loud rather than passing
silently. The leak and stale halves are covered with it: a release
crate recorded with no `source` at all — a `path` package — carries its
own exit status, a missing release crate and a divergent query are
stale, and the release record's filled `Commit` field must name the
recorded revision. `ci/check.sh`'s `lockfile` stage runs this same
module, which these tests hold to the stage's own delegation."""
import contextlib
import importlib.util
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

_CI = Path(__file__).resolve().parents[1] / "reference-plant" / "ci"
_LEG = _CI / "lockfile_pin.py"
_CHECK = _CI / "check.sh"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


lockfile_pin = load(_LEG, "lockfile_pin")


class Remote:
    """A local stand-in for the published origin: a bare repository the
    tests seed with two commits, `main` moved past the first and a tag
    `movable-tag` on the second, so every movable spelling names the
    revision the tests compare against. No network is involved."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="dcs-lockfile-pin-"))
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
        # An abbreviation of the tip the recorded base commit does not
        # share, so the short-sha case is decided by construction
        # rather than by an assumed hex-prefix collision.
        self.short = next(
            self.tip[:width]
            for width in range(4, 41)
            if not self.base.startswith(self.tip[:width])
        )

    def git(self, *args):
        subprocess.run(["git", *args], check=True, capture_output=True, text=True)

    def rev(self, ref):
        return subprocess.run(
            ["git", "-C", str(self.dir / "seed"), "rev-parse", ref],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def tree(self, kind, value, recorded, name="dcs-model", source=None):
        """A scratch manifest declaring `kind = "<value>"` on this
        remote and a scratch lockfile recording `recorded` for every
        release crate — `source` replaces each recorded source line
        whole, for the leak and divergent-query cases — and `name`
        renaming the third block, for the absent-crate case."""
        directory = Path(tempfile.mkdtemp(dir=self.dir))
        pin = f'{{ git = "{self.url}", {kind} = "{value}" }}'
        (directory / "Cargo.toml").write_text(
            '[package]\nname = "movable-pin"\nversion = "0.1.0"\nedition = "2021"\n'
            "\n[dependencies]\n"
            f"dcs-build = {pin}\ndcs-model = {pin}\n"
        )
        line = f'source = "git+{self.url}?{kind}={value}#{recorded}"'
        if source is not None:
            line = source.format(url=self.url, query=f"{kind}={value}", sha=recorded)
        blocks = [
            f'[[package]]\nname = "{crate}"\nversion = "0.10.0"\n{line}\n'
            for crate in ("dcs-build", "dcs-core", name)
        ]
        (directory / "Cargo.lock").write_text("version = 4\n\n" + "\n".join(blocks))
        return directory

    def run(self, directory, record=""):
        """The leg's own exit status over one scratch pair, with its
        stdout and stderr captured."""
        out, err = io.StringIO(), io.StringIO()
        argv = [
            "--manifest", str(directory / "Cargo.toml"),
            "--lock", str(directory / "Cargo.lock"),
            "--remote", self.url,
            "--record", str(record) if record else "",
        ]
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = lockfile_pin.main(argv)
        return status, out.getvalue(), err.getvalue()


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

    def test_a_tag_pin_with_a_moved_target_is_refused(self):
        status, _, err = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.base))
        self.assert_refused(status, err, REMOTE.base, REMOTE.tip)

    def test_a_tag_pin_at_its_target_passes(self):
        status, out, _ = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.tip))
        self.assertEqual(status, 0, out)
        self.assertIn(f"records tag=movable-tag at {REMOTE.tip}", out)

    def test_a_full_sha_rev_pin_is_compared_literally(self):
        status, _, err = REMOTE.run(REMOTE.tree("rev", REMOTE.tip, REMOTE.base))
        self.assertEqual(status, 1)
        self.assertIn(f"records {REMOTE.base} for rev {REMOTE.tip}", err)
        status, out, _ = REMOTE.run(REMOTE.tree("rev", REMOTE.tip, REMOTE.tip))
        self.assertEqual(status, 0, out)

    def test_a_pin_the_remote_serves_nothing_for_is_named_unresolvable(self):
        status, out, _ = REMOTE.run(REMOTE.tree("rev", "no-such-branch", REMOTE.base))
        self.assertEqual(status, 0, out)
        self.assertIn("unresolvable pin is pin-unresolvable's finding", out)

    def test_an_unreachable_remote_leaves_the_query_comparison_holding(self):
        absent = REMOTE.dir / "absent.git"
        self.assertEqual(lockfile_pin.remote_refs(f"file://{absent}"), {})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            lockfile_pin.check_precise(REMOTE.tip, REMOTE.url, REMOTE.tip, "tag",
                                       "movable-tag", {})
            lockfile_pin.check_precise(REMOTE.tip, REMOTE.url, REMOTE.tip, "rev",
                                       "main", {})
        self.assertEqual(
            out.getvalue().count("unresolvable pin is pin-unresolvable's finding"), 2,
            out.getvalue(),
        )


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
        status, _, err = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.tip), record)
        self.assertEqual(status, 1)
        self.assertIn(f"records commit {REMOTE.base}", err)
        record.write_text(f"# Release\n\n| Commit | `{REMOTE.tip}` |\n")
        status, out, _ = REMOTE.run(REMOTE.tree("tag", "movable-tag", REMOTE.tip), record)
        self.assertEqual(status, 0, out)


class ServedNameTests(unittest.TestCase):
    """The ref-name resolution the movable `rev` path compares
    through: git's own `rev-parse` shapes first, any other namespace's
    entry of that name only when the remote serves none of them, and an
    annotated tag read peeled."""

    def test_the_heads_and_head_shapes_resolve(self):
        served = {"refs/heads/main": "a" * 40, "HEAD": "b" * 40}
        self.assertEqual(lockfile_pin.served_name(served, "main"), {"refs/heads/main": "a" * 40})
        self.assertEqual(lockfile_pin.served_name(served, "HEAD"), {"HEAD": "b" * 40})

    def test_an_annotated_tag_resolves_to_its_peeled_commit(self):
        served = {"refs/tags/v1.2.3": "c" * 40, "refs/tags/v1.2.3^{}": "d" * 40}
        self.assertEqual(
            lockfile_pin.served_name(served, "v1.2.3"), {"refs/tags/v1.2.3": "d" * 40}
        )

    def test_another_namespaces_entry_resolves_when_no_shape_is_served(self):
        served = {"refs/pull/7/head": "e" * 40}
        self.assertEqual(
            lockfile_pin.served_name(served, "7/head"), {"refs/pull/7/head": "e" * 40}
        )

    def test_a_name_the_remote_serves_nothing_for_resolves_to_nothing(self):
        self.assertEqual(lockfile_pin.served_name({"refs/heads/main": "a" * 40}, "nope"), {})


class CheckStageTests(unittest.TestCase):
    """`ci/check.sh`'s `lockfile` stage runs the extracted leg, so the
    module these tests cover is the one a consumer's own CI runs."""

    def test_the_stages_leg_is_the_extracted_module(self):
        text = _CHECK.read_text()
        leg = text.split("lockfile_leg() {", 1)[1].split("}", 1)[0]
        self.assertIn("ci/lockfile_pin.py", leg)
        self.assertNotIn("re.fullmatch", text.split('echo "== lockfile =="', 1)[1])


if __name__ == "__main__":
    unittest.main()
