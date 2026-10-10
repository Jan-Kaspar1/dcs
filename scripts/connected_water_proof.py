#!/usr/bin/env python3
"""Clean connected-area consumer proof over an immutable candidate release.

Invoked by the nested Rust proof through verify.py's build gate. It creates a
versioned file:// release origin in runner-owned scratch, not a public release.
Engineering dependencies and generic tooling are built from that exact revision.
The customer source is copied independently and contains no workspace paths.
Publishing the release and replacing the recorded public pin remain supervisor
operations. Evidence explicitly calls this a candidate, never a published cut.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = '0.11.0-rc.1'
TAG = 'v' + VERSION


def run(argv, cwd, env, **kwargs):
    return subprocess.run([str(a) for a in argv], cwd=cwd, env=env, check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cargo', default=os.environ.get('CARGO', 'cargo'))
    parser.add_argument('--evidence', default=str(ROOT / 'target/water-area-evidence/customer.json'))
    args = parser.parse_args()
    env = dict(os.environ, CARGO_BUILD_JOBS='1')
    with tempfile.TemporaryDirectory(prefix='dcs-water-customer-') as temporary:
        scratch = Path(temporary)
        origin = scratch / 'release'
        origin.mkdir()
        for name in ('Cargo.toml', 'Cargo.lock', 'rust-toolchain.toml'):
            shutil.copy2(ROOT / name, origin / name)
        shutil.copytree(ROOT / 'crates', origin / 'crates', ignore=shutil.ignore_patterns('target'))
        # Integration documentation is compiled into the generic assembly crate.
        shutil.copytree(ROOT / 'docs', origin / 'docs')
        for name in ('Cargo.toml', 'Cargo.lock'):
            path = origin / name
            path.write_text(path.read_text().replace('version = "0.10.0"', f'version = "{VERSION}"'))
        # Scratch-only release history. No staging, committing or tagging in the
        # assigned clone; this is the same immutable-pin stand-in as M11 proofs.
        git_env = dict(env, GIT_AUTHOR_DATE='2026-10-09T00:00:00Z', GIT_COMMITTER_DATE='2026-10-09T00:00:00Z')
        run(['git', 'init', '-q'], origin, git_env)
        run(['git', 'config', 'user.name', 'DCS release proof'], origin, git_env)
        run(['git', 'config', 'user.email', 'release-proof@example.invalid'], origin, git_env)
        run(['git', 'add', '.'], origin, git_env)
        run(['git', 'commit', '-qm', 'Immutable connected-water candidate artifact'], origin, git_env)
        run(['git', 'tag', TAG], origin, git_env)
        commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=origin, text=True).strip()
        # Cache only build products under the exact source identity. Cargo owns
        # its target lock; the consumer checkout and origin remain fresh per run.
        target = ROOT / 'target' / 'water-candidate' / commit
        artifact_env = dict(env, CARGO_TARGET_DIR=str(target))
        run([args.cargo, 'build', '--locked', '--quiet', '-p', 'dcs-controller', '-p', 'dcs-plant', '-p', 'dcs-model', '--bins'], origin, artifact_env)
        tools = target / 'debug'
        customer = scratch / 'plant-source'
        shutil.copytree(ROOT / 'reference-plant', customer, ignore=shutil.ignore_patterns('target', '__pycache__'))
        remote = origin.as_uri()
        manifest = customer / 'Cargo.toml'
        manifest.write_text(manifest.read_text().replace('https://github.com/Jan-Kaspar1/dcs.git', remote).replace('tag = "v0.10.0"', f'tag = "{TAG}"'))
        lock = customer / 'Cargo.lock'
        lock.write_text(re.sub(r'git\+https://github.com/Jan-Kaspar1/dcs.git\?tag=v0.10.0#[0-9a-f]+', f'git+{remote}?tag={TAG}#{commit}', lock.read_text()))
        lock.write_text(re.sub(r'(name = "dcs-[^"]+"\nversion = )"[^"]+"', lambda m: m.group(1) + '"' + VERSION + '"', lock.read_text()))
        run(['git', 'init', '-q'], customer, git_env)
        run(['git', 'config', 'user.name', 'Independent water customer'], customer, git_env)
        run(['git', 'config', 'user.email', 'customer-proof@example.invalid'], customer, git_env)
        run(['git', 'add', '.'], customer, git_env)
        run(['git', 'commit', '-qm', 'Customer engineering pinned to candidate artifacts'], customer, git_env)
        checkout = scratch / 'plant'
        run(['git', 'clone', '--quiet', '--no-local', customer, checkout], scratch, env)
        customer = checkout
        customer_commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=customer, text=True).strip()
        assert not subprocess.check_output(['git', 'status', '--porcelain'], cwd=customer), 'customer checkout must start clean'
        lock = customer / 'Cargo.lock'
        customer_env = dict(env, CARGO_TARGET_DIR=str(target / 'customer'))
        run([args.cargo, 'build', '--quiet', '--locked', '--features', 'connected-water', '--bin', 'connected-water'], customer, customer_env)
        binary = target / 'customer' / 'debug' / 'connected-water'
        def emit(*argv):
            return subprocess.check_output([str(binary), *argv], cwd=customer)
        first, second = emit(), emit()
        assert first == second, 'customer emit is not byte deterministic'
        model, dynamics = customer / 'model/connected-water.json', customer / 'model/connected-water-dynamics.json'
        model.write_bytes(first); dynamics.write_bytes(emit('--dynamics'))
        model_json = json.loads(first)
        assert model_json['version'] == 2, 'bounded controls must negotiate model version 2'
        # Every DCS dependency resolved from the immutable Git artifact; source
        # inside a Cargo Git checkout is allowed, workspace/path dependencies are not.
        import tomllib
        packages = tomllib.loads(lock.read_text())['package']
        assert all(p.get('source', '').startswith('git+' + remote) for p in packages if p['name'].startswith('dcs-'))
        run([tools / 'dcs-model', 'validate', model], customer, env)
        run([tools / 'dcs-controller', model, '--check'], customer, env)
        added = json.loads(emit('--add-pump'))
        assert len([e for e in added['equipment'] if e['kind'] == 'pump']) == 3
        assert len([n for n in added['views'][0]['nodes'] if n['symbol'] == 'pump']) == 3
        third = customer / 'model/three-pumps.json'; third.write_text(json.dumps(added))
        run([tools / 'dcs-controller', third, '--check'], customer, env)
        third_dynamics = customer / 'model/three-pump-dynamics.json'
        third_dynamics.write_bytes(emit('--add-pump', '--dynamics'))
        third_result = subprocess.check_output(['python3', 'ci/connected_water.py', '--model', str(third), '--dynamics', str(third_dynamics), '--controller', str(tools / 'dcs-controller'), '--plant-server', str(tools / 'dcs-plant-server')], cwd=customer, text=True).strip()
        common = ['--model', model, '--dynamics', dynamics, '--controller', tools / 'dcs-controller', '--plant-server', tools / 'dcs-plant-server']
        exercise = customer / 'ci/connected-water-scenario.json'
        exercise.write_bytes(subprocess.check_output(['python3', 'ci/connected_water.py', '--model', str(model), '--emit-scenario'], cwd=customer))
        digests = []
        for attempt in range(2):
            extended = ['--extended', '--evidence', str(customer / 'ci/operator-evidence.json')] if attempt == 0 else []
            result = subprocess.check_output(['python3', 'ci/connected_water.py', *map(str, common), *extended], cwd=customer, text=True)
            digests.append(result.strip())
        assert digests[0] == digests[1], 'customer scenario is nondeterministic'
        destination = Path(args.evidence); destination.parent.mkdir(parents=True, exist_ok=True)
        bundle = destination.parent / f'{TAG}-{commit}.bundle'
        run(['git', 'bundle', 'create', str(bundle.resolve()), TAG], origin, env)
        customer_bundle = destination.parent / f'customer-{customer_commit}.bundle'
        run(['git', 'bundle', 'create', str(customer_bundle.resolve()), 'HEAD'], customer, env)
        documents = {}
        for name, path in [('model', model), ('dynamics', dynamics), ('three_pump_model', third), ('three_pump_dynamics', third_dynamics), ('scenario', exercise)]:
            artifact = destination.parent / f'{commit}-{name}.json'
            shutil.copy2(path, artifact)
            documents[name] = {'file': artifact.name, 'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}
        output = {'status': 'candidate-release-proven; public release cut pending', 'tag': TAG, 'commit': commit,
            'model_sha256': hashlib.sha256(first).hexdigest(), 'equipment': len(model_json['equipment']), 'customer_checkout': 'clean independent Git clone; no platform workspace dependencies',
            'customer_commit': customer_commit, 'customer_lock_sha256': hashlib.sha256(lock.read_bytes()).hexdigest(),
            'engineering_binary_sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'candidate_bundle': bundle.name, 'candidate_bundle_sha256': hashlib.sha256(bundle.read_bytes()).hexdigest(),
            'customer_bundle': customer_bundle.name, 'customer_bundle_sha256': hashlib.sha256(customer_bundle.read_bytes()).hexdigest(),
            'candidate_origin': remote, 'documents': documents,
            'model_version': model_json['version'], 'added_pump_equipment': 3, 'added_pump_scenario': third_result, 'scenario': digests[0],
            'operator': json.loads((customer / 'ci/operator-evidence.json').read_text()),
            'tools': {name: hashlib.sha256((tools / name).read_bytes()).hexdigest() for name in ['dcs-model', 'dcs-controller', 'dcs-plant-server']}}
        destination.write_text(json.dumps(output, indent=2) + '\n')
        print(json.dumps(output, indent=2))

if __name__ == '__main__':
    main()
