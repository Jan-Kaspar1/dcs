# Connected water-area consumer

`src/bin/connected-water.rs` is customer-owned Rust engineering. It composes
water transport, storage, duty/standby allocation, isolation, draining PI control,
measurements and managed alarms through the DCS release API. Changing
`site.station.pumps` from two to three (or running `--add-pump`) obtains another
pump's standard control, diagnostics, alarms, panel and diagram. No customer
JavaScript or separate controller program is needed.

The shipped `v0.10.0` pin is still pending and predates this API. The new binary
is feature-gated so the existing plant remains buildable against its old lock.
Before using the commands below, repin **both Cargo.toml and Cargo.lock** to the
reviewed connected-control release. Generic runtime/tooling must come from that
same immutable release. Do not substitute workspace binaries while claiming
an independent release proof.

After that repin, from this customer repository:

```sh
cargo build --locked --features connected-water --bin connected-water
mkdir -p model state
target/debug/connected-water > model/connected-water.json
target/debug/connected-water --dynamics > model/connected-water-dynamics.json
dcs-model validate model/connected-water.json
dcs-controller model/connected-water.json --check
```

Start the simulated field service, then the generic controller in another
terminal. These commands use local simulated I/O:

```sh
dcs-plant-server model/connected-water.json --dynamics model/connected-water-dynamics.json --listen 127.0.0.1:9012
```

```sh
dcs-controller model/connected-water.json --remote 127.0.0.1:9012 --listen 127.0.0.1:9081 --scan-ms 200 --dt 0.2 --state-file state/controller.json --journal-file state/journal.jsonl --history-file state/history.jsonl
```

Open `http://127.0.0.1:9081/`. Select LIC-201 to change the setpoint, XV-201 to
close/open the discharge, and LV-201 for manual/automatic operation. Manual 30%
settles near 43.2 m3/h. Protection may block a stored request; applied receipt
means the request was stored, while applied output and actual position show the
resulting process behavior. Closing isolation increases balance storage and
raises a warning. The FT-201 and LT-201 readings open standard measurement panels.
Use member alarm controls for acknowledgment and reasoned, bounded shelving.

Run the operator, consumer-isolation, restart and compatible takeover acceptance
against those installed artifacts (it launches its own isolated driven rigs):

```sh
python3 ci/connected_water.py --model model/connected-water.json --dynamics model/connected-water-dynamics.json --controller "$(command -v dcs-controller)" --plant-server "$(command -v dcs-plant-server)" --extended --evidence operator-evidence.json
```

All numeric process assumptions and owner-policy limitations are recorded with
the release's connected-water milestone documentation. The vessels are fast
training storage, pumps are constant-flow and isolation mechanics instantaneous;
this does not establish real hydraulic design, closed-limit proof or commissioning.
For production deployment, pin image digests and define setting/state activation
and recovery policy. An infrastructure manifest is not a plant activation
transaction. The candidate has no implemented DCS plan/apply command.
