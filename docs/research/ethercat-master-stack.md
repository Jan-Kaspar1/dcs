# Userspace EtherCAT master stack for the DCS controller

- **Checked:** 2026-09-15
- **Question:** Which EtherCAT master stack can plausibly run inside the delivered
  `dcs-controller` on the Lenovo QA host, beneath the point-wise `IoDriver`
  contract, on a dedicated USB Ethernet NIC — and what does the choice require of
  the cyclic I/O contract, packaging, and host capabilities?
- **Affected requirements:** enabling work for `WW-FND-002` (hardware-independent
  control — the third driver kind after `sim-tcp`/`sim-bus`, and the first real
  fieldbus) and `WW-OPS-003` (signal and communication confidence — exchange
  freshness and link health surface through it). Part of the Lenovo hardware-QA
  lane in `docs/lenovo-hardware-qa-plan.md` ("Test subject: EtherCAT inside DCS").

## Primary sources

1. `ethercrab` crate, crates.io — <https://crates.io/crates/ethercrab> — version
   0.7.1 published 2026-03-23; license MIT OR Apache-2.0; ~48.6k total downloads,
   ~5k in the last 90 days; MSRV 1.85 for 0.7.x.
2. EtherCrab API documentation, docs.rs — <https://docs.rs/ethercrab/0.7.1/ethercrab/> —
   `std::tx_rx_task(interface, pdu_tx, pdu_rx)`, `std::tx_rx_task_io_uring`,
   `MainDevice::init`/`init_single_group`, `SubDeviceGroup::into_safe_op`/`into_op`,
   cyclic `tx_rx_*` methods returning `TxRxResponse`.
3. EtherCrab source, `std/unix` TX/RX implementation — <https://docs.rs/ethercrab/latest/src/ethercrab/std/unix/mod.rs.html> —
   `tx_rx_task` opens a `RawSocketDesc` bound to the interface name (Linux
   AF_PACKET raw socket), then runs as an async future driving the PDU loop.
4. EtherCrab CHANGELOG — <https://github.com/ethercrab-rs/ethercrab/blob/main/CHANGELOG.md> —
   release cadence and breaking-change history (0.7.0: MSRV 1.85, `xdp` AF_XDP
   feature on Linux, `io-uring` made optional in 0.7.1, `UnknownSubDevice`
   grouping filter, per-SubDevice state reads on `tx_rx_*`).
5. `ethercat` crate (ethercat-rs), crates.io/GitHub — <https://crates.io/crates/ethercat>,
   <https://github.com/ethercat-rs/ethercat> — version 0.3.x; Rust wrapper over
   the IgH EtherLab master; requires the IgH kernel modules and a matching
   master checkout (`ETHERCAT_PATH`) or pregenerated bindings pinned to one IgH
   revision.
6. IgH EtherCAT Master for Linux, EtherLab — <https://etherlab.org/en/ethercat/> —
   kernel-module master stack (`ec_master` plus NIC driver binding); not a
   userspace library.
7. SOEM (Simple Open EtherCAT Master), OpenEtherCATSociety —
   <https://github.com/OpenEtherCATsociety/soem> — C userspace master library
   (GPLv2 with a linking/embedded exception clause); would enter the workspace
   only through FFI.
8. Wago 750-354 EtherCAT fieldbus coupler data sheet and manual —
   <https://www.wago.com/global/i-o-systems/fieldbus-coupler-ethercat/p/750-354> —
   100 Mbit/s, 2x RJ-45 (upper to master, lower to downstream devices), up to 64
   I/O modules per node, fieldbus process image max 1024 bytes each direction,
   digital module data mapped bit-by-bit, and documented process-data watchdog
   behavior on cyclic-exchange interruption (manual sections on controlling the
   process images and on interruption of the cyclical process data exchange).
9. Wago 750-400/750-501 module data sheets — <https://www.wago.com/750-400>,
   <https://www.wago.com/750-501> — 2-bit input/output process data width each;
   electrical data carried in the rig manifest.

## Supported findings

### EtherCrab fits the architectural seam

- It is a pure-Rust, async-first master. The application owns pacing: after
  `init` reaches PRE-OP and the group is taken through SAFE-OP into OP, each
  cyclic `tx_rx_*` call performs one process-data exchange and returns a
  `TxRxResponse` carrying the working-counter result and per-SubDevice state —
  exactly the "one exchange per controller scan" shape the cyclic I/O contract
  needs [2, 4].
- Identity verification is a first-class seam: the `init` group filter can
  reject an unrecognised SubDevice with `Error::UnknownSubDevice`, and
  SubDevice EEPROM vendor/product/revision data plus the configured PDO layout
  are readable during PRE-OP — so "unknown identity or incompatible layout
  fails startup before outputs are enabled" maps onto ordinary init code, not
  custom machinery [2, 4].
- License (MIT OR Apache-2.0) is compatible with this proprietary workspace;
  MSRV 1.85 sits below the repo toolchain (1.98.1, edition 2024 — 0.7.x already
  uses edition 2024 itself) [1, 4].
- Maintenance looks healthy for a niche crate: 0.7.0/0.7.1 in March 2026 after a
  steady 0.4–0.6 series through 2024–2025, an active PR queue, and a fully
  documented public API. It is pre-1.0 — breaking changes between minor
  versions are routine (the changelog shows several per release), so the
  dependency should be pinned exactly and upgraded deliberately [1, 4].
- Async-first is the main integration cost: the DCS executor is deliberately
  synchronous and tick-deterministic (decision 4). The natural boundary is a
  dedicated driver thread running the TX/RX future (a small async-io-scale
  runtime; no tokio requirement) while the executor-facing `IoDriver` impl
  serves the latched image synchronously. This keeps all asynchrony beneath the
  driver seam [2, 3].

### Transport needs on this host (verified against the Lenovo, 2026-09-15)

- `std::tx_rx_task` on Linux opens an AF_PACKET raw socket on the named
  interface — the process needs `CAP_NET_RAW`; in a container that means
  `cap_add: NET_RAW` plus a network topology giving the container an L2
  presence on the field segment (a macvlan child on `enx00e04c751f7c`, the
  physical interface moved into the container netns, or host networking). The
  kernel has `macvlan.ko` available; whether the RTL8153 USB NIC passes macvlan
  traffic cleanly is a *measured* question the isolation plan already names —
  it must be validated before the topology is committed, not assumed [3, 8;
  host recon recorded in `docs/wago-ethercat-rig-manifest.md`].
- Timing: the host runs a PREEMPT_DYNAMIC kernel booted in "lazy" mode with no
  `isolcpus` and the `powersave` CPU governor — there is no hard real-time
  scheduling available. EtherCrab's `xdp`/io_uring transports reduce per-frame
  overhead but cannot fix scheduler jitter. Realistic initial exchange periods
  are in the tens-of-milliseconds range; sub-millisecond cycle claims do not
  apply here. The Wago 750-354's process-data watchdog behavior on missed
  cycles must be measured on the real rig before acceptance bounds are written
  [8; host recon, `docs/wago-ethercat-rig-manifest.md`].
- USB NICs add per-frame latency versus PCIe (USB scheduling and adapter
  buffering; the r8152 driver aggregates interrupts). At QA-rig cycle times
  (tens of ms) this is noise; it rules out claiming hard cycle deadlines, not
  feasibility [3; host recon].

### Alternatives

- **IgH EtherLab via the `ethercat` crate** — rules itself out twice: the
  binding targets IgH stable-1.5/1.6 and needs the IgH kernel modules plus a
  matching master checkout at build time, and recon confirms the Lenovo has no
  `ec_*` modules, no `ethercat` CLI, and no EtherLab install. Adding a kernel
  module and NIC-binding reconfiguration would also conflict with the
  read-only/NIC boundaries on the shared homelab host. Not available; do not
  plan on it [5, 6].
- **SOEM via FFI** — a mature C master used widely in industry; it would mean a
  C toolchain dependency, an unsafe FFI boundary, and GPLv2-with-exception
  terms to review against this proprietary workspace. A plausible fallback if
  EtherCrab fails the rig experiment; strictly worse on integration cost [7].
- **Other Rust masters** — none with comparable maintenance or a usable cyclic
  API surfaced in this evaluation.

## Proposed DCS implications

- **Recommendation: adopt EtherCrab as the first candidate stack**, pinned to
  an exact 0.7.x version, behind a new `dcs-ethercat` crate that exposes only
  the point-wise `IoDriver` surface plus the cyclic-exchange hook the proposed
  decision adds. Selection is *recommended, not final*: the plan requires the
  stack to be proven on the actual rig, and the validating evidence is the
  supervised commissioning run — discovery of the 750-354 station, a
  process-data layout match, and a measured exchange cycle.
- The cyclic I/O contract (proposed as a new `docs/architecture.md` decision on
  this branch; final numbering lands at merge time) is stack-agnostic: one
  image exchange per scan, latched inputs, staged outputs, acquisition-tick
  freshness, link and exchange diagnostics. EtherCrab's `tx_rx_*` and
  `TxRxResponse` map onto it directly.
- Driver threading: one background thread per logical bus owns the async TX/RX
  pump and the periodic exchange; the `IoDriver` impl reads/writes the latch
  synchronously. No async runtime enters `dcs-runtime`.
- Container topology decision is deferred to measured validation: a raw socket
  in the controller container with `NET_RAW` over a macvlan child is the
  preferred shape (keeps the host's interface configuration untouched);
  physical-interface passthrough is the fallback; host networking is rejected
  because it forfeits the QA lane's isolation.
- Recovery policy stays application-owned (EtherCrab deliberately leaves
  re-init to the application): exchange failure reports through the link
  diagnostics, the held input image ages through the declared freshness budget,
  and reads escalate to `Disconnected` after the declared miss threshold;
  recovery re-enters the PRE-OP/SAFE-OP/OP transitions at the exchange
  boundary, never silently mid-scan.

## Assumptions and open questions requiring validation

- Actual station composition, module revisions, terminal order, and wiring are
  unverified — see the rig manifest's unknowns table. The 750-354's PDO layout
  is auto-configured from the physical terminal order; the manifest's expected
  layout is provisional until the master's own discovery reads it.
- Whether the RTL8153 USB adapter passes macvlan-originated EtherCAT frames
  (ethertype 0x88A4, non-IP) correctly — USB NICs can filter non-IP or
  promiscuous traffic; this must be measured.
- Achievable exchange period and jitter on this kernel/governor under homelab
  load — to be measured during commissioning; the manifest's timing bounds are
  placeholders pending that measurement and the Wago manual's watchdog data.
- Distributed Clocks: not needed for two digital modules at QA cycle times;
  revisit if a later rig adds motion or synchronized sampling.
- Whether `xdp` (AF_XDP) works through the r8152 driver — optional optimization
  only; the plain raw-socket path is the baseline.
