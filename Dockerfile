# syntax=docker/dockerfile:1

# Build stage: compile the controller with the workspace-pinned toolchain.
FROM rust:1.98.1-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p dcs-controller \
    && cargo build --release --locked -p dcs-monitor --bin dcs-forge --bin dcs-ctl

# Runtime stage: minimal image carrying the controller binary plus
# dcs-forge — the QA lane's bridge-placed checkpoint endpoint, launched
# with --entrypoint dcs-forge — and dcs-ctl, the operator CLI the
# declared health check probes the monitor through.
FROM debian:bookworm-slim AS runtime
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 dcs
COPY --from=build /src/target/release/dcs-controller /usr/local/bin/dcs-controller
COPY --from=build /src/target/release/dcs-forge /usr/local/bin/dcs-forge
COPY --from=build /src/target/release/dcs-ctl /usr/local/bin/dcs-ctl
USER dcs
# The health contract: the probe asks the monitor's own bounded
# liveness answer — GET /health on the heartbeat lane — through the
# shipped operator CLI, so the container reports healthy only once the
# listener actually serves (a still-binding or restarted container
# reads unhealthy until first service, never on a fixed sleep).
# DCS_MONITOR_ADDR overrides the probe's loopback target when --listen
# uses a port other than the documented 8080.
HEALTHCHECK --interval=2s --timeout=3s --start-period=10s --retries=15 \
    CMD dcs-ctl "${DCS_MONITOR_ADDR:-127.0.0.1:8080}" health
ENTRYPOINT ["dcs-controller"]
CMD ["--help"]
