# Packaging

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
