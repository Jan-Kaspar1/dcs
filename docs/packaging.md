# Packaging

This file documents the platform's packaging artifacts: the generic
container images and the checked-in demonstration rig. What a customer
plant pins — the release set, pin mechanisms, version scheme, and
compatibility policy — is the consumer release contract in
`docs/release-contract.md`. The checked-in fixtures these pages
reference are platform conformance tests owned by this repository,
not customer-project examples; a customer plant is an external consumer
of a pinned release (decision 79).

## Controller container image

The root `Dockerfile` packages the `dcs-controller` binary as a container
image. A `rust:1.98.1-bookworm` build stage runs the workspace release
build — `cargo build --release --locked -p dcs-controller`, under the same
pinned toolchain `rust-toolchain.toml` declares — and a
`debian:bookworm-slim` runtime stage carries only the resulting binary,
executed as a non-root user. `.dockerignore` keeps `target/` and local
agent state out of the build context. This is a checked-in packaging
artifact only; it defines no deployment or orchestration.

### Build

From the repository root:

```sh
docker build -t dcs-controller .
```

### Run

The image's entrypoint is the controller binary, whose first argument is
the plant model path *inside* the container. Mount the model file
read-only and pass its container path:

```sh
docker run --rm \
    -v "$PWD/crates/dcs-demo/fixtures/tank_level.json:/model/plant.json:ro" \
    dcs-controller /model/plant.json --scan-ms 100
```

All remaining arguments are the binary's own (`--ticks N` bounds the run
deterministically and prints the final telemetry snapshot; see
`docker run --rm dcs-controller --help`).

### Redundant pair

A redundant pair is two of these containers on separate hosts, both built
from the same image and mounting the same plant model document — the model
is the shared contract, and each container is an independent instance of
the same controller. Hot-swap means starting or replacing the standby
container while the active keeps scanning; peer management and takeover
semantics are recorded in `docs/architecture.md` (decisions 9, 10, and
11–15) and are not part of this image.

## Plant container image

`Dockerfile.plant` packages the `dcs-plant-server` binary — the shared
simulated plant of `dcs-sim-net` — the same way: a
`rust:1.98.1-bookworm` build stage runs
`cargo build --release --locked -p dcs-plant`, and a
`debian:bookworm-slim` runtime stage carries only the resulting binary,
executed as the same non-root user. A second Dockerfile, rather than a
build-arg-parameterized shared one, keeps each image a self-contained,
statically inspectable artifact; the two files are deliberately kept in
lockstep. Together with the controller image it completes the
demonstration rig — one plant container plus a redundant controller
pair — as checked-in packaging artifacts only; it defines no deployment
or orchestration.

### Build

From the repository root:

```sh
docker build -f Dockerfile.plant -t dcs-plant-server .
```

### Run

The image's entrypoint is the plant server binary. Mount the plant model
and — when the plant carries process physics — a dynamics document
read-only, and pass their container paths plus `--listen`. The listener
must bind an address reachable from outside the container
(`0.0.0.0`, not loopback):

```sh
docker network create dcs-rig
docker run --rm -d --name dcs-plant --network dcs-rig \
    -v "$PWD/crates/dcs-plant/fixtures/tank_loop.json:/model/plant.json:ro" \
    -v "$PWD/crates/dcs-plant/fixtures/tank_loop_dynamics.json:/model/dynamics.json:ro" \
    dcs-plant-server /model/plant.json \
        --dynamics /model/dynamics.json --listen 0.0.0.0:9001
```

`--dynamics` is optional; drop the flag and its mount for a plant whose
field values are only what attachments write. The bound address is
reported on stderr (`listening on …`); with `--name` on a user-defined
network, other containers reach the listener at `dcs-plant:9001`. For
clients on the Docker host — or for a rig spread across machines —
publish the port instead (`-p 9001:9001` replaces `--network`).

### Attaching the controller pair

Both controller containers attach to the plant's listener through
`--remote ADDR` — the field-observing driver mode — mounting the same
plant model the plant serves, since the model is the shared contract
on both sides of the protocol:

```sh
docker run --rm -d --name ctrl-a --network dcs-rig -p 8080:8080 \
    -v "$PWD/crates/dcs-plant/fixtures/tank_loop.json:/model/plant.json:ro" \
    dcs-controller /model/plant.json --remote dcs-plant:9001 \
        --scan-ms 100 --listen 0.0.0.0:8080
docker run --rm -d --name ctrl-b --network dcs-rig -p 8081:8081 \
    -v "$PWD/crates/dcs-plant/fixtures/tank_loop.json:/model/plant.json:ro" \
    dcs-controller /model/plant.json --remote dcs-plant:9001 \
        --standby ctrl-a:8080 --scan-ms 100 --listen 0.0.0.0:8081
```

The active (`ctrl-a`) owns the field: it steps the plant and its writes
pass. The standby (`ctrl-b`) pulls a checkpoint per scan from the
active's monitoring address (`--standby ctrl-a:8080`), scans
output-quiesced behind its write gate, and promotes through
`POST /promote` on its own monitor — or self-promotes with
`--auto-promote N`. In a cross-host rig the same commands hold with the
plant's published `host:port` in place of `dcs-plant:9001` and the
active's published monitoring address in place of `ctrl-a:8080`.
Alternatively the model can declare `sim-tcp` devices whose
`parameters.address` names the plant listener; the registry then builds
the attachment itself and `--remote` is not needed — the form the
hot-swap test (`crates/dcs-controller/tests/hot_swap.rs`) exercises.

## The demonstration rig

`compose.yaml` at the repository root is the checked-in rig definition
— the same topology the commands above describe, declared as data:
one `dcs-plant` service mounting the checked-in reference-station
artifacts — `crates/dcs-demo/fixtures/pump_station.json` and
`crates/dcs-demo/fixtures/pump_station_dynamics.json`, the model and
dynamics documents `dcs-build`'s `pump_station` example emits — the
`ctrl-a`/`ctrl-b` redundant pair mounting the same station model and
attaching to its listener with the standby wired to the active's
monitor, and the pair's monitor ports published on the host. The
plant service's healthcheck orders the controllers' one-shot
`--remote` attach behind the listener actually serving. The file is a
statically inspectable declaration — `docker compose config` checks
it — and like the Dockerfiles it is a checked-in packaging artifact:
a single-host orchestration declaration that defines no deployment.
It is also a platform conformance artifact: the reference-station
fixtures it mounts are owned by this repository as test evidence, not
as a template for a customer project — the customer-shaped path is an
independent plant repository pinning a release per
`docs/release-contract.md`.

What the running rig demonstrates is the reference duty/standby
pumping station under the redundant pair: the dynamics document drives
the wet well — the declared inflow against both pump draws through
the level integrator and lag — while the active controller runs the
station: failover-select over the primary and backup level
measurements feeding the threshold chain, the pump group staging the
duty and lag pumps on its computed demand, and the full alarm set
(high and low level, per-pump motor-fault, thermal, and moisture,
backup-active, none-available, all-faulted, and power-fail) latching
until acknowledged. Every alarm `ack` point and each pump's manual
takeover — the writable `mode`, `hand`, and `oos` points — are
model-declared writable, so they are commanded through the published
monitor ports the same way the documented promotion is.

### Build

`docker compose build` runs both Dockerfile builds and tags them
`dcs-controller` and `dcs-plant-server` — the same images the
individual `docker build` commands above produce:

```sh
docker compose build
```

### Run

```sh
docker compose up -d
```

`docker compose ps` shows the three services; `docker compose logs -f
ctrl-b` follows the standby's tracking. The monitoring page presents
the pair as one logical controller — open either peer's published
monitor port and pass the other as `?peer=`:

```
http://localhost:8080/?peer=localhost:8081
```

The page polls `GET /role` on both peers, renders the settled-active
peer's telemetry plus per-peer pair health, and submits commands only
to the peer reporting `active`.

### Demonstrating a promotion

Wait for the standby to converge — `GET /role` on its published port
reports `standby` with a `tracking` convergence:

```sh
curl -s http://localhost:8081/role
```

Then switch over in the documented order — demote the field-owning
peer first, then promote the converged standby; each `POST` answers
with the peer's post-change `RoleReport`:

```sh
curl -s -X POST http://localhost:8080/demote
curl -s -X POST http://localhost:8081/promote
```

The pair view keeps serving the one logical controller — its data now
sources from `ctrl-b`, and `ctrl-a` reports `standby`. `dcs-ctl`
(`cargo run -p dcs-monitor --bin dcs-ctl -- <addr> role|demote|promote`)
runs the same contract from the host against the published ports.

### Teardown

```sh
docker compose down
```

removes the containers and the `dcs-rig` network; the images remain
for the next `up`.

### The two-machine form

The rig is a single-host declaration. The two-machine demonstration
the vision describes is the same services spread across hosts with
the published-address variant the run commands above record: the plant
publishes `9001` on its host, each controller runs on its own machine
with `--remote <plant-host>:9001`, and the standby's `--standby` names
the active's reachable monitor address — the network names
`dcs-plant:9001` and `ctrl-a:8080` replaced by the hosts' published
`host:port`s. An actual second machine stays outside the workflow, per
the no-live-deployment bound.
