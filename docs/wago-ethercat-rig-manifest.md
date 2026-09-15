# Wago EtherCAT rig manifest

Sanitized description of the Lenovo QA lane's physical rig: what is verified,
what is only datasheet-derived, and what remains unknown until supervised
commissioning. The plan (`docs/lenovo-hardware-qa-plan.md`, "Rig contract and
physical acceptance") makes this document the rig contract: physical wiring is
a commissioning prerequisite, never an agent guess.

**Status convention.** Every field carries one of:

- `verified` — confirmed by direct observation (remote recon on 2026-09-15, or
  a recorded physical check). The evidence column names how.
- `datasheet` — taken from vendor documentation; the part number it assumes is
  itself unconfirmed on the physical rig.
- `unverified` — expected from the plan's photograph or reasoning, not yet
  confirmed. **Renders as "needs attention" on the QA dashboard.**
- `unknown` — no basis yet. **Renders as "needs attention".**

Nothing in this document authorizes driving the rig. Physical output
commissioning is a supervised later step.

## Host (Lenovo ThinkCentre M70q, 192.168.178.107)

| Field | Value | Status | Evidence |
| --- | --- | --- | --- |
| Host | `jan-kaspar-ThinkCentre-M70q`, Ubuntu 26.04.1 LTS | verified | `hostname`, `os-release` over SSH 2026-09-15 |
| Kernel | 7.0.0-31-generic, `PREEMPT_DYNAMIC`, dynamic mode **lazy** | verified | `uname`, `dmesg` "Dynamic Preempt: lazy" |
| Real-time capability | No PREEMPT_RT, no isolcpus, no_rt changes; governor `powersave` on all 12 threads; intel_pstate active | verified | `/proc/cmdline`, cpufreq, 2026-09-15 |
| CPU | Intel i5-10500T, 12 threads | verified | `/proc/cpuinfo` |
| RAM / root storage | 16 GB (homelab docs); 468 GB NVMe root, ~10% used | verified (RAM: homelab docs) | `df`, `docs/storage.md` |
| Docker | 29.1.3, overlayfs, systemd cgroup driver | verified | `docker info` |
| Docker-HDD dependency | Docker requires `/srv/homelab-data` (8 TB USB HDD, `/dev/sda1`, mounted) via `docker.service.d/homelab-storage.conf` | verified | mount output + `config/lenovo/docker/homelab-storage.conf` |
| EtherCAT tooling | No IgH modules, no `ethercat` CLI, no EtherLab install | verified | `lsmod`, `which`, 2026-09-15 |
| Raw-socket capability | `CAP_NET_RAW` available in the bounding set; `tcpdump` present; capture requires privilege | verified | `capsh`, passive capture attempt (permission-denied as unprivileged user) |
| macvlan feasibility | `macvlan.ko` present for the running kernel; not yet exercised | verified present / unverified functional | `modinfo macvlan` |
| CPU isolation options | None configured. Candidates for later: `preempt=full` boot param, `isolcpus`, `performance` governor, IRQ affinity — all require a deliberate, supervised host change | unknown effectiveness | `/proc/cmdline` shows none today |

### Dedicated field NIC `enx00e04c751f7c`

| Field | Value | Status | Evidence |
| --- | --- | --- | --- |
| Interface identity | `enx00e04c751f7c`, MAC `00:e0:4c:75:1f:7c` | verified | `ip link` |
| Adapter | Realtek RTL8153 USB 3.0 Gigabit adapter (USB `0bda:8153`), driver `r8152`, firmware `rtl8153b-2 v2` | verified | `ethtool -i`, `lsusb` |
| USB topology | Bus 002 port 6, xHCI, negotiated **5000M (SuperSpeed)** — shares the bus root hub with the WD HDD (5000M, port 1); no intervening hub | verified | `lsusb -t` |
| Link state | UP, link detected, **100 Mb/s full duplex**, auto-negotiation on | verified | `ethtool` |
| Link partner advertisement | Only 10baseT and 100baseT modes — a 10/100-only partner, consistent with a 750-354's 100 Mbit/s port but **not proof the partner is the coupler** | verified fact, identity unverified | `ethtool` link-partner section |
| Address configuration | No IPv4; link-local IPv6 only; the host's own mDNS/DHCP/router-solicitation traffic egresses on it (a network manager is autoconfiguring the interface — must be disabled on this NIC before an EtherCAT master binds it) | verified | `ip addr`, 8 s passive `tcpdump` (3 packets, all host-originated) |
| Partner traffic | **None observed** in an 8 s passive capture — consistent with a coupler that only forwards frames a master sends | verified observation | `sudo tcpdump` 2026-09-15 |
| Timestamping | Software-only (no hardware PTP) | verified | `ethtool -T` |
| Interrupt/coalescing | Single queue via xhci_hcd MSI; `rx-usecs: 15000` default coalescing hint (driver may not honor it) | verified | `/proc/interrupts`, `ethtool -c` |

## Field segment

| Field | Value | Status | Evidence |
| --- | --- | --- | --- |
| Topology | One point-to-point segment: NIC → (cable) → one EtherCAT station. Whether a switch or additional stations sit between is unknown | unverified | link-partner advertisement implies a direct 10/100 device |
| Station count | Expected exactly 1 | unverified | master discovery at commissioning |
| EtherCAT frames on segment | None until a master transmits (passive capture was silent) | verified | tcpdump 2026-09-15 |

## Expected device identity — station 0

The plan's photograph identifies a **Wago 750-354 EtherCAT fieldbus coupler**
with attached labels suggesting a **750-501** (2-channel DO, 24 V DC, 0.5 A)
and a **750-400** (2-channel DI, 24 V DC, 3.0 ms filter). One station exposes
all terminals' process data through the coupler — the terminals are **not**
separate EtherCAT endpoints.

| Field | Expected | Status | Evidence / source |
| --- | --- | --- | --- |
| Coupler model | 750-354 (EtherCAT, 100 Mbit/s, 2x RJ-45, ≤64 modules, ≤1024 B process image each direction) | unverified — photograph only | plan photo; datasheet [Wago 750-354] |
| Coupler revision / firmware | — | unknown | read at commissioning (vendor/product/revision registers) |
| Terminal order | Expected: 750-354 | 750-501 (2 DO) | 750-400 (2 DI) | end terminal — **order and presence unconfirmed** | unverified | plan photo; must be read from the discovered process-data layout, not assumed |
| Supply / end modules | Field-supply feed for the DO/DI terminals and the terminating end module | unknown | not determinable remotely |
| EtherCAT identity check | Vendor ID `0x00000021` (Wago) + product/revision match at init; process-data layout compared against the declared profile; mismatch → startup fails before OP | datasheet + contract | EtherCrab `init` group filter; see stack note |
| Process-data layout (expected, provisional) | Outputs: 2 bits (750-501 ch1/ch2). Inputs: 2 bits (750-400 ch1/ch2), plus coupler status/control words if mapped. Digital data is bit-packed in terminal order — exact bit offsets are **datasheet-derived and must be confirmed by master discovery** | datasheet | 750-354 manual §process-data architecture; 750-400/501 data sheets (2-bit internal data width each) |

## Approved channels

| Channel | Point class | Approved use | Status |
| --- | --- | --- | --- |
| DO1 (750-501 ch1) | `Out` bool | Commanded through the DCS operator path during supervised commissioning only | unverified mapping |
| DO2 (750-501 ch2) | `Out` bool | Same; independence check target | unverified mapping |
| DI1 (750-400 ch1) | `In` bool | Read back as loopback witness of DO1 — **only valid once loopback wiring is verified** | unverified wiring |
| DI2 (750-400 ch2) | `In` bool | Loopback witness of DO2 / spare | unverified wiring |

No other channels exist on the expected rig. Any additional discovered terminal
is unapproved until this manifest is updated.

## Electrical ranges (datasheet values for the *expected* modules)

| Parameter | Value | Status | Source |
| --- | --- | --- | --- |
| Coupler system supply | 24 V DC, −25 %/+30 % (≈18–31.2 V) | datasheet | 750-354 data sheet |
| DI signal(0) / signal(1) | −3…+5 V / 15…30 V, ~4.5 mA per channel, 3.0 ms input filter | datasheet | 750-400 data sheet |
| DO rating | 24 V DC, 0.5 A per channel, high-side, short-circuit protected | datasheet | 750-501 data sheet |
| Isolation | 500 V system/field | datasheet | both module sheets |
| Actual field supply present | — | unknown | multimeter check at commissioning |
| Actual wiring (any) | — | unknown | not verifiable remotely |

## Interface binding

| Field | Value | Status |
| --- | --- | --- |
| Logical bus name | `ecat0` (proposed; set by the deployment config, not the model document) | unverified |
| Host interface | `enx00e04c751f7c` — bound by name at deployment; the model device carries the logical bus name only | verified interface, unverified binding config |
| Transport | Raw AF_PACKET socket (EtherCAT ethertype 0x88A4); container shape TBD — macvlan child preferred, interface passthrough fallback, host networking rejected | unverified — measured at commissioning |
| Prerequisite host change | Disable network-manager autoconfiguration on `enx00e04c751f7c` (it currently DHCP/mDNS-probes the field segment) — a supervised host change, not yet made | verified needed |

## Startup state

| Field | Expected | Status |
| --- | --- | --- |
| Coupler power-up behavior | Auto-configures the local process image from installed terminals; reaches PRE-OP under a master | datasheet |
| Driver startup contract | Station identity + process-data layout verified during PRE-OP; mismatch fails startup **before outputs enable**; output image initialized to all-zeros (safe off) before the first exchange | contract (proposed decision) |
| Rig fallback on master loss | 750-354 process-data watchdog drives field outputs to their safe state after the configured watchdog interval | datasheet — **behavior and interval must be confirmed from the manual and measured** |

## Watchdog response

| Field | Value | Status |
| --- | --- | --- |
| Mechanism | Coupler process-data watchdog: on interruption of cyclic exchange, outputs transition to the configured safe state (750-354 manual, "Interruption of the Cyclical Process Data Exchange" / "Behaviour during a Cycle Time Overrun") | datasheet |
| Configured interval | unknown — default per manual, exact value to be read from the coupler (object dictionary) or manual during commissioning | unknown |
| Acceptance bound | placeholder — set from the module manual plus measured host jitter; recorded here before acceptance testing | unknown |

## Available physical feedback

| Feedback | Status |
| --- | --- |
| Per-channel green status LEDs on 750-400/750-501 (DI/DO state) | datasheet — usable as supervised visual confirmation |
| DO→DI wired loopback | **unverified — none has been confirmed; do not infer one** |
| Coupler status word / diagnostic objects (0x10F3 diagnostic history etc.) | datasheet — readable over CoE once the master runs |
| Independent electrical observation (multimeter / scope at terminals) | unknown — supervised commissioning tooling |

## Explicit unknowns (dashboard "needs attention" set)

1. Coupler part number confirmation, revision, firmware — photograph-derived only.
2. Terminal order, module revisions, count, and the presence of supply/end modules.
3. Field-side power supply presence and voltage at the terminals.
4. All field wiring, including whether any DO→DI loopback exists.
5. Process-data layout (PDO assignment, bit offsets, coupler status/control words).
6. Watchdog configured interval and actual safe-state behavior.
7. Whether anything else shares the field segment (link-partner evidence says one 10/100 device, silent — consistent with the coupler alone).
8. Achievable exchange period/jitter on the USB NIC under lazy-preempt, powersave governor, homelab load.
9. macvlan operation over r8152 for non-IP EtherCAT frames.
10. Container topology for the field interface (macvlan vs. interface move).
11. Response-time and watchdog acceptance bounds — to be set from the module
    manuals plus measured host behavior, per the plan.

## Loopback commissioning plan (supervised, later step)

Preconditions: station identity and layout verified by master discovery; field
supply confirmed; **loopback wiring confirmed by a human on the rig** — this
plan does not create that wiring.

1. **Channel 1.** With DO1 wired to DI1: command DO1 through the DCS operator
   path (receipted command → component → staged output → exchange). Assert DI1
   follows DO1 in the telemetry. Record command→observed-transition latency.
2. **Channel 2.** Repeat DO2→DI2.
3. **Channel independence.** Toggle DO1 while observing that DI2 does **not**
   follow, and vice versa — proves the bit offsets are not swapped or shared.
4. **Timing bounds.** Measure exchange period jitter and command→feedback
   latency over a stated scan count under representative homelab load; compare
   against the acceptance bounds entered above (placeholder until the module
   manual and measurements land).
5. **Watchdog.** With the exchange stopped (controller kill), confirm outputs
   return to the safe state within the confirmed watchdog interval — observe
   via the DI loopback *and* independently (LED/multimeter), per the plan's
   rule that a report is not a substitute for a verified device response.
6. **Link loss/recovery.** Cable pull / link flap under supervision: link-down
   diagnostics surface, held inputs age to stale, reads escalate per contract;
   on restore the driver re-enters OP and exchanges resume.

## Machine-readable mirror

A JSON mirror of this manifest's verified/unknown status is published to the QA
evidence channel (`/srv/metrics/site/qa/rig-manifest.json` on the metrics host)
so the dashboard renders unverified fields as "needs attention". The checked-in
document is authoritative; the JSON is a reporting projection.
