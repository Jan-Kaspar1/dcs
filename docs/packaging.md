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
