# Try the pump equipment library

This demonstration runs two independent pumps composed through the public
`dcs_build::pump` API. Each instance declares its equipment identity, control
points, protection status, motor feedback fault, and three managed alarms in
the same plant model the generic controller and monitor consume. The example
also composes a separate bearing-temperature warning for each pump from the
existing managed analog alarm block.

The simulated running contact follows the pump output through an explicit
field loopback in `crates/dcs-build/examples/pump.rs`. Thermal and moisture
contacts are healthy initially. The protective measurement is held at a
healthy value. There is no wet-well model or duty allocator in this small
example; an independent supervisory demand lets you try automatic control.
The model initializes the simulated bearing temperatures to 20 °C. These warnings
notify the operator and do not trip the pump.

## Start from this checkout

From a Linux terminal at the repository root, build the matching artifacts
through the shared build gate, then start the simulation:

```sh
python3 scripts/verify.py --phase rust-demo
python3 scripts/pump_demo.py run
```

Open [the operator UI](http://127.0.0.1:9080/). The launcher generates and checks
the model, starts the real `dcs-plant-server` and `dcs-controller`, scans every
200 ms, and serves the ordinary DCS monitoring page. Both services bind to
loopback by default. Pass `--no-browser` after `run` when starting from SSH or
a terminal without a browser.

On Windows, run those same commands inside WSL after changing to the checkout,
for example `cd /mnt/c/Users/Kaspar/workspace/dcs2/dcs`. Open the URL in the
Windows browser.

The launcher expects current `dcs-controller`, `dcs-plant-server`, and the
`pump` example under `target/debug`. It requires the equipment metadata from
this checkout and stops its children on Ctrl+C. Logs and the generated model
are under `target/pump-demo/<timestamp>/`. Pass `--artifacts target/release`
after `run` for matching release binaries. Full workspace verification remains
`python3 scripts/verify.py`.

## Home-network access

To serve the monitor on the Lenovo's home-network interface, use:

```sh
python3 scripts/pump_demo.py run --listen 192.168.178.107:9080 --no-browser
```

The Lenovo installation is available at
[dcs-demo.home.arpa](https://dcs-demo.home.arpa/) through the home lab's configured
DNS and Caddy route. DNS and HTTPS were checked from the Lenovo on 2026-10-07.
Other home-network devices can also open
[the monitor directly](http://192.168.178.107:9080/). The simulated plant service
remains on local port 9011; fault injection runs on the Lenovo. The new demo's
phone/Tailscale access has not been independently checked.

This installation runs as the Lenovo user service `dcs-pump-demo.service`.
Manage it there with:

```sh
systemctl --user status dcs-pump-demo.service --no-pager
systemctl --user stop dcs-pump-demo.service
systemctl --user restart dcs-pump-demo.service
```

Stop the service before starting another copy on the same ports. Restarting
starts a fresh simulation.

For this launch, add `--monitor http://192.168.178.107:9080` before each helper
action, for example:

```sh
python3 scripts/pump_demo.py --monitor http://192.168.178.107:9080 fault p101
```

The steps below use the default local launch and helper addresses.

## Try manual operation and protection

1. Open the plant overview from the left navigation and click the **Pump 1**
   symbol. Its equipment drawer contains the pump's controls, status, and alarms.
2. Select **Manual**, then **Run request**. After the configured protection-clear
   holdout, the command and running contact become true.
3. In another terminal, inject bad quality on the simulated thermal contact:

   ```sh
   python3 scripts/pump_demo.py fault p101
   ```

4. The protection state trips and the command stops. The contact quality and
   managed thermal alarm identify the cause. The top alarm strip opens the full
   alarms and events list; selecting a pump alarm opens its owning equipment's
   control drawer. Acknowledging the alarm changes its acknowledgment state;
   it does not clear the bad contact or stop a standing run request.
5. Select **Stop request**, then clear the simulated contact fault:

   ```sh
   python3 scripts/pump_demo.py recover p101
   ```

6. The protection recovers. Select **Run request** again to restart. Try
   **Out of service** while running: it inhibits the output in either mode.

The run request is held state. Clearing protection or returning a pump to
service can resume a standing demand after the applicable holdout. This slice
uses the existing automatic recovery policy, with no separate fault-reset
command. The operator surface exposes command request, actual running feedback,
and protection/fault state separately.

## Try warning priorities and alarm management

With Pump 1 running, raise its simulated bearing temperature to 80 °C:

```sh
python3 scripts/pump_demo.py warning p101 on
```

The P3 bearing-temperature alarm appears and the pump's symbol and drawer show
its owned alarm. The pump continues running. Open the alarm from the top strip
or the full alarm list to see its actual temperature, consequence, and required
response. Expand **Configured limits** to inspect the declared 60 °C high
limit and hysteresis. Select **Acknowledge** to record awareness; the standing
condition remains visible until the temperature recovers.

Use the selected warning's management actions to try **Shelve**, supplying a
reason such as `Inspecting the simulated bearing sensor`. The request requires
a reason and expires after 300 controller ticks, or 60 seconds at this demo's
scan rate. The warning remains listed with its managed state. Unshelve it to
resume annunciation sooner. Its alarm **Out of service** action is independent
of the pump's equipment service state: it marks only this warning out of
service for maintenance. Its standing condition and latch remain visible, and
return to service is explicit. The pump's thermal
and moisture protection alarms retain their non-shelvable policy.

Restore the temperature to 20 °C with:

```sh
python3 scripts/pump_demo.py warning p101 off
```

Substitute `p102` to try the other pump's independent warning. To see priority
ranking with both pumps, inject `fault p102`, then `fault p101`. The example
configures Pump 1's thermal alarm as P1 and Pump 2's as P2, ahead of the P3
warnings. The top strip presents up to four unacknowledged alarms, sorted by
ascending priority code. Returned alarms remain there until acknowledged;
active acknowledged alarms remain visible on their equipment's schematic badge.
Use `recover p101` and `recover p102` after stopping any held demand; acknowledge
the remaining latches from the alarm list.

Alarm policy is declared in Rust. `PumpConfig` exposes `fault_priority`,
`thermal_priority`, and `moisture_priority` as nonnegative site codes; its
unchanged defaults are 2, 2, and 3. For example:

```rust
let mut config = PumpConfig::new("p101", 1000, 5000);
config.thermal_priority = 1;
```

The example's `BEARING_WARNING_LIMIT`, `BEARING_WARNING_PRIORITY`, and
`BEARING_WARNING_MAX_SHELVE_TICKS` constants configure its process warnings.
Their rationalization, writable acknowledgment/shelving/maintenance points,
reason requirement, journaled lifecycle outputs, and equipment ownership are
declared alongside the analog alarm composition in
`crates/dcs-build/examples/pump.rs`. Change the code, rebuild, and restart the
demo to try a different policy. The monitor's `ALARM_PRESENTATION.maxUnacknowledged`
constant in `crates/dcs-monitor/src/page.html` configures two, three, or four
top-bar slots; four is the default. The browser provides operator controls.

## Configure the schematic in code

The operator UI provides the plant overview and individual pump areas in the
left navigation. Click a bound symbol to open its live controls, feedback,
protection status, and alarms. The top alarm strip opens the complete alarm
list and the equipment behind an alarm.

The plant composition declares the areas, reusable symbols, model bindings,
and pipe routes through `PlantBuilder::view`. See
`crates/dcs-build/examples/pump.rs` for the working configuration. The reusable
symbol set covers pumps, motors, valves, tanks, numeric measurements, and
labels. Their state and controls come from the same model as the controller.

For example, after composing the `p101` equipment:

```rust
let mut area = PlantView::new("pumps", "Pump area");
let mut symbol = PlantViewNode::new("pump-1", PlantViewSymbol::Pump, "Pump 1", 520, 300);
symbol.binding = Some(PlantViewBinding::Equipment("p101".into()));
area.nodes.push(symbol);
plant.view(area);
```

Change the view declarations in Rust, rebuild the example, and restart the
demo to try a different layout. Model validation rejects missing bindings,
invalid navigation trees, and unsupported geometry before the controller
starts. Unbound symbols remain drawing elements and show no process values.
The browser displays the configured plant and submits operator commands
through the existing validated command path.

## Try automatic demand

Select **Auto** for Pump 1, then submit its supervisory demand:

```sh
python3 scripts/pump_demo.py automatic p101 on
```

The helper resolves the model-declared signal and uses `POST /command`, prints
its admission receipt, then waits for its terminal settlement in `/journal`.
Set the demand to `off` to stop. Pump 2 can be controlled independently by
substituting `p102`. Use `status` to print the equipment summary from the actual
runtime's `/signals` and `/snapshot` surfaces.

## Stop or restart

Press Ctrl+C in the startup terminal to stop both simulated processes.

Each startup creates a fresh generated model, journal, and simulation. This demo
does not demonstrate retained plant state across restart or redundant takeover.
Those runtime capabilities need their separate acceptance runs.

The same actions can be submitted from a second terminal:

```sh
python3 scripts/pump_demo.py mode p101 manual
python3 scripts/pump_demo.py start p101
python3 scripts/pump_demo.py fault p101
python3 scripts/pump_demo.py stop p101
python3 scripts/pump_demo.py recover p101
python3 scripts/pump_demo.py status
```

## Docker alternative

With a Linux Docker engine running, start from the repository root:

```sh
docker compose -f compose.pump-demo.yml up --build
```

Open [the local operator UI](http://127.0.0.1:9080/). The container compiles the
current source and exposes both services on host loopback. Run helpers inside
the container with its internal addresses:

```sh
docker compose -f compose.pump-demo.yml exec pump-demo python3 /app/scripts/pump_demo.py --monitor http://127.0.0.1:8080 --plant 127.0.0.1:9001 fault p101
```

Substitute the other helper actions from above. Stop with Ctrl+C, then run
`docker compose -f compose.pump-demo.yml down` to remove the stopped container.

The example source shows the supported engineering seam: a consumer owns device
and logical-I/O bindings, calls `pump(...)` once per instance, and emits the
versioned model. The control module creates its owned blocks, managed alarms,
and equipment surface without requiring a pump-specific runtime or UI server.
