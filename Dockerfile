# syntax=docker/dockerfile:1

# Build stage: compile the controller with the workspace-pinned toolchain.
FROM rust:1.98.1-bookworm AS build
WORKDIR /src
COPY . .
RUN cargo build --release --locked -p dcs-controller

# Runtime stage: minimal image carrying only the controller binary.
FROM debian:bookworm-slim AS runtime
RUN useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 dcs
COPY --from=build /src/target/release/dcs-controller /usr/local/bin/dcs-controller
USER dcs
ENTRYPOINT ["dcs-controller"]
CMD ["--help"]
