# Vision
I want to build a process control system from the ground up, including the controllers themselves. The controllers should be hardware-independent and run as Docker containers. I should be able to start a redundant controller instance on another machine and hot-swap between them without interrupting the process, so the active controller continues running while another instance is updated, replaced, or takes over.

On top of that, I want a monitoring and control system built around a unified plant data model. The same model that defines the plant configuration, devices, signals, and relationships should also be the contract used by the controllers and by the monitoring and control UI. This contract is important because it means the control logic, engineering data, and visualization all refer to the same underlying plant model instead of maintaining separate representations.

For the controllers, I want a hardware abstraction layer. The control logic should not need to care whether an input or output comes from local I/O, EtherCAT, Ethernet, or another field interface. I define which logical I/O the control logic requires, map it to the physical device, and the runtime handles communication with the field devices.

The goal is to let customers quickly build and change plant software by composing tested, reusable components in a higher-level language such as Rust. Standard pumps, motors, valves, and other components include control behavior, diagnostics, alarms, state handling, redundancy contracts, and operator controls. Types, simulation, and conformance tests establish the supported behavior and its limits.

The platform handles communication, fieldbus drivers, supported device integrations, recovery, and protection constraints. Customers can add custom components, but arbitrary extensions do not inherit platform guarantees: those depend on declared, verified timing, state, and compatibility contracts. Engineering should connect simulation, change review, deployment and recovery, commissioning, and operation.

Customers and their agents can tailor plant screens around platform equipment controls, alarm management, and status conventions. Adding equipment should reuse its tested behavior, diagnostics, tests, and presentation from the same model. A candidate commissioning workflow would verify connections and behavior; its open questions belong in the [platform requirements](docs/requirements/platform.md).

Each reusable block declares one typed, machine-readable interface covering measurements, configuration, runtime state, commands, and events. The controller runtime owns the authoritative plant state and execution; monitoring and UI processes are replaceable consumers whose slowness, disconnection, or restart must not delay or stop a control scan. Commands enter only through a bounded, validated, receipted path, while telemetry delivery may coalesce or expose gaps without changing plant behavior.

Customer plants live in independent repositories, pin versioned DCS releases, use supported engineering APIs, and deploy with the generic runtime. Platform fixtures complement independent consumer proofs. [Product strategy](docs/product-strategy.md) sets delivery scope; [batch research](docs/research/chemical-production-scheduling.md) preserves the scheduling and ISA-88 foundations for future expansion.

## Agent workflow

- For product direction, market requirements, library scope, or roadmap work, read `docs/product-strategy.md`, then the applicable file under `docs/requirements/` and its linked evidence under `docs/research/`. Water and wastewater is the first market; batch control is a later expansion.
- Implement only the assigned issue and its acceptance criteria. Read `docs/architecture.md` and `docs/plan.md` before making design decisions; planners maintain those documents through reviewed CI-gated changes.
- Develop software against simulated I/O. Physical equipment access and live infrastructure deployment are outside this autonomous workflow.
- Run `python3 scripts/verify.py` before reporting completion. Route heavy Rust builds through this command so shared build limits apply.
- Leave implementation edits in the assigned clone and report verification results and unresolved criteria. The supervisor owns staging, committing, publishing PRs, issue reservations, and merging; workers leave these Git operations to it.
- Preserve unrelated files and incomplete work. Report denied permissions or missing credentials as blockers; never escalate permissions or switch models to bypass a failure.
