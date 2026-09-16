# QiTech framework as a block-interface and runtime-boundary reference

Accessed 2026-09-16. This note records a supplied design reference, not a compatibility target or implementation dependency. The inspected repository is `qitechgmbh/qitech_framework` at commit `9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f` (2026-09-15). A reference-only clone is kept outside the DCS source and build graph at `/home/kaspar/workspace/references/qitech/qitech_framework`; sibling clones of `qitech_lib`, `control`, and `ethercrab` are stored under the same reference directory. The QiTech framework is LGPL-3.0; DCS may learn from the architecture but must not copy implementation code without a deliberate licensing decision.

## Source facts

1. `MachineSchema` separates five resource collections: `config_properties`, `state_properties`, `measurements`, `commands`, and `events`. Each schema also carries a framework version and revision. [schema type](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/qitech_framework_core/src/schema/mod.rs)
2. Example YAML schemas use those categories directly: the laser declares target/tolerance configuration, an `in_tolerance` state, diameter measurements, and an `out_of_tolerance` event; the scale declares state, measurements, and `zero`/`tare`/`clear_tare` commands. [laser schema](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/examples/apps/qitech_laser/schemas/laser_v1.yaml), [scale schema](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/examples/apps/xtrem_scale/schemas/scale_v1.yaml)
3. Runtime registration validates declared resource paths and types and refuses duplicate measurement, command, and event registrations. Commands may declare a current execution capability before their callback runs. [measurement builder](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/qitech_framework/src/machine/build/measurements.rs), [command builder](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/qitech_framework/src/machine/build/command.rs), [event builder](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/qitech_framework/src/machine/build/event.rs)
4. Configuration writes carry constraints, external-write capability, operation origin, and accepted/rejected outcomes. State changes, command capability/execution, and emitted events are journaled separately, while measurements are sampled into their own report collection. [configuration property](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/qitech_framework/src/machine/config_property.rs), [machine report](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/qitech_framework_core/src/report/machines.rs)
5. The runtime architecture states that machine execution must not block on data delivery and assigns buffering, persistence, and distribution to a controller session. It also chooses to terminate the runtime when that sole controller session is lost, because that session is treated as the ordered persistence authority. [runtime architecture](https://github.com/qitechgmbh/qitech_framework/blob/9b953f2c23261f81fe38b3ebd8fa51e0669e7a6f/docs/architecture/runtime.md)

## Existing DCS basis and gaps

DCS already has much of the foundation: typed component ports, `ComponentDescriptor`, `ParameterDescriptor`, semantic `PortRole`s, typed `dcs-build` specs with drift tests, the signal index, point quality, live parameter telemetry, validated scan-boundary commands with structured receipts, and a durable ordered journal. The generic page already renders faceplates from descriptors without per-kind code.

The gaps are structural rather than cosmetic:

- measurements, writable configuration, runtime state, named commands, and emitted events are not one explicit served block-interface vocabulary;
- named block actions such as start/stop/home have no typed declaration or availability surface independent of generic point writes and parameter edits;
- block events have no declared payload schema and stable kind separate from inferred point/journal transitions;
- the current monitor and paced scan share an executor mutex, so UI/network non-interference is not yet an enforceable or load-tested boundary;
- consumer gap/coalescing and command-ingress overload behavior are not explicit contracts.

## Chosen DCS behavior

Decisions 82–83 and requirements `WW-FND-003`–`WW-FND-004` select the following behavior:

- one versioned block interface declares measurements, configuration, state, commands, and events;
- existing DCS descriptors and contracts migrate into that interface instead of creating parallel sources of truth;
- generic tools and UI work from the schema, while optional custom views remain presentation only;
- control execution publishes immutable read models to bounded consumer storage and never waits on UI delivery;
- slow consumers receive a newer value or an explicit sequence/freshness gap;
- commands are never fire-and-forget: bounded admission, validation, deterministic scan-boundary application, and structured receipts remain mandatory;
- losing a UI or telemetry consumer never terminates the controller. QiTech's loss-of-controller-session shutdown is not adopted because its session is a persistence authority, whereas the DCS browser UI is explicitly disposable.

## Open implementation details, not product-direction questions

- Whether the additive contract is named `BlockInterface`, `ComponentSchema`, or another term should follow the repository's domain vocabulary; the five semantic categories are fixed.
- The first named-command payload may deliberately support only zero-argument and typed scalar/object forms, but unsupported payload shapes must fail by name rather than fall back to raw JSON.
- Event retention classes need a small initial vocabulary aligned with latest-value telemetry, bounded history, and the durable journal; every emitted event need not be durable.
- The publication primitive may be an atomic latest snapshot, a bounded channel, or a locked copy outside the executor, provided the non-interference acceptance tests hold and memory is bounded.
