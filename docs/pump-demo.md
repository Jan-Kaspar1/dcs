# Try the pump equipment library

This demonstration runs two independent pumps composed through the public
`dcs_build::pump` API. Each instance declares its equipment identity, control
points, protection status, motor feedback fault, and three managed alarms in
the same plant model the generic controller and monitor consume.

The simulated running contact follows the pump output through an explicit
field loopback in `crates/dcs-build/examples/pump.rs`. Thermal and moisture
contacts are healthy initially. The protective measurement is held at a
healthy value. There is no wet-well model or duty allocator in this small
example; an independent supervisory demand lets you try automatic control.

## Start from this checkout

Start Docker Desktop with its Linux engine, then run this command from the
repository root:

```sh
docker compose -f compose.pump-demo.yml up --build
```

The first build downloads the base images and compiles the current source.
Open [the operator UI](http://127.0.0.1:9080/). The container starts the real
`dcs-plant-server` and `dcs-controller`, scans every 200 ms, and serves the
ordinary DCS monitoring page. Both exposed ports bind to host loopback.

## Try manual operation and protection

1. Select **Equipment**, then **Pump 1**.
2. Select **Manual**, then **Run request**. After the configured protection-clear
   holdout, the command and running contact become true.
3. In another terminal, inject bad quality on the simulated thermal contact:

   ```sh
   docker compose -f compose.pump-demo.yml exec pump-demo python3 /app/scripts/pump_demo.py --monitor http://127.0.0.1:8080 --plant 127.0.0.1:9001 fault p101
   ```

4. The protection state trips and the command stops. The contact quality and
   managed thermal alarm identify the cause. Acknowledging the alarm changes
   its acknowledgment state; it does not clear the bad contact or stop a
   standing run request.
5. Select **Stop request**, then clear the simulated contact fault:

   ```sh
   docker compose -f compose.pump-demo.yml exec pump-demo python3 /app/scripts/pump_demo.py --monitor http://127.0.0.1:8080 --plant 127.0.0.1:9001 recover p101
   ```

6. The protection recovers. Select **Run request** again to restart. Try
   **Out of service** while running: it inhibits the output in either mode.

The run request is held state. Clearing protection or returning a pump to
service can resume a standing demand after the applicable holdout. This slice
uses the existing automatic recovery policy, with no separate fault-reset
command. The operator surface exposes command request, actual running feedback,
and protection/fault state separately.

## Try automatic demand

Select **Auto** for Pump 1, then submit its supervisory demand:

```sh
docker compose -f compose.pump-demo.yml exec pump-demo python3 /app/scripts/pump_demo.py --monitor http://127.0.0.1:8080 automatic p101 on
```

The helper resolves the model-declared signal and uses `POST /command`, prints
its admission receipt, then waits for its terminal settlement in `/journal`.
Set the demand to `off` to stop. Pump 2 can be controlled independently by
substituting `p102`. Use `status` to print the equipment summary from the actual
runtime's `/signals` and `/snapshot` surfaces.

## Stop or restart

Press Ctrl+C in the startup terminal, then remove the stopped container:

```sh
docker compose -f compose.pump-demo.yml down
```

Each startup creates a fresh generated model, journal, and simulation. This demo
does not demonstrate retained plant state across restart or redundant takeover.
Those runtime capabilities need their separate acceptance runs.

## Native run with matching compiled artifacts

Build matching native artifacts through the shared build gate, then start:

```sh
python3 scripts/verify.py --phase rust-demo
python3 scripts/pump_demo.py run
```

The launcher expects current `dcs-controller`, `dcs-plant-server`, and the
`pump` example under `target/debug` (`.exe` on Windows). It validates the emitted
model, requires the new equipment metadata, starts both processes, opens the UI,
and stops its children on Ctrl+C. Logs and the generated model are under
`target/pump-demo/<timestamp>/`. Pass `--artifacts target/release` for release
binaries. Heavy workspace verification remains `python3 scripts/verify.py`.

With the native run, the helper's default addresses already match:

```sh
python3 scripts/pump_demo.py mode p101 manual
python3 scripts/pump_demo.py start p101
python3 scripts/pump_demo.py fault p101
python3 scripts/pump_demo.py stop p101
python3 scripts/pump_demo.py recover p101
python3 scripts/pump_demo.py status
```

The example source shows the supported engineering seam: a consumer owns device
and logical-I/O bindings, calls `pump(...)` once per instance, and emits the
versioned model. The control module creates its owned blocks, managed alarms,
and equipment surface without requiring a pump-specific runtime or UI server.
