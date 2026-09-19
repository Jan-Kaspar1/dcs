# Vision
I want to build a process control system from the ground up, including the controllers themselves. The controllers should be hardware-independent and run as Docker containers. I should be able to start a redundant controller instance on another machine and hot-swap between them without interrupting the process, so the active controller continues running while another instance is updated, replaced, or takes over.

On top of that, I want a monitoring and control system built around a unified plant data model. The same model that defines the plant configuration, devices, signals, and relationships should also be the contract used by the controllers and by the monitoring and control UI. This contract is important because it means the control logic, engineering data, and visualization all refer to the same underlying plant model instead of maintaining separate representations.

For the controllers, I want a hardware abstraction layer. The control logic should not need to care whether an input or output comes from local I/O, EtherCAT, Ethernet, or another field interface. I define which logical I/O the control logic requires, map it to the physical device, and the runtime handles communication with the field devices.

The goal is to make it easy to build a high-quality reusable library that covers both control logic and the corresponding monitoring and control UI. As an engineer, I should be able to use a higher-level language such as Rust to compose plant software from these reusable components. The required contracts can be expressed through types and interfaces and enforced as much as possible at compile time.

The library should encapsulate the difficult parts: communication, diagnostics, state handling, visualization conventions, safety-related constraints, and other system requirements. That way, engineering a plant becomes mostly about describing the actual process and connecting reusable components, while the platform ensures that the surrounding contracts are fulfilled consistently.

Adding a new device should follow the same model. Once its device integration and library component exist, I can instantiate it in the plant model, connect its I/O, and automatically obtain the corresponding control behavior, monitoring information, diagnostics, and UI representation without engineering each layer separately.

Each reusable block declares one typed, machine-readable interface covering measurements, configuration, runtime state, commands, and events. The controller runtime owns the authoritative plant state and execution; monitoring and UI processes are replaceable consumers whose slowness, disconnection, or restart must not delay or stop a control scan. Commands enter only through a bounded, validated, receipted path, while telemetry delivery may coalesce or expose gaps without changing plant behavior.

Customer plant code is a consumer of this platform, not part of the platform implementation. A plant project must be able to live in its own repository, depend only on versioned DCS release artifacts, compose its model from the supported engineering API, and deploy that model with the generic controller runtime. Platform-owned fixtures may remain in this repository for conformance testing, but they are not the customer-project boundary and must not be the only proof that the public interfaces work.

## Agent workflow

- For product direction, market requirements, library scope, or roadmap work, read `docs/product-strategy.md`, then the applicable file under `docs/requirements/` and its linked evidence under `docs/research/`. Water and wastewater is the first market; batch control is a later expansion.
- Implement only the assigned issue and its acceptance criteria. Read `docs/architecture.md` and `docs/plan.md` before making design decisions; planners maintain those documents through reviewed CI-gated changes.
- Develop software against simulated I/O. Physical equipment access and live infrastructure deployment are outside this autonomous workflow.
- Run `python3 scripts/verify.py` before reporting completion. Route heavy Rust builds through this command so shared build limits apply.
- Leave implementation edits in the assigned clone and report verification results and unresolved criteria. The supervisor owns staging, committing, publishing PRs, issue reservations, and merging; workers leave these Git operations to it.
- Preserve unrelated files and incomplete work. Report denied permissions or missing credentials as blockers; never escalate permissions or switch models to bypass a failure.
