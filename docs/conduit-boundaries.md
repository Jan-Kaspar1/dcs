# Conduit-boundary list

The platform's externally reachable transports, enumerated once, as the
named partition points an integrator's IEC 62443-3-2 zone-and-conduit
design draws boundaries across and a client's SL-T assignment hangs off
(`WW-SEC-001`'s deferred zone/conduit evidence — `docs/research/
security-identity.md` gap 8 and `docs/research/pilot-acceptance.md` SE-7
point here). The list records what exists: it assigns no security levels,
draws no zones, and changes no contract.

## Reading the entries

Each entry records four things:

- **Transport** — the wire shape and the crate/binary that serves it.
- **Exposed operations** — what a connected peer can ask the surface to
  do.
- **Bind posture** — where the listener or link takes its address and who
  declares it. Two postures recur: *simulated links* in this repository's
  rigs bind so they are reachable only on host loopback or inside the
  no-egress rig bridge under the documented host egress policy
  (`qa_lane/netpolicy.py`, installed by `dcs-hwtest-netpolicy.service`,
  documented in `qa_lane/deploy/README.md` — rig bridges get no new
  outbound connections and no rig-sourced packet reaches a host socket);
  and *consumer deployments* bind the deployment-declared addresses the
  manifest shape in `docs/release-contract.md` records (`plant.listen`,
  `controllers[].listen`, `controllers[].standby`) or the model-declared
  device `parameters.address` — the platform binds what the declaration
  says, and the deployment's port publication decides external
  reachability.
- **Authentication boundary** — the decision-48 split
  (`docs/architecture.md`): the control product carries no transport
  authentication. Verification is site infrastructure — an authenticating
  fronting proxy, the site's directory/VPN/MFA layer, network
  segmentation — and the product's share is carrying a verified `actor`
  into the durable journaled record and bounding the declared command
  surface. Per-entry notes record only what a transport adds on top of
  that shared boundary.

The list is complete: these five are every externally reachable transport
a deployment exposes. The shipped tools — `dcs-ctl`, `dcs-plant-ctl`,
`dcs-sim-bus-ctl`, `dcs-alarm-report`, and the monitoring page — are
outbound-only clients of these surfaces and open no listeners of their
own; the model document, `--state-file`, and `--journal-file` are
filesystem artifacts, not conduits. QA-lane and agent-pool machinery are
test infrastructure outside the product surface. A new listener,
protocol, or device kind is a contract-visible event that updates this
list in the same change.

## The boundaries

### 1. Monitor HTTP surface — operator and tooling access

- **Transport:** HTTP/1.1 with JSON bodies over TCP — `dcs-monitor`'s
  synchronous embedded server (`tiny_http`), served by
  `dcs-controller --listen ADDR` (decision 8).
- **Exposed operations:** reads — `GET /` (the monitoring page),
  `GET /signals`, `GET /snapshot`, `GET /receipts`, `GET /role`,
  `GET /history`, `GET /journal`, `GET /schema`, `GET /resources`;
  control — `POST /command` (the bounded, validated, receipted command
  ingress of decisions 7 and 18), `POST /scan` (the driven-mode pacing
  request, refused `409` while `--scan-ms` paces), `POST /promote` and
  `POST /demote` (the role-switchover actions). `GET /checkpoint` is
  served on this same listener but is its own conduit — entry 2 — because
  its communicating peer and trust expectation differ from the operator
  surface's.
- **Bind posture:** deployment-declared. The controller binds exactly
  the `--listen` address it is launched with — conventionally
  `0.0.0.0:<port>` inside a container so the deployment's port
  publication decides reachability; in this repository's rigs the
  published ports bind host loopback only under the netpolicy above.
- **Authentication boundary:** unauthenticated by design — decision 48.
  `POST /command` accepts a declared `actor` carried into the journaled
  `CommandSettled` receipt: attribution, not verification; a fronting
  proxy fills `actor` from authenticated context and enforces whatever
  read/write grading the site requires (security-identity.md gaps 1 and
  5 hold the in-product alternatives as client-decision questions).
  `promote`/`demote` carry no actor field — gap 2 records that highest-
  consequence attribution hole.

### 2. Peer checkpoint link — pair-member tracking

- **Transport:** the same HTTP+JSON listener, `GET /checkpoint` —
  pulled by a tracking standby once per scan from the active's monitor
  address and applied at the standby's own scan boundary (decision 12).
  The pull may carry `?peer=<puller-monitor-addr>` (the tracking-source
  announce) and `?prove=<nonce>` (the keyed `line_proof` request); a
  demotion runs bounded verify pulls against announced candidates.
- **Bind posture:** served on each peer's `--listen`; the *dialed*
  address is deployment-declared — the `--standby`/`--peer` flags, the
  manifest's `standby` field, or a tracking source adopted under the
  keyed contract. Same loopback-published posture in this repository's
  rigs.
- **Authentication boundary:** a shared `--pair-token` adds the keyed
  `line_proof` — a digest stamped over the served checkpoint that only a
  peer holding the token produces, so an endpoint replaying or
  fabricating the line's checkpoints can neither arm a demotion nor feed
  a demoted peer forged state. It is a line-integrity attestation
  between pair members — a deployment secret carried on the invocation,
  never manifest data — not operator authentication. `GET /checkpoint`
  itself is a public read; who may reach it is the zone boundary's
  answer, not the protocol's.

### 3. Remote-driver plant protocol — the `dcs-sim-net` TCP link

- **Transport:** newline-delimited JSON over TCP — one request in
  flight per connection, messages capped at 64 KiB — the
  `PlantRequest`/`PlantResponse` protocol `dcs-plant-server` serves and
  `RemoteDriver` speaks (decision 13, #31).
- **Exposed operations:** `read`, `write`, the explicit `step`,
  fault injection (`inject_fault`/`clear_fault`), `list_points`, and the
  field write-ownership claim family — `claim_writer`,
  `claim_writer_unless_held`, `ensure_writer`, `release_writer`,
  `probe_writer` (decision 28's single-writer fencing).
- **Bind posture:** simulated link. The plant server binds `--listen`
  (a consumer manifest's `plant.listen`; `0.0.0.0` inside a container so
  the deployment's publication decides reachability); controllers attach
  through `--remote ADDR` or a model-declared `sim-tcp` device's
  `parameters.address`; `dcs-plant-ctl` drives the same link as a field
  tool. Under the documented netpolicy the link stays inside the
  no-egress rig bridge or on loopback-published ports.
- **Authentication boundary:** none on the wire. The claim family is
  *fencing* between attachments — which field owner may mutate the
  shared plant right now — not authentication of who may connect;
  reads, fault injection, and `list_points` stay open to every
  attachment by design (commissioning tooling needs them). Controlling
  who reaches the listener is site infrastructure.

### 4. sim-bus register protocol — the `dcs-sim-bus` TCP link

- **Transport:** length-prefixed binary frames over TCP — payloads
  capped at 8 KiB, one request in flight — the register-mapped fieldbus
  protocol `dcs-sim-bus-device` serves and `BusDriver` speaks
  (decision 32, #113).
- **Exposed operations:** `read_register`, `write_register`,
  `list_registers`, the explicit `step`, `claim_writer`/`release_writer`
  (the single-writer claim #154 extended to this wire), quality
  injection (`inject_quality`/`clear_quality`), `exchange` — the cyclic
  whole-image exchange the `sim-cyclic` kind runs under decision 78 —
  and the scripted-outcome `script_exchange`; `dcs-sim-bus-ctl` is the
  field tool. `sim-bus` and `sim-cyclic` devices share this one wire:
  they are one conduit, not two.
- **Bind posture:** simulated link. The device server binds `--listen`,
  defaulting to the device model's declared `parameters.address` — a
  model-declared `host:port` the deployment stands up; loopback or
  rig-bridge reachability under the documented netpolicy.
- **Authentication boundary:** as above — the claim is fencing, not
  authentication, and the wire carries no credentials.

### 5. EtherCAT cyclic binding — the `dcs-ethercat` field segment

- **Transport:** raw Ethernet frames — EtherCAT ethertype `0x88A4` — on
  a dedicated host interface: layer-2, non-routable, one `BusMaster` per
  logical bus over the EtherCrab-backed `EthercrabTransport`, running
  the whole-process-image `exchange` once per scan under the decision-78
  cyclic contract.
- **Exposed operations:** the cyclic exchange alone — each scan
  publishes the staged output image and latches the input image
  atomically. Station identity and process-data layout are verified
  during PRE-OP and a mismatch fails startup before outputs enable;
  declared `safe_outputs` seed the image before the first exchange.
- **Bind posture:** two-level by design (decision 47's split): the model
  declares only the *logical* bus name (`"bus"`, e.g. `ecat0`), and
  deployment configuration binds the name to a host NIC through
  `EthercatBuses` — the QA rig's binding is recorded in
  `docs/wago-ethercat-rig-manifest.md` (the dedicated `enx00e04c751f7c`
  USB adapter). A bus with no deployment binding fails startup honestly
  rather than substituting simulation, and the shipped `dcs-controller`
  invocation wires the registry's validating stub — reaching this
  surface at all is a deliberate deployment act.
- **Authentication boundary:** the cyclic exchange has no
  authentication construct — the boundary is the dedicated physical
  segment itself, and what may sit on it is the zone-and-conduit
  design's answer. What the product guarantees at the boundary is
  honest failure: a second logical bus claiming one interface fails,
  and the kind exposes `claim: None`, so redundant peers never
  arbitrate a field bus they cannot fence.

## What the list is not

It assigns no SL-T — an owner's IEC 62443-3-2 risk process assigns
target levels per zone and conduit — and it draws no zone boundaries —
the integrator's design does that against these named points. It is
documentation only: no code, wire-format, or model changes, and it does
not promote `WW-SEC-001`, whose recorded revisit conditions stand in
`docs/requirements/water-wastewater.md`.
