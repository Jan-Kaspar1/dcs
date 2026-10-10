"""Identity and offline-source guarantees for the connected-area delivery."""

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/water_artifact_bundle.py"
SPEC = importlib.util.spec_from_file_location("water_artifact_bundle", SCRIPT)
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class WaterArtifactDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def archive(self, entries=None):
        source = self.root / "source"
        source.write_bytes(b"immutable artifact\n")
        archive = self.root / "delivery.tar.gz"
        delivery.write_archive(archive, entries or {"bin/tool": (source, True)}, {"status": "candidate"})
        return archive

    def fixture_customer(self):
        plant = self.root / "plant"
        plant.mkdir()
        origin = "file:///unavailable/candidate"
        tag = "v0.11.0-rc.1"
        commit = "a" * 40
        (plant / "Cargo.toml").write_text(
            '[package]\nname = "customer"\nversion = "0.1.0"\n[workspace]\n[dependencies]\n'
            f'dcs-build = {{ git = "{origin}", tag = "{tag}" }}\n'
            f'dcs-model = {{ git = "{origin}", tag = "{tag}" }}\n'
        )
        source = f"git+{origin}?tag={tag}#{commit}"
        (plant / "Cargo.lock").write_text(
            'version = 4\n[[package]]\nname = "dcs-build"\nversion = "0.11.0-rc.1"\n'
            f'source = "{source}"\n[[package]]\nname = "dcs-model"\nversion = "0.11.0-rc.1"\nsource = "{source}"\n'
        )
        evidence = {"candidate_origin": origin, "tag": tag, "commit": commit,
                    "customer_lock_sha256": delivery.sha256(plant / "Cargo.lock")}
        return plant, evidence

    def test_archive_bytes_do_not_depend_on_source_mtime_or_destination_name(self):
        first = self.archive()
        expected = first.read_bytes()
        source = self.root / "source"
        source.touch()
        second = self.root / "other-name.tar.gz"
        delivery.write_archive(second, {"bin/tool": (source, True)}, {"status": "candidate"})
        self.assertEqual(second.read_bytes(), expected)

    def test_restore_verifies_bytes_and_installs_executable_mode(self):
        archive = self.archive()
        target = self.root / "restored"
        manifest = delivery.read_archive(archive, target)
        self.assertEqual((target / "bin/tool").read_bytes(), b"immutable artifact\n")
        self.assertEqual((target / "bin/tool").stat().st_mode & 0o777, 0o755)
        self.assertEqual(manifest["files"]["bin/tool"]["sha256"], hashlib.sha256(b"immutable artifact\n").hexdigest())

    def test_changed_member_refused_before_creating_restore_directory(self):
        archive = self.archive()
        with tarfile.open(archive, "r:gz") as original:
            members = [(member, original.extractfile(member).read()) for member in original.getmembers()]
        tampered = self.root / "tampered.tar.gz"
        with tarfile.open(tampered, "w:gz") as output:
            for member, data in members:
                if member.name == "bin/tool":
                    data = b"a different tool!\n"
                    member.size = len(data)
                output.addfile(member, io.BytesIO(data))
        target = self.root / "refused"
        with self.assertRaisesRegex(ValueError, "size/mode mismatch|hash mismatch"):
            delivery.read_archive(tampered, target)
        self.assertFalse(target.exists())

    def test_unlisted_file_and_traversal_are_refused_before_any_write(self):
        for path in ("extra", "../escape"):
            with self.subTest(path=path):
                archive = self.root / ("unsafe-" + path.replace("/", "_") + ".tar.gz")
                manifest = delivery.json_bytes({"format": delivery.FORMAT, "files": {}})
                with tarfile.open(archive, "w:gz") as output:
                    first = tarfile.TarInfo("manifest.json")
                    first.size = len(manifest)
                    output.addfile(first, io.BytesIO(manifest))
                    extra = tarfile.TarInfo(path)
                    extra.size = 1
                    output.addfile(extra, io.BytesIO(b"x"))
                target = self.root / "refused"
                with self.assertRaisesRegex(ValueError, "unsafe paths|differ from its manifest"):
                    delivery.read_archive(archive, target)
                self.assertFalse(target.exists())

    def test_symlink_is_not_an_archive_dependency(self):
        archive = self.root / "symlink.tar.gz"
        with tarfile.open(archive, "w:gz") as output:
            link = tarfile.TarInfo("bin/tool")
            link.type = tarfile.SYMTYPE
            link.linkname = "/some/other/tool"
            output.addfile(link)
        with self.assertRaisesRegex(ValueError, "special or unsafe"):
            delivery.read_archive(archive, self.root / "refused")

    def test_restore_never_overwrites_an_existing_checkout(self):
        archive = self.archive()
        target = self.root / "owned"
        target.mkdir()
        (target / "keep").write_text("customer work")
        with self.assertRaisesRegex(ValueError, "must not already exist"):
            delivery.read_archive(archive, target)
        self.assertEqual((target / "keep").read_text(), "customer work")

    def test_customer_revision_and_lock_hash_must_agree(self):
        plant, evidence = self.fixture_customer()
        delivery.validate_customer(plant, evidence)
        lock = plant / "Cargo.lock"
        lock.write_text(lock.read_text().replace("a" * 40, "b" * 40))
        evidence["customer_lock_sha256"] = delivery.sha256(lock)
        with self.assertRaisesRegex(ValueError, "revision/version"):
            delivery.validate_customer(plant, evidence)

    def test_path_dependency_refused_even_when_dcs_lock_is_valid(self):
        plant, evidence = self.fixture_customer()
        with (plant / "Cargo.toml").open("a") as output:
            output.write('helper = { path = "../../platform" }\n')
        with self.assertRaisesRegex(ValueError, "path dependency"):
            delivery.validate_customer(plant, evidence)

    def test_target_build_dependency_cannot_escape_to_platform_workspace(self):
        plant, evidence = self.fixture_customer()
        with (plant / "Cargo.toml").open("a") as output:
            output.write('[target.\'cfg(unix)\'.build-dependencies]\nhelper = { path = "../../platform" }\n')
        with self.assertRaisesRegex(ValueError, "path dependency"):
            delivery.validate_customer(plant, evidence)

    def test_every_remote_dependency_resolves_from_delivered_vendor(self):
        plant, evidence = self.fixture_customer()
        source = f'git+{evidence["candidate_origin"]}?tag={evidence["tag"]}#{evidence["commit"]}'
        packages = [{"name": "dcs-build", "version": evidence["tag"][1:], "source": source,
                     "manifest_path": str(plant / "vendor/dcs-build/Cargo.toml")},
                    {"name": "serde", "version": "1", "source": "registry+crates.io",
                     "manifest_path": str(plant / "vendor/serde/Cargo.toml")}]
        delivery.validate_metadata({"packages": packages}, plant, evidence)
        packages[1]["manifest_path"] = str(self.root / "cached-registry/serde/Cargo.toml")
        with self.assertRaisesRegex(ValueError, "delivered vendor"):
            delivery.validate_metadata({"packages": packages}, plant, evidence)

    def test_vendor_replacement_preserves_original_pin_without_expired_origin(self):
        original = "file:///expired/release"
        relocated = "file:///scratch/plant/.artifact-origin"
        config = (
            '[source.crates-io]\nreplace-with = "vendored-sources"\n'
            f'[source."git+{relocated}?tag=v0.11.0-rc.1"]\ngit = "{relocated}"\n'
            'tag = "v0.11.0-rc.1"\nreplace-with = "vendored-sources"\n'
            '[source.vendored-sources]\ndirectory = "/scratch/vendor"\n'
        )
        result = delivery.vendor_config(config, relocated, original).decode()
        self.assertIn(f'git = "{original}"', result)
        self.assertNotIn(relocated, result)
        self.assertIn('directory = "vendor"', result)
        self.assertIn("offline = true", result)

    def test_generic_tool_mismatch_refused_instead_of_using_workspace_fallback(self):
        wrong = self.root / "wrong-tool"
        wrong.write_bytes(b"workspace version")
        with self.assertRaisesRegex(ValueError, "artifact hash mismatch"):
            delivery.exact_file(wrong, hashlib.sha256(b"released version").hexdigest())


if __name__ == "__main__":
    unittest.main()
