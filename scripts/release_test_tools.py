#!/usr/bin/env python3
"""Build test tooling from an immutable baseline revision, never the working tree.

Called only inside scripts/verify.py's build gate. This prevents a current
schema from masquerading as a historical release artifact in consumer tests.
"""
import argparse
import fcntl
import os
import re
from pathlib import Path
import subprocess
import tarfile
import tempfile


def build(root, revision, cargo, target_root):
    # A caller may use an exact baseline SHA, or the precise customer lock
    # for an existing tag. Pending release-tool substitutions are explicit.
    lock = (root / 'reference-plant/Cargo.lock').read_text()
    pinned = re.search(r'git\+[^\"\n]+\?tag=' + re.escape(revision) + r'#([0-9a-f]{40})', lock)
    identity = pinned.group(1) if pinned else revision
    # CI's Rust gates fetch full history for immutable consumer artifacts.
    archive_root = root
    commit = subprocess.check_output(['git', 'rev-parse', '--verify', identity + '^{commit}'], cwd=root, text=True).strip()
    directory = Path(target_root) / commit
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'build.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        source = directory / 'source'
        if not (source / 'Cargo.toml').exists():
            source.mkdir(exist_ok=True)
            with tempfile.TemporaryFile() as archive:
                subprocess.run(['git', 'archive', commit], cwd=archive_root, stdout=archive, check=True)
                archive.seek(0)
                with tarfile.open(fileobj=archive) as tree:
                    # Python 3.11 on the release build image predates filters.
                    # Accept only regular files/directories contained in this
                    # immutable Git archive; never follow archived links.
                    for member in tree.getmembers():
                        if not (source / member.name).resolve().is_relative_to(source.resolve()) or not (member.isfile() or member.isdir()):
                            raise ValueError('unsupported release archive member: ' + member.name)
                    tree.extractall(source)
        env = dict(os.environ, CARGO_TARGET_DIR=str(directory / 'target'), CARGO_BUILD_JOBS='1')
        subprocess.run([cargo, 'build', '--locked', '--quiet', '-p', 'dcs-model', '-p', 'dcs-controller', '-p', 'dcs-plant', '-p', 'dcs-monitor', '-p', 'dcs-sim-net'], cwd=source, env=env, check=True)
        return directory / 'target' / 'debug'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--cargo', default=os.environ.get('CARGO', 'cargo'))
    parser.add_argument('--target-root', required=True)
    args = parser.parse_args()
    print(build(Path(args.root), args.revision, args.cargo, args.target_root))

if __name__ == '__main__':
    main()
