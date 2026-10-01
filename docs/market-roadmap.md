# Market evidence roadmap

Last updated: 2026-09-23.

This roadmap records the product journey. It does not add implementation requirements. Only water and wastewater is active; accepted requirements remain in `docs/requirements/water-wastewater.md`, and the existing milestone order remains in `docs/plan.md`.

A future market may be researched early, but implementation advances in the sequence below. Each market needs its own evidence pack. A passing gate in an earlier market does not prove a later market's needs.

## Common gate for each market

Before implementation broadens into a market:

1. Select a customer-shaped application and name its operating boundary and intended users.
2. Record primary sources and direct customer, operator, integrator, or domain-expert evidence. Separate sourced facts, proposed DCS behavior, and unresolved assumptions.
3. Create a market-specific requirements baseline with stable IDs, observable acceptance evidence, and explicit candidate, accepted, implemented, or deferred status.
4. Exercise an end-to-end representative plant through the versioned engineering, runtime, monitoring, and operator interfaces. Record which capabilities use the shared core and which require a declared market extension.
5. Define the verification and release evidence for that market, including deployment, upgrade, handover, or commissioning obligations where customer evidence requires them.

A manual or feature catalogue can suggest tests, but does not establish a customer requirement or satisfy this gate. Do not create implementation work from an unverified market assumption.

## Ordered stages

### 1. Water and wastewater — active

Keep the accepted baseline and milestone sequence in `docs/requirements/water-wastewater.md` and `docs/plan.md`. The current reference application is the simulated duty/standby pumping station. Complete the active water pre-pilot milestones in their existing order; preserve recorded deferrals until their stated revisit conditions fire before broadening the implementation portfolio.

### 2. Food and beverage — queued

Choose the representative process with a customer or domain expert. Discovery may ask whether recipe or sequence handling, cleaning, changeover, traceability, or quality records belong in the product boundary; these are questions, not established market requirements. Pass the common gate with its own application, scenarios, and accepted baseline before implementation.

### 3. Cement — queued

Choose a representative customer-backed application. Discovery may ask which process and equipment coordination, operating sequences, interlocks, failure responses, or operator diagnostics are needed. Treat these as research prompts only. Pass the common gate and identify any market-specific extension with executable evidence.

### 4. Chemical — queued

Choose the target plant class and representative application with customer or domain-expert input. Establish which control, recipe or procedure, records, identity, and change-management capabilities are product obligations for that application. Keep batch control deferred until the evidence supports its own requirements baseline and representative batch scenario.

### 5. Pharmaceutical — queued

Treat pharmaceutical as a separate stage from chemical. Use its own customer and domain evidence to decide whether validation records, review, identity, change control, or other obligations belong in the product contract. Do not transfer obligations or claims from chemical research or vendor examples without evidence for this stage.

### 6. Oil and gas — queued

Choose the intended segment and deployment context with specialist and customer input. Establish the relevant operating boundary, failure and hazard assumptions, availability expectations, remote-operation needs, and protection responsibilities before writing product requirements. A control-library feature list is not a substitute for those decisions.

## Library evidence across markets

Use the library completeness contract in `docs/product-strategy.md` for each reusable block, equipment module, and process assembly. Add market-specific behavior only when its requirement and representative scenario exist. Keep the shared contract generic where evidence supports reuse; version an extension when evidence shows a real difference. Record adopted APL-inspired test prompts with the reviewed page, DCS requirement, executable scenario, and any unresolved customer assumption in `docs/research/apl-reusable-library.md`.
