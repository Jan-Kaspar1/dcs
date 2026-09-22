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
5. **Close the pre-pilot gaps.** Take the validated water library to first-customer-pilot scale in the order `docs/plan.md`'s pre-pilot tranche records. Pilot-customer-scale deployment comes first — topology beyond one pair per model where the site requires it, a defined configuration-backup artifact set, named rollback semantics, and commissioning and handover records (`WW-LCM-002`), which depends on a first-client deployment plan or owner specification naming the lifecycle obligations, or on a pilot topology outgrowing the current configuration seams. The capability gaps follow in audit order: operations UI depth, completing the `WW-OPS-001`–`WW-OPS-003` end-to-end evidence and depending on the recorded station-policy answers only where they fix presentation policy; durable history and historian export, the recording substrate the `WW-REP-001`/`WW-LCM-002` retention clauses and the remaining acceptance runs assume; security and identity (`WW-SEC-001`), depending on a first client naming role tiers; operating reports (`WW-REP-001`), depending on a first client naming report formats; and engineering scale-up (`WW-ENG-002`), depending on a client naming I/O-list/tag conventions. Each step's acceptance evidence names the customer answer it depends on, and a requirement stays candidate until that evidence exists.
6. **Expand by market evidence.** Add batch control for pharmaceutical and chemical plants after a separate requirements baseline and representative batch application exist. Batch control stays deferred behind the pre-pilot tranche above.

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

## Product areas and investment allocation

Every open managed issue belongs to exactly one product area. The area names the
capability receiving the investment; it is independent of the crate being edited,
the validation venue, and the worker concurrency group. A reference-plant or rig
acceptance leg therefore inherits the capability it proves. Use `verification`
only when the deliverable is QA, simulation, or conformance machinery itself.

| Area | Target | Investment boundary |
| --- | ---: | --- |
| `engineering` | 15% | Engineering model, SDK, composition, and authoring experience |
| `control-runtime` | 15% | Deterministic execution, commands, state, and scan semantics |
| `high-availability` | 15% | Redundancy, failover, convergence, and ownership integrity |
| `field-connectivity` | 10% | Drivers, fieldbuses, cyclic exchange, and physical I/O |
| `operations` | 10% | Operator UI, monitoring, history, and runtime observability |
| `alarms-diagnostics` | 10% | Alarm lifecycle, diagnostics, attribution, and event evidence |
| `library` | 10% | Reusable control blocks, equipment modules, and process patterns |
| `deployment-lifecycle` | 5% | Packaging, release, upgrade, compatibility, and rollout |
| `verification` | 5% | Test infrastructure, conformance, simulation, and QA machinery |
| `delivery-platform` | 5% | Agent factory, CI, repository automation, and contributor flow |

Priority remains authoritative. Within one priority, dispatch favors areas below
their rolling target; targets guide portfolio balance and never manufacture work.

The planner keeps the sequence above visible in `docs/plan.md`, checks current code and issues before proposing work, and favors a narrow vertical slice through the reference application over disconnected library breadth. `WW-ENG-003`, `WW-FND-003`, and `WW-FND-004` are implemented, so the sequencing authority is the pre-pilot tranche `docs/plan.md` records: its ordered milestones and its deferral list, whose recorded deferrals are not re-proposed until their named revisit conditions fire. Already-started slices and concrete reliability or hardware prerequisites may finish.
