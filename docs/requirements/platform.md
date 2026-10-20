# Platform and customer engineering requirements

Kaspar's product brief of 20 October 2026 sharpens the existing vision: customers should quickly build and change their plants from tested components, inherit supported runtime and field-connectivity behavior, and tailor their UI around consistent platform functions. Custom components are allowed with explicit verification and support limits. Guided on-site commissioning is a candidate; its workflow and policies remain open.

These requirements extend the [product strategy](../product-strategy.md). Existing [water requirements](water-wastewater.md) retain their statuses and evidence; this page does not claim new capabilities are implemented or alter market gates, milestone order, or the disabled automatic backlog generation. Accepted entries record product intent with observable acceptance targets; shared contracts still require reviewed architecture decisions before implementation.

## PLT-ENG-001 — Change plants using tested foundations

- **Status:** accepted; end-to-end change-workflow evidence pending.
- **Need:** Customers own their plant code and compose standard equipment with tested control behavior, diagnostics, alarms, operator controls, and state/redundancy contracts. Supported fieldbus drivers and device integrations bind logical I/O without rewriting control logic. Releases state tested configurations and limits; physical wiring and site acceptance require commissioning evidence.
- **Acceptance evidence:** An independent plant project pins released artifacts, adds or rebinds equipment and changes a control parameter or composition through supported APIs, validates and simulates the revision, reviews its effects, and follows a documented compatible activation or rejection and recovery procedure. Record engineering effort and repeat the checks affected by the change. Reuse `WW-FND-001`–`004`, `WW-CTL-001`, and `WW-ENG-003`; deployment/recovery policy remains linked to candidate `WW-LCM-002`. Agree a useful change-time target from representative customer work rather than inventing one.

## PLT-EXT-001 — Explicit extension and guarantee boundaries

- **Status:** accepted; extension mechanism and conformance details require design.
- **Need:** Customers may add their own components. Support and inherited platform guarantees depend on explicit interface, execution-time, determinism, state/restore, compatibility, and failure-behavior obligations. Unverified extensions are identified as such; arbitrary code in the controller cannot be assumed isolated from the runtime.
- **Acceptance evidence:** Publish the supported extension contract and its checks. Demonstrate a conforming customer component and named failures for violated obligations. State which guarantees apply to each tested configuration, including redundancy, and which require additional evidence. Do not treat a type-correct extension as fully verified or select a plugin architecture through this requirement.

## PLT-UI-001 — Tailored screens with consistent platform operation

- **Status:** accepted; customer-UI composition evidence pending.
- **Need:** Customers and their agents can tailor layouts and process views while composing platform equipment controls, alarm management, and status/quality indications. The supported UI retains a defined platform alarm/status surface; presentation changes cannot bypass controller-side validation or become an execution dependency.
- **Acceptance evidence:** Two different customer layouts use the same versioned interface and platform operational components. Both preserve required alarm visibility, command availability/refusal and receipt semantics, and quality indications; absent or stalled UI consumers leave authoritative execution unchanged. Reuse `WW-FND-003`–`004` and `WW-OPS-001`–`003`. Define the mandatory presentation contract in review; this does not introduce a graphical engineering editor.

## PLT-COM-001 — Guided commissioning from the plant model

- **Status:** candidate, linked to `WW-LCM-002`; not implementation authority.
- **Need:** A technician can identify equipment, inspect expected I/O connections, exercise authorized checks, compare commands and feedback with physical observations, record defects, and restore normal operation. Checks and results should reuse the equipment/model contract and name its revision.
- **Candidate acceptance:** Start with one pump: identify → inspect mapping/quality → authorized command and feedback check → physical confirmation → applicable alarm/permissive checks → restoration and recorded result. Distinguish commanded, reported, and physically observed state. Record tester, revision, test conditions, outcomes and unresolved defects; a changed mapping invalidates affected check results. Phone-friendly access is an option, not a selected app architecture.
- **Open evidence / revisit condition:** A site owner or commissioning specialist supplies the representative loop, permitted actions and roles, local/remote ownership, connection-loss behavior, force/inhibit handling, physical precautions and witness/retention obligations. Then refine the workflow with simulated acceptance cases before any separately authorized physical trial. Existing [commissioning research](../research/deployment-commissioning.md) and the [automated handover record](../releases/commissioning-record.md) are inputs; simulation does not establish physical connectivity or site sign-off.

## Future batch foundation

Preserve the [Maravelias / ISA-88 investigation](../research/chemical-production-scheduling.md) under [deferred batch requirements](batch-control.md). Its candidate requirements and design alternatives are research for future plants, not a mandatory batch-engine decomposition or an active implementation stage.
