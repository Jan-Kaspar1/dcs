#!/usr/bin/env python3
"""Export and restore one exact connected-water candidate and customer checkout.

The archive preserves original customer Git/lock identities, bundles the exact
tested executables, and vendors locked Cargo sources for an offline rebuild.
It is a simulated candidate delivery, not publication or plant activation.
"""

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import tomllib


FORMAT = 1
TOOLS = ("dcs-model", "dcs-controller", "dcs-plant-server")
DOCUMENTS = ("model", "dynamics", "three_pump_model", "three_pump_dynamics", "scenario")


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def run(argv, cwd=None, capture=False, env=None):
    result = subprocess.run(
        [str(arg) for arg in argv], cwd=cwd, env=env, check=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else None


def exact_file(path, expected):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or sha256(path) != expected:
        raise ValueError(f"artifact hash mismatch: {path}")
    return path


def validate_customer(plant, evidence):
    """The engineering inputs stay pinned to the archived immutable release."""
    manifest = tomllib.loads((plant / "Cargo.toml").read_text())
    lock = tomllib.loads((plant / "Cargo.lock").read_text())
    if "workspace" not in manifest:
        raise ValueError("customer must declare its independent workspace")
    if "patch" in manifest or "replace" in manifest:
        raise ValueError("customer dependency overrides are outside this artifact proof")
    if manifest["workspace"].get("members") or manifest["workspace"].get("dependencies"):
        raise ValueError("customer workspace must not import other source trees")
    dependency_tables = [manifest.get(key, {}) for key in ("dependencies", "dev-dependencies", "build-dependencies")]
    for target in manifest.get("target", {}).values():
        dependency_tables.extend(target.get(key, {}) for key in ("dependencies", "dev-dependencies", "build-dependencies"))
    dependencies = {name: dependency for table in dependency_tables for name, dependency in table.items()}
    dcs_dependencies = {name: value for name, value in dependencies.items() if name.startswith("dcs-")}
    if not {"dcs-build", "dcs-model"}.issubset(dcs_dependencies):
        raise ValueError("customer lacks the supported engineering dependencies")
    for dependency in dependencies.values():
        if isinstance(dependency, dict) and "path" in dependency:
            raise ValueError("customer path dependency refused")
    for name, dependency in dcs_dependencies.items():
        if dependency.get("git") != evidence["candidate_origin"] or dependency.get("tag") != evidence["tag"]:
            raise ValueError(f"customer pin mismatch: {name}")
    expected = f'git+{evidence["candidate_origin"]}?tag={evidence["tag"]}#{evidence["commit"]}'
    packages = [package for package in lock["package"] if package["name"].startswith("dcs-")]
    if not packages or any(package.get("source") != expected or package["version"] != evidence["tag"][1:] for package in packages):
        raise ValueError("customer lock does not match the release revision/version")
    exact_file(plant / "Cargo.lock", evidence["customer_lock_sha256"])


def validate_metadata(metadata, plant, evidence):
    expected = f'git+{evidence["candidate_origin"]}?tag={evidence["tag"]}#{evidence["commit"]}'
    dcs_packages = []
    for package in metadata["packages"]:
        path = Path(package["manifest_path"])
        if package.get("source") is not None and not path.is_relative_to(plant / "vendor"):
            raise ValueError("offline dependencies did not resolve from the delivered vendor artifacts")
        if package["name"].startswith("dcs-"):
            dcs_packages.append(package)
            if package.get("source") != expected or package["version"] != evidence["tag"][1:]:
                raise ValueError("resolved SDK identity differs from the exact release pin")
    if not dcs_packages:
        raise ValueError("offline metadata contains no DCS engineering packages")


def relocate_vendor_inputs(plant, evidence, origin):
    """Only scratch vendoring inputs move; the installed customer pins do not."""
    original = evidence["candidate_origin"]
    relocated = origin.resolve().as_uri()
    for name in ("Cargo.toml", "Cargo.lock"):
        path = plant / name
        source = path.read_text()
        if original not in source:
            raise ValueError(f"customer origin missing from {name}")
        path.write_text(source.replace(original, relocated))
    return relocated


def vendor_config(config, relocated, original):
    """Use Cargo's exact source replacement rather than repinning the customer."""
    config = config.replace(relocated, original)
    config, count = re.subn(r'^directory = ".*"$', 'directory = "vendor"', config, flags=re.MULTILINE)
    if count != 1:
        raise ValueError("unexpected cargo vendor source configuration")
    parsed = tomllib.loads(config)
    sources = parsed.get("source", {})
    if sources.get("vendored-sources", {}).get("directory") != "vendor":
        raise ValueError("missing portable Cargo vendor directory")
    if not any(source.get("git") == original for source in sources.values()):
        raise ValueError("vendor config does not replace the original Git source")
    return (config + "\n[net]\noffline = true\n").encode()


def write_archive(destination, entries, metadata):
    """Fixed ordering, owner, modes, timestamps and gzip header give stable bytes."""
    files = {}
    for name, (path, executable) in sorted(entries.items()):
        files[name] = {"sha256": sha256(path), "bytes": path.stat().st_size, "executable": executable}
    manifest = dict(metadata, format=FORMAT, files=files)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".partial")
    try:
        with temporary.open("wb") as output:
            with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0, compresslevel=6) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive:
                    data = json_bytes(manifest)
                    info = tarfile.TarInfo("manifest.json")
                    info.size, info.mode = len(data), 0o644
                    archive.addfile(info, io.BytesIO(data))
                    for name, (path, executable) in sorted(entries.items()):
                        info = tarfile.TarInfo(name)
                        info.size, info.mode = path.stat().st_size, 0o755 if executable else 0o644
                        with path.open("rb") as source:
                            archive.addfile(info, source)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return manifest


def export_bundle(evidence_path, tools, engineering, destination, cargo="cargo"):
    evidence_path = Path(evidence_path).resolve()
    evidence = json.loads(evidence_path.read_text())
    if not re.fullmatch(r"v0\.11\.0-rc\.\d+", evidence["tag"]) or not re.fullmatch(r"[0-9a-f]{40}", evidence["commit"]):
        raise ValueError("this delivery script expects a named connected-water candidate revision")
    entries = {}
    def add(name, path, expected, executable=False):
        entries[name] = (exact_file(path, expected), executable)
    source = evidence_path.parent
    add("source/release.bundle", source / evidence["candidate_bundle"], evidence["candidate_bundle_sha256"])
    add("source/customer.bundle", source / evidence["customer_bundle"], evidence["customer_bundle_sha256"])
    for name in TOOLS:
        add(f"bin/{name}", Path(tools) / name, evidence["tools"][name], True)
    add("bin/connected-water", engineering, evidence["engineering_binary_sha256"], True)
    for name in DOCUMENTS:
        artifact = evidence["documents"][name]
        add(f"documents/{name}.json", source / artifact["file"], artifact["sha256"])
    entries["evidence/customer.json"] = (evidence_path, False)
    helper = Path(__file__).resolve()
    entries["delivery/water_artifact_bundle.py"] = (helper, True)
    readme = helper.parents[1] / "reference-plant/deploy/ARTIFACT-BUNDLE.md"
    if readme.is_file():
        entries["delivery/README.md"] = (readme, False)
    with tempfile.TemporaryDirectory(prefix="dcs-water-export-") as temporary:
        scratch = Path(temporary)
        plant = scratch / "plant"
        run(["git", "clone", "--quiet", entries["source/customer.bundle"][0], plant])
        if run(["git", "rev-parse", "HEAD"], plant, capture=True).decode().strip() != evidence["customer_commit"]:
            raise ValueError("customer bundle revision mismatch")
        validate_customer(plant, evidence)
        # The origin is inside this checkout so a Cargo container mounting only
        # its current directory can fetch the exact source during vendoring.
        origin = plant / ".artifact-origin"
        run(["git", "clone", "--quiet", "--branch", evidence["tag"], entries["source/release.bundle"][0], origin])
        if run(["git", "rev-parse", "HEAD"], origin, capture=True).decode().strip() != evidence["commit"]:
            raise ValueError("release bundle revision mismatch")
        relocated = relocate_vendor_inputs(plant, evidence, origin)
        config = run([cargo, "vendor", "--locked", "--versioned-dirs", "vendor"], plant, capture=True).decode()
        config_path = scratch / "cargo-config.toml"
        config_path.write_bytes(vendor_config(config, relocated, evidence["candidate_origin"]))
        entries["offline/config.toml"] = (config_path, False)
        vendor = plant / "vendor"
        for path in sorted(vendor.rglob("*")):
            if path.is_symlink():
                raise ValueError(f"vendored symlink refused: {path}")
            if path.is_file():
                entries["offline/vendor/" + path.relative_to(vendor).as_posix()] = (path, False)
        metadata = {
            "status": "immutable simulated candidate delivery; public release pending",
            "release": {"tag": evidence["tag"], "commit": evidence["commit"]},
            "customer": {"commit": evidence["customer_commit"], "lock_sha256": evidence["customer_lock_sha256"]},
            "target": {"system": platform.system(), "machine": platform.machine(), "linkage": "Linux host libc, libm, libgcc_s and ELF loader required"},
            "offline": "locked Cargo source replacement; Rust toolchain itself is a host prerequisite",
        }
        manifest = write_archive(destination, entries, metadata)
    return {"archive": str(Path(destination).resolve()), "sha256": sha256(destination), "release": manifest["release"], "files": len(entries)}


def safe_name(name):
    path = PurePosixPath(name)
    return bool(name and not path.is_absolute() and ".." not in path.parts and str(path) == name and "\\" not in name)


def read_archive(archive_path, destination):
    """Validate every member before creating a customer delivery directory."""
    destination = Path(destination).resolve()
    if destination.exists():
        raise ValueError("restore destination must not already exist")
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)) or any(not member.isfile() or not safe_name(member.name) for member in members):
            raise ValueError("archive has duplicate, special or unsafe paths")
        manifests = [member for member in members if member.name == "manifest.json"]
        if len(manifests) != 1 or manifests[0].size > 16 * 1024 * 1024:
            raise ValueError("archive manifest missing or oversized")
        manifest = json.load(archive.extractfile(manifests[0]))
        if manifest.get("format") != FORMAT or not isinstance(manifest.get("files"), dict):
            raise ValueError("unsupported delivery manifest")
        files = manifest["files"]
        if set(names) != set(files) | {"manifest.json"}:
            raise ValueError("archive files differ from its manifest")
        for member in members:
            if member.name == "manifest.json":
                continue
            record = files[member.name]
            if record.get("bytes") != member.size or not isinstance(record.get("executable"), bool):
                raise ValueError(f"archive size/mode mismatch: {member.name}")
            digest = hashlib.sha256()
            with archive.extractfile(member) as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != record.get("sha256"):
                raise ValueError(f"archive hash mismatch: {member.name}")
        destination.mkdir(parents=True)
        # No extractall: members were checked and only ordinary bytes are written.
        for member in members:
            path = destination / member.name
            path.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, path.open("xb") as output:
                shutil.copyfileobj(source, output)
            path.chmod(0o755 if files.get(member.name, {}).get("executable") else 0o644)
    return manifest


def restore_bundle(archive_path, destination):
    destination = Path(destination).resolve()
    manifest = read_archive(archive_path, destination)
    evidence = json.loads((destination / "evidence/customer.json").read_text())
    if manifest["release"] != {"tag": evidence["tag"], "commit": evidence["commit"]}:
        raise ValueError("manifest release differs from candidate evidence")
    plant = destination / "plant"
    run(["git", "clone", "--quiet", destination / "source/customer.bundle", plant])
    if run(["git", "rev-parse", "HEAD"], plant, capture=True).decode().strip() != evidence["customer_commit"]:
        raise ValueError("restored customer revision mismatch")
    validate_customer(plant, evidence)
    release = destination / "release"
    run(["git", "clone", "--quiet", "--branch", evidence["tag"], destination / "source/release.bundle", release])
    if run(["git", "rev-parse", "HEAD"], release, capture=True).decode().strip() != evidence["commit"]:
        raise ValueError("restored release revision mismatch")
    if (plant / ".cargo/config.toml").exists() or (plant / "vendor").exists():
        raise ValueError("customer already owns Cargo source replacement; merge policy required")
    (plant / ".cargo").mkdir(exist_ok=True)
    shutil.copy2(destination / "offline/config.toml", plant / ".cargo/config.toml")
    shutil.copytree(destination / "offline/vendor", plant / "vendor")
    validate_customer(plant, evidence)
    return {"delivery": str(destination), "release": manifest["release"], "customer_commit": evidence["customer_commit"], "lock_identity_preserved": True}


def check_bundle(destination, cargo="cargo", rebuild=False):
    if rebuild and not os.environ.get("DCS_BUILD_SLOT_FD"):
        raise ValueError("rebuild must run through scripts/verify.py's shared build gate")
    destination = Path(destination).resolve()
    manifest = json.loads((destination / "manifest.json").read_text())
    for name, record in manifest["files"].items():
        exact_file(destination / name, record["sha256"])
    evidence = json.loads((destination / "evidence/customer.json").read_text())
    plant = destination / "plant"
    validate_customer(plant, evidence)
    if (plant / ".cargo/config.toml").read_bytes() != (destination / "offline/config.toml").read_bytes():
        raise ValueError("installed Cargo replacement differs from the archived artifact")
    for name, record in manifest["files"].items():
        if name.startswith("offline/vendor/"):
            exact_file(plant / "vendor" / name.removeprefix("offline/vendor/"), record["sha256"])
    empty_cache = plant / ".delivery-cargo-home"
    empty_cache.mkdir(exist_ok=True)
    target = plant / ".delivery-target"
    delivery_env = dict(os.environ, CARGO_HOME=str(empty_cache), CARGO_TARGET_DIR=str(target))
    # CARGO_TARGET_DIR outranks Cargo config. Override the inherited platform
    # target explicitly; --frozen precludes fetching or changing the lock.
    metadata = json.loads(run([cargo, "metadata", "--frozen", "--format-version", "1", "--features", "connected-water"], plant, capture=True, env=delivery_env))
    if Path(metadata["target_directory"]).resolve() != target:
        raise ValueError("offline customer target escaped its independent checkout")
    validate_metadata(metadata, plant, evidence)
    binary = destination / "bin/connected-water"
    if rebuild:
        run([cargo, "build", "--target-dir", target, "--frozen", "--features", "connected-water", "--bin", "connected-water"], plant, env=delivery_env)
        binary = target / "debug/connected-water"
    emitted = {}
    for name, arguments in (("model", []), ("dynamics", ["--dynamics"]), ("three_pump_model", ["--add-pump"]), ("three_pump_dynamics", ["--add-pump", "--dynamics"])):
        data = run([binary, *arguments], plant, capture=True)
        digest = hashlib.sha256(data).hexdigest()
        # Historical three-pump model was reserialized by the proof. Its
        # semantic JSON, rather than whitespace, is the artifact comparison.
        artifact = destination / f"documents/{name}.json"
        if name == "three_pump_model":
            if json.loads(data) != json.loads(artifact.read_bytes()):
                raise ValueError("three-pump engineering output differs from the proven model")
        elif digest != evidence["documents"][name]["sha256"]:
            raise ValueError(f"engineering emission differs from proven artifact: {name}")
        emitted[name] = digest
    tools = destination / "bin"
    for model_name, dynamics_name in (("model", "dynamics"), ("three_pump_model", "three_pump_dynamics")):
        model = destination / f"documents/{model_name}.json"
        dynamics = destination / f"documents/{dynamics_name}.json"
        run([tools / "dcs-model", "validate", model])
        run([tools / "dcs-controller", model, "--check"])
        run([tools / "dcs-plant-server", model, "--check-dynamics", dynamics])
    validate_customer(plant, evidence)
    report = {"release": manifest["release"], "customer_commit": evidence["customer_commit"], "original_lock_sha256": evidence["customer_lock_sha256"], "lock_identity_preserved": True,
              "sdk_sources": "delivered locked Cargo vendor artifacts", "metadata_frozen": True, "offline_rebuilt": rebuild, "emitted": emitted,
              "generic_validate_assemble_dynamics": True, "tools": evidence["tools"], "public_release": False}
    (destination / "check-evidence.json").write_bytes(json_bytes(report))
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="package already-proven candidate artifacts and vendor locked sources")
    export.add_argument("--evidence", required=True)
    export.add_argument("--tools", required=True)
    export.add_argument("--engineering", required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--cargo", default=os.environ.get("CARGO", "cargo"))
    restore = commands.add_parser("restore", help="restore into a new independent directory")
    restore.add_argument("archive")
    restore.add_argument("destination")
    check = commands.add_parser("check", help="prove delivered identities, offline SDK resolution and generic tooling")
    check.add_argument("destination")
    check.add_argument("--cargo", default=os.environ.get("CARGO", "cargo"))
    check.add_argument("--rebuild", action="store_true", help="rebuild offline only under verify.py's inherited build gate")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_bundle(args.evidence, args.tools, args.engineering, args.output, args.cargo)
        elif args.command == "restore":
            result = restore_bundle(args.archive, args.destination)
        else:
            result = check_bundle(args.destination, args.cargo, args.rebuild)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"water-artifact-delivery-refused: {error}\n")


if __name__ == "__main__":
    main()
