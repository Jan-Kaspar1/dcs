# syntax=docker/dockerfile:1

# Build stage: compile the controller with the workspace-pinned toolchain.
FROM rust:1.98.1-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p dcs-controller \
    && cargo build --release --locked -p dcs-monitor --bin dcs-forge

# Runtime stage: minimal image carrying the controller binary plus
# dcs-forge — the QA lane's bridge-placed checkpoint endpoint, launched
# with --entrypoint dcs-forge.
FROM debian:bookworm-slim AS runtime
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 dcs
COPY --from=build /src/target/release/dcs-controller /usr/local/bin/dcs-controller
COPY --from=build /src/target/release/dcs-forge /usr/local/bin/dcs-forge
USER dcs
ENTRYPOINT ["dcs-controller"]
CMD ["--help"]
