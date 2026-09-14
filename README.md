# DCS

A process control platform built around a shared plant model, reusable control components, and hardware-independent controllers. The original project vision and worker instructions live in [AGENTS.md](AGENTS.md).

## Development

Use Ubuntu WSL, Python 3, Git, and rustup. The repository pins its Rust toolchain. Run all local checks with:

```sh
python3 scripts/verify.py
```

The verification command shares four build slots across worker clones, limits Cargo to four build threads, and keeps build artifacts in each clone. CI independently checks Rust formatting, Clippy, Rust tests, and Python supervisor tests.

## Packaging

The root `Dockerfile` builds the `dcs-controller` binary into a minimal container image, and `Dockerfile.plant` does the same for `dcs-plant-server` — one plant container plus a redundant controller pair is the demonstration rig. See [docs/packaging.md](docs/packaging.md) for the image build and run commands.

## Local agent pool

The supervisor coordinates local Devin CLI sessions using `swe-2-high`, independent clones, SQLite reservations, and GitHub issues and PRs. Workers implement software against simulated I/O. CI success gates supervisor merges; this local policy cannot prevent unrelated direct pushes.

The pool starts with five workers, increases to ten after five verified automated merges, and to twenty after fifteen. A separate planner maintains independent ready work and records its decisions in [architecture](docs/architecture.md) and the [rolling plan](docs/plan.md).

Runtime configuration, logs, SQLite state, credentials, and generated prompts belong outside Git. A pinned supervisor installation changes only through an explicit upgrade. The personal DCS checkout remains separate from worker clones.

See the [operations runbook](docs/agent-operations.md) for installation, startup, controls, recovery, upgrades, and rollout acceptance evidence.
