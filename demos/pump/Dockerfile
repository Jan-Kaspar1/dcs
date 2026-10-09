# syntax=docker/dockerfile:1
FROM rust:1.98.1-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN python3 scripts/verify.py --phase rust-demo

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --no-create-home --shell /usr/sbin/nologin --uid 10001 dcs
COPY --from=build /src/target/debug/dcs-controller /usr/local/bin/dcs-controller
COPY --from=build /src/target/debug/dcs-plant-server /usr/local/bin/dcs-plant-server
COPY --from=build /src/target/debug/examples/pump /usr/local/bin/pump
COPY scripts/pump_demo.py /app/scripts/pump_demo.py
USER dcs
ENTRYPOINT ["python3", "/app/scripts/pump_demo.py"]
CMD ["run", "--artifacts", "/usr/local/bin", "--run-dir", "/tmp/pump-demo", "--listen", "0.0.0.0:8080", "--plant-listen", "0.0.0.0:9001", "--no-browser"]
