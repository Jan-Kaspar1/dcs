# Product strategy

This document directs roadmap and library planning. `docs/architecture.md` remains the authority for technical decisions, and `docs/requirements/` turns this direction into traceable product requirements.

## Market sequence

The first target market is municipal and industrial water and wastewater. The first product must let an engineer assemble, simulate, commission, operate, diagnose, and maintain a representative plant from reusable components whose control behavior and operator UI share the same contracts.

Pharmaceutical and chemical batch control is a later product phase. Foundation choices should leave room for it, but current tickets must not add batch semantics unless a current water requirement or an accepted architecture decision needs the same capability.

## Delivery sequence

1. **Validate the foundation through a reference application.** Prove the plant model, UI/control contracts, hardware abstraction, deterministic execution, redundancy, diagnostics, commands, alarm management, and engineering workflow together in a simulated duty/standby pumping station. Alarm behavior is part of the UI/control contract: water operators must see consequential mode changes, abnormal equipment state, worsening process conditions, bad or stale data, and the response expected before the condition becomes hazardous.
2. **Prove the customer-owned plant boundary.** Build and run a plant from an independent repository using only versioned DCS release artifacts: the engineering API and schema, the generic controller/runtime image, and documented deployment inputs. Finish already-started reference slices, but do not continue broad component growth while the only working plant projects depend on workspace paths or private repository structure.
3. **Complete the schema-driven block and presentation boundary.** Make every reusable block publish one typed interface for measurements, configuration, runtime state, commands, and events. Prove that generic tooling and UI derive their surfaces from that contract and that slow, disconnected, restarted, or absent UI consumers cannot delay or terminate plant execution.
4. **Build the water and wastewater library.** Add reusable equipment modules and process assemblies in response to accepted requirements. Every component includes control behavior, modes, diagnostics, alarms, state persistence, descriptors, and faceplate behavior where applicable.
5. **Reach pilot readiness.** Make configuration, deployment, commissioning, backup and restore, upgrades, troubleshooting, and operator workflows suitable for a first customer plant.
6. **Expand by market evidence.** Add batch control for pharmaceutical and chemical plants after a separate requirements baseline and representative batch application exist.

## Product principles

- The unified plant model is the contract between engineering, controller runtime, monitoring, and UI.
- The DCS platform and each customer plant have separate ownership and release lifecycles. Customer plant source lives outside the platform repository and depends on documented, versioned release interfaces rather than workspace paths or platform internals.
- A block owns one typed interface schema. Measurements and state are observed, configuration and named commands are admitted through declared validation rules, and emitted events retain stable identities; the UI must not rediscover these categories from kind-specific code or naming conventions.
- The controller runtime is the plant authority. UI and telemetry delivery are replaceable consumers: they may disconnect, lag, restart, coalesce updates, or observe an explicit gap, but they never pace the scan or decide authoritative state. Commands are the exception to fire-and-forget delivery and always use the bounded, validated, receipted scan-boundary path.
- A reusable component is complete only when its control behavior, operator interaction, diagnostics, and lifecycle behavior work together.
- Alarm management is foundation work for water and wastewater. An alarm is actionable operator guidance with priority, state, context, history, and a defined response; it is not only a Boolean flag. Alarm handling does not replace an independent automatic protection function where hazard analysis requires one.
- Representative applications validate shared contracts before the library grows broadly.
- Research supplies evidence and alternatives. Architecture decisions define this product's semantics; vendor behavior is inspiration rather than a compatibility target.
- Customer evidence outranks vendor feature breadth. Record assumptions that still need an operator, integrator, or plant owner to validate.

## Planning policy

Every product issue cites one or more stable requirement IDs from `docs/requirements/`. A pure enabler may use `ENABLER`, but its scope must state which requirement or milestone it unlocks. If a requirement lacks enough evidence to write observable acceptance criteria, the planner creates a research issue first. Research issues update `docs/research/` and the applicable requirements file; implementation follows in a later planning pass.

The planner keeps the sequence above visible in `docs/plan.md`, checks current code and issues before proposing work, and favors a narrow vertical slice through the reference application over disconnected library breadth. Until `WW-ENG-003` is implemented, it treats the independent customer-project proof as the next product gate. It then treats `WW-FND-003` and `WW-FND-004` as the next foundation tranche ahead of new library breadth: first the shared block-interface schema, then the runtime/monitor isolation and generic-consumer proof. Already-started slices and concrete reliability or hardware prerequisites may finish.
