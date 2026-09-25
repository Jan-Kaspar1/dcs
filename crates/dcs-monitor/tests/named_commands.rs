//! End-to-end tests for the declared named-command and emitted-event
//! surface: `Command::Invoke` rides the bounded scan-boundary path and
//! settles through ordinary receipts and `command_settled` journal
//! entries, and the events a kind emits during `step` route by their
//! declared `EventRetention` — `Journal`-retained and undeclared
//! emissions journal at the producing tick in emission order (the
//! durable `--journal-file` carries them) while `History`/`Latest`
//! emissions land in the read model's routed stores, served beside the
//! journal tail in the resource view's per-instance `events`.

use dcs_blocks::{Sequencer, SequencerStep};
use dcs_core::{
    Command, CommandError, CommandOutcome, CommandState, ComponentDescriptor, Direction,
    EmittedEvent, EventDecl, EventField, EventFieldKind, EventRetention, EventValue, IoDriver,
    IoError, JournalEntry, JournalEvent, PointId, ResourceEvent, ResourceView, Sample, Tick, Value,
    ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig, read_journal_file};
use dcs_runtime::{Component, ComponentIo, Executor, IoRequirement, PointMap, StepError};
use std::collections::HashMap;
use std::path::Path;
use std::process::{Command as Process, Output};
use std::sync::Mutex;
use std::thread;

/// In-memory driver stub — the same minimal stand-in the other monitor
/// tests use.
struct StubDriver {
    points: Mutex<HashMap<PointId, Sample>>,
}

impl StubDriver {
    fn new(points: &[(PointId, Value)]) -> Self {
        Self {
            points: Mutex::new(
                points
                    .iter()
                    .map(|&(point, value)| (point, Sample::good(value, Tick::ZERO)))
                    .collect(),
            ),
        }
    }
}

impl IoDriver for StubDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.points
            .lock()
            .unwrap()
            .get(&point)
            .copied()
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let mut points = self.points.lock().unwrap();
        let sample = points.get_mut(&point).ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != sample.value.kind() {
            return Err(IoError::TypeMismatch {
                point,
                expected: sample.value.kind(),
                found: value,
            });
        }
        *sample = Sample::good(value, Tick::ZERO);
        Ok(())
    }
}

/// A component emitting declared events during `step`: `fired` —
/// `Journal`-retained — then `shift` — `History`-retained — then
/// `beat` — `Latest`-retained — each scan, the payload's `n` counting
/// emissions. `fail` reports the step error
/// after emitting, so a failing step's events still drain.
struct Emitter {
    name: &'static str,
    n: i64,
    fail: bool,
}

impl Emitter {
    fn event(event: &str, n: i64) -> EmittedEvent {
        EmittedEvent {
            event: event.to_string(),
            component: String::new(),
            fields: [("n".to_string(), EventValue::Value(Value::Int(n)))]
                .into_iter()
                .collect(),
        }
    }
}

impl Component for Emitter {
    fn name(&self) -> &str {
        self.name
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        Vec::new()
    }

    fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        self.n += 1;
        if self.fail {
            return Err("the step reported a fault".into());
        }
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        let event = |name: &str, retention: EventRetention| EventDecl {
            name: name.to_string(),
            payload: vec![EventField {
                name: "n".to_string(),
                kind: EventFieldKind::Value(ValueKind::Int),
                optional: false,
            }],
            retention,
        };
        ComponentDescriptor {
            name: self.name.to_string(),
            kind: "emitter".to_string(),
            label: self.name.to_string(),
            ports: Vec::new(),
            parameters: Vec::new(),
            commands: Vec::new(),
            events: vec![
                event("fired", EventRetention::Journal),
                event("shift", EventRetention::History),
                event("beat", EventRetention::Latest),
            ],
        }
    }

    fn drain_events(&mut self) -> Vec<EmittedEvent> {
        vec![
            Self::event("fired", self.n),
            Self::event("shift", self.n),
            Self::event("beat", self.n),
        ]
    }
}

/// A component emitting an event its descriptor never declares — the
/// undeclared-emission routing case: the audit journal still carries
/// the emission, exactly once, under the durable class.
struct Undeclared;

impl Component for Undeclared {
    fn name(&self) -> &str {
        "rogue"
    }

    fn io_requirements(&self) -> Vec<IoRequirement> {
        Vec::new()
    }

    fn step(&mut self, _io: &dyn ComponentIo, _tick: Tick) -> Result<(), StepError> {
        Ok(())
    }

    fn describe(&self) -> ComponentDescriptor {
        ComponentDescriptor {
            name: "rogue".to_string(),
            kind: "rogue".to_string(),
            label: "rogue".to_string(),
            ports: Vec::new(),
            parameters: Vec::new(),
            commands: Vec::new(),
            events: Vec::new(),
        }
    }

    fn drain_events(&mut self) -> Vec<EmittedEvent> {
        vec![Emitter::event("mystery", 1)]
    }
}

/// The `dcs-ctl` binary — the `events` subcommand's serving-side proof
/// shares this file's emitter rig.
const CTL: &str = env!("CARGO_BIN_EXE_dcs-ctl");

/// The model fixture behind the monitor.
const MODEL: &str = include_str!("../fixtures/monitor.json");

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// The sequencer rig's points: `run`/`reset` `In` and the `out`/`step`/
/// `done` `Out` triple.
const RUN: PointId = PointId(10);
const RESET: PointId = PointId(11);
const OUT: PointId = PointId(20);
const STEP: PointId = PointId(21);
const DONE: PointId = PointId(22);

/// A two-step sequencer: each step drives one scan.
fn sequencer() -> Sequencer {
    Sequencer::new(
        "seq",
        RUN,
        RESET,
        OUT,
        STEP,
        DONE,
        vec![
            SequencerStep {
                ticks: 1,
                value: 10.0,
            },
            SequencerStep {
                ticks: 1,
                value: 20.0,
            },
        ],
    )
    .unwrap()
}

fn invoke(component: &str, command: &str, arguments: &[(&str, Value)]) -> Command {
    Command::Invoke {
        component: component.to_string(),
        command: command.to_string(),
        arguments: arguments
            .iter()
            .map(|(name, value)| (name.to_string(), *value))
            .collect(),
    }
}

/// Builds the sequencer rig and runs `body` against a serving monitor;
/// the server is shut down before the driver's borrow ends.
fn with_sequencer<T>(
    config: MonitorConfig,
    body: impl FnOnce(&StubDriver, &MonitorClient) -> T,
) -> T {
    let driver = StubDriver::new(&[
        (RUN, Value::Bool(false)),
        (RESET, Value::Bool(false)),
        (OUT, Value::Float(0.0)),
        (STEP, Value::Int(0)),
        (DONE, Value::Bool(false)),
    ]);
    let map = PointMap::new()
        .with_point(RUN, Direction::In, ValueKind::Bool)
        .with_point(RESET, Direction::In, ValueKind::Bool)
        .with_point(OUT, Direction::Out, ValueKind::Float)
        .with_point(STEP, Direction::Out, ValueKind::Int)
        .with_point(DONE, Direction::Out, ValueKind::Bool);
    let executor = Executor::new(&driver, map, vec![Box::new(sequencer())]).unwrap();
    let monitor = Monitor::bind_with("127.0.0.1:0", executor, signal_index(), config).unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result =
            std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(&driver, &client)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// Builds an executor over `components` — the emitters need no I/O
/// surface — and runs `body` against a serving monitor under `config`,
/// handing it the bound address for subprocess tooling; the server is
/// shut down before the driver's borrow ends.
fn with_components<T>(
    components: Vec<Box<dyn Component>>,
    config: MonitorConfig,
    body: impl FnOnce(&MonitorClient, std::net::SocketAddr) -> T,
) -> T {
    let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
        .into_iter()
        .collect();
    let executor = Executor::new(&driver, map, components).unwrap();
    let monitor = Monitor::bind_with("127.0.0.1:0", executor, signal_index(), config).unwrap();
    let addr = monitor.local_addr();
    let client = MonitorClient::new(addr);
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(&client, addr)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// Runs `dcs-ctl <addr> <args>` — the `events` read-side proof over the
/// emitter rig.
fn ctl(addr: std::net::SocketAddr, args: &[&str]) -> Output {
    Process::new(CTL)
        .arg(addr.to_string())
        .args(args)
        .output()
        .expect("failed to run dcs-ctl")
}

/// The served component's `events` — the resource view's per-instance
/// collection the retention classes share.
fn resource_events(client: &MonitorClient, name: &str) -> Vec<ResourceEvent> {
    client
        .resources()
        .unwrap()
        .components
        .into_iter()
        .find(|entry| entry.name == name)
        .unwrap_or_else(|| panic!("no served component named {name}"))
        .events
}

/// Whether the entry is a kind-emitted record of `name`.
fn emitted_named(entry: &ResourceEvent, name: &str) -> bool {
    matches!(
        &entry.event,
        JournalEvent::EventEmitted { event } if event.event == name
    )
}

/// The `n` payload an emitted-record entry carries.
fn emitted_n(entry: &ResourceEvent) -> i64 {
    let JournalEvent::EventEmitted { event } = &entry.event else {
        unreachable!("the fixture's entries are all emitted records")
    };
    match event.fields["n"] {
        EventValue::Value(Value::Int(n)) => n,
        _ => panic!("the n field must be Int"),
    }
}

/// The journal's `event_emitted` entries since `since`, in seq order.
fn emitted(client: &MonitorClient, since: u64) -> Vec<JournalEntry> {
    client
        .journal(since)
        .unwrap()
        .into_iter()
        .filter(|entry| matches!(entry.event, JournalEvent::EventEmitted { .. }))
        .collect()
}

/// The journal's `command_settled` entries since `since`, in seq order.
fn settled(client: &MonitorClient, since: u64) -> Vec<JournalEntry> {
    client
        .journal(since)
        .unwrap()
        .into_iter()
        .filter(|entry| matches!(entry.event, JournalEvent::CommandSettled { .. }))
        .collect()
}

/// The `seq` component's served command state for `name` — the
/// `GET /resources` view's `commands` entry.
fn command_state<'a>(view: &'a ResourceView, name: &str) -> &'a CommandState {
    view.components
        .iter()
        .find(|entry| entry.name == "seq")
        .and_then(|seq| seq.commands.iter().find(|command| command.name == name))
        .unwrap_or_else(|| panic!("{name} missing from seq's commands"))
}

#[test]
fn invoke_submission_rejections_journal_at_the_run_tick() {
    with_sequencer(MonitorConfig::default(), |_driver, client| {
        // Unknown component, undeclared command, and argument-kind
        // mismatch all refuse at admission — the receipted rejection
        // journals immediately at the run's tick.
        let rejected = client.command(&invoke("nope", "advance", &[])).unwrap();
        assert_eq!(
            rejected.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownComponent {
                    component: "nope".to_string()
                }
            }
        );
        let rejected = client.command(&invoke("seq", "spin", &[])).unwrap();
        assert_eq!(
            rejected.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::UnknownCommand {
                    component: "seq".to_string(),
                    command: "spin".to_string(),
                }
            }
        );
        let rejected = client
            .command(&invoke("seq", "advance", &[("count", Value::Bool(true))]))
            .unwrap();
        assert_eq!(
            rejected.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::ArgumentTypeMismatch {
                    component: "seq".to_string(),
                    command: "advance".to_string(),
                    argument: "count".to_string(),
                    expected: ValueKind::Int,
                    found: ValueKind::Bool,
                }
            }
        );

        // Every admission rejection journaled `command_settled` at tick
        // 0 — the run's current tick, before any scan.
        let entries = settled(client, 0);
        assert_eq!(entries.len(), 3);
        assert!(entries.iter().all(|entry| entry.tick == Tick(0)));
    });
}

#[test]
fn sequencer_invokes_apply_at_the_boundary_and_journal() {
    with_sequencer(MonitorConfig::default(), |driver, client| {
        // `advance` queues like every command: the receipt settles when
        // the next scan applies it.
        let accepted = client.command(&invoke("seq", "advance", &[])).unwrap();
        assert_eq!(
            accepted.outcome,
            CommandOutcome::Accepted {
                apply_tick: Tick(1)
            }
        );
        assert!(settled(client, 0).is_empty());
        client.advance(1).unwrap();
        let entries = settled(client, 0);
        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].tick, Tick(1));
        assert_eq!(
            entries[0].event,
            JournalEvent::CommandSettled {
                receipt: dcs_core::CommandReceipt {
                    command: invoke("seq", "advance", &[]),
                    outcome: CommandOutcome::Applied { tick: Tick(1) },
                    actor: None,
                    reason: None,
                }
            }
        );
        // The effect: the table moved to step 2.
        let snapshot = client.snapshot().unwrap();
        assert_eq!(
            snapshot
                .points
                .iter()
                .find(|point| point.point == STEP)
                .unwrap()
                .sample
                .unwrap()
                .value,
            Value::Int(2)
        );

        // `run` paced to the end: each completing step emitted its
        // `step_completed` at the producing tick — step 2 on tick 2,
        // then the run `done`.
        driver.write(RUN, Value::Bool(true)).unwrap();
        client.advance(1).unwrap();
        let events = emitted(client, 0);
        assert_eq!(events.len(), 1);
        assert_eq!(events[0].tick, Tick(2));
        assert_eq!(
            events[0].event,
            JournalEvent::EventEmitted {
                event: EmittedEvent {
                    event: "step_completed".to_string(),
                    component: "seq".to_string(),
                    fields: [("step".to_string(), EventValue::Value(Value::Int(2)))]
                        .into_iter()
                        .collect(),
                }
            }
        );

        // `advance` past the end is `KindDeclared`-refused at the
        // boundary — the kind's reason reaches the receipt and journal.
        client.advance(1).unwrap(); // step 2 completes → done
        let refused = client.command(&invoke("seq", "advance", &[])).unwrap();
        assert!(matches!(refused.outcome, CommandOutcome::Accepted { .. }));
        client.advance(1).unwrap();
        let refusal = settled(client, 0)
            .into_iter()
            .find(|entry| {
                matches!(
                    &entry.event,
                    JournalEvent::CommandSettled { receipt }
                        if matches!(receipt.outcome, CommandOutcome::Rejected { .. })
                )
            })
            .expect("the boundary refusal is journaled");
        let JournalEvent::CommandSettled { receipt } = &refusal.event else {
            unreachable!()
        };
        assert_eq!(
            receipt.outcome,
            CommandOutcome::Rejected {
                reason: CommandError::CommandRefused {
                    component: "seq".to_string(),
                    command: "advance".to_string(),
                    reason: "the sequence has run to its end; reset restarts it".to_string(),
                }
            }
        );

        // `reset` is `Always`-available: it applies and the table
        // reports step 1 again.
        client.command(&invoke("seq", "reset", &[])).unwrap();
        client.advance(1).unwrap();
        assert_eq!(
            client
                .snapshot()
                .unwrap()
                .points
                .iter()
                .find(|point| point.point == STEP)
                .unwrap()
                .sample
                .unwrap()
                .value,
            Value::Int(1)
        );
    });
}

#[test]
fn resources_serve_the_published_command_verdicts() {
    with_sequencer(MonitorConfig::default(), |driver, client| {
        // The seed publication predates the first scan's probe: with
        // no `command_verdicts` section to join, a `KindDeclared`
        // command keeps the unconditional `available` the read model
        // reported before the section existed.
        let view = client.resources().unwrap();
        assert!(command_state(&view, "advance").available);
        assert_eq!(command_state(&view, "advance").refusal, None);

        // One scan in, the probe's verdicts join the view: `advance`
        // mid-table reports invocable — and `reset`, `Always`-
        // available, is untouched by the section.
        client.advance(1).unwrap();
        let view = client.resources().unwrap();
        let advance = command_state(&view, "advance");
        assert!(advance.available);
        assert_eq!(advance.refusal, None);
        assert!(command_state(&view, "reset").available);

        // `run` paced to the table's end publishes the kind's standing
        // refusal: the served state reports `advance` unavailable
        // carrying the probe's reason — the same text a refused
        // submission's receipt settles.
        driver.write(RUN, Value::Bool(true)).unwrap();
        client.advance(2).unwrap();
        let view = client.resources().unwrap();
        let advance = command_state(&view, "advance");
        assert!(!advance.available);
        assert_eq!(
            advance.refusal.as_deref(),
            Some("the sequence has run to its end; reset restarts it")
        );
        assert!(command_state(&view, "reset").available);
    });
}

#[test]
fn emitted_events_journal_in_emission_order_at_the_producing_tick() {
    // Two emitters bracket a failing one: the journal carries the
    // `Journal`-retained emissions in scan then emission order at the
    // producing tick — including the failing step's — while the
    // `Latest`-retained `beat` follows its own serving channel and is
    // not duplicated into the durable journal.
    let driver = StubDriver::new(&[(PointId(10), Value::Float(0.0))]);
    let map: PointMap = [(PointId(10), Direction::In, ValueKind::Float)]
        .into_iter()
        .collect();
    let executor = Executor::new(
        &driver,
        map,
        vec![
            Box::new(Emitter {
                name: "first",
                n: 0,
                fail: false,
            }),
            Box::new(Emitter {
                name: "fragile",
                n: 0,
                fail: true,
            }),
            Box::new(Emitter {
                name: "last",
                n: 0,
                fail: false,
            }),
        ],
    )
    .unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig::default(),
    )
    .unwrap();
    let client = MonitorClient::new(monitor.local_addr());
    thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            client.advance(1).unwrap();

            let events = emitted(&client, 0);
            assert_eq!(
                events
                    .iter()
                    .map(|entry| {
                        let JournalEvent::EventEmitted { event } = &entry.event else {
                            unreachable!()
                        };
                        (entry.tick, event.component.as_str(), event.event.as_str())
                    })
                    .collect::<Vec<_>>(),
                vec![
                    (Tick(1), "first", "fired"),
                    (Tick(1), "fragile", "fired"),
                    (Tick(1), "last", "fired"),
                ]
            );

            // The failing step's own record lands after its emissions.
            let failure = client
                .journal(0)
                .unwrap()
                .into_iter()
                .find(|entry| matches!(entry.event, JournalEvent::StepFailed { .. }))
                .expect("the step failure is journaled");
            assert_eq!(
                failure.event,
                JournalEvent::StepFailed {
                    component: "fragile".to_string(),
                    error: "the step reported a fault".to_string(),
                }
            );
            assert!(failure.seq > events[1].seq);
        }));
        monitor.shutdown();
        result.unwrap_or_else(|panic| std::panic::resume_unwind(panic));
    });
}

#[test]
fn the_durable_journal_file_carries_emitted_events() {
    let dir = std::env::temp_dir().join(format!("dcs-named-commands-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let journal_path = dir.join("journal.jsonl");

    with_sequencer(
        MonitorConfig {
            journal_file: Some(journal_path.clone()),
            ..MonitorConfig::default()
        },
        |driver, client| {
            client.advance(1).unwrap();
            driver.write(RUN, Value::Bool(true)).unwrap();
            client.advance(2).unwrap();
        },
    );

    // The durable record holds the emitted `step_completed` events at
    // their producing ticks — what a cold restart replays.
    let data = read_journal_file(Path::new(&journal_path)).unwrap();
    let events: Vec<(Tick, i64)> = data
        .entries
        .iter()
        .filter_map(|entry| match &entry.event {
            JournalEvent::EventEmitted { event } if event.event == "step_completed" => {
                let step = match event.fields["step"] {
                    EventValue::Value(Value::Int(step)) => step,
                    _ => panic!("step field must be Int"),
                };
                Some((entry.tick, step))
            }
            _ => None,
        })
        .collect();
    assert_eq!(events, vec![(Tick(2), 1), (Tick(3), 2)]);
}

/// The routed classes' served half: `GET /resources`'s per-instance
/// `events` joins the retained journal tail beside the bounded
/// event-history ring's records and the latest-emission view's
/// standing record — each entry's `retention` mark telling the
/// diagnostic streams from the durable record — attributed per
/// producing component with payload and producing tick. `dcs-ctl
/// events` prints the same attributed lists.
#[test]
fn routed_emissions_serve_through_the_resource_view() {
    with_components(
        vec![
            Box::new(Emitter {
                name: "first",
                n: 0,
                fail: false,
            }),
            Box::new(Emitter {
                name: "last",
                n: 0,
                fail: false,
            }),
        ],
        MonitorConfig::default(),
        |client, addr| {
            client.advance(2).unwrap();

            let events = resource_events(client, "first");
            // `fired` — the durable record, once per scan, marked
            // `journal` like every journal-attributed entry.
            let fired: Vec<&ResourceEvent> = events
                .iter()
                .filter(|entry| emitted_named(entry, "fired"))
                .collect();
            assert_eq!(fired.len(), 2);
            assert!(
                fired
                    .iter()
                    .all(|entry| entry.retention == EventRetention::Journal)
            );
            assert_eq!(
                fired
                    .iter()
                    .map(|entry| (entry.tick, emitted_n(entry)))
                    .collect::<Vec<_>>(),
                vec![(Tick(1), 1), (Tick(2), 2)]
            );
            // `shift` — the event-history ring's records, payload and
            // producing tick intact, marked `history`.
            let shift: Vec<&ResourceEvent> = events
                .iter()
                .filter(|entry| emitted_named(entry, "shift"))
                .collect();
            assert_eq!(shift.len(), 2);
            assert!(
                shift
                    .iter()
                    .all(|entry| entry.retention == EventRetention::History)
            );
            assert_eq!(
                shift
                    .iter()
                    .map(|entry| (entry.tick, emitted_n(entry)))
                    .collect::<Vec<_>>(),
                vec![(Tick(1), 1), (Tick(2), 2)]
            );
            // `beat` — the latest-emission view's one standing record:
            // the newest emission, marked `latest`.
            let beat: Vec<&ResourceEvent> = events
                .iter()
                .filter(|entry| emitted_named(entry, "beat"))
                .collect();
            assert_eq!(beat.len(), 1);
            assert_eq!(beat[0].retention, EventRetention::Latest);
            assert_eq!((beat[0].tick, emitted_n(beat[0])), (Tick(2), 2));

            // The attribution rule is the producing component's: every
            // emitted record in `first`'s list is `first`'s, and
            // `last`'s list carries its own identical emissions.
            assert!(events.iter().all(|entry| matches!(
                &entry.event,
                JournalEvent::EventEmitted { event } if event.component == "first"
            )));
            let last = resource_events(client, "last");
            assert_eq!(last.len(), events.len());
            assert!(last.iter().all(|entry| matches!(
                &entry.event,
                JournalEvent::EventEmitted { event } if event.component == "last"
            )));

            // No emission is double-recorded: the journal tail carries
            // `fired` alone — each emitter's two — never `shift`/`beat`.
            let journal = emitted(client, 0);
            assert_eq!(journal.len(), 4);
            assert!(journal.iter().all(|entry| matches!(
                &entry.event,
                JournalEvent::EventEmitted { event } if event.event == "fired"
            )));

            // `dcs-ctl events` prints the same attributed list — the
            // routed entries' `retention` marks included.
            let output = ctl(addr, &["events", "first"]);
            assert!(output.status.success(), "{output:?}");
            let printed: Vec<ResourceEvent> = serde_json::from_slice(&output.stdout).unwrap();
            assert_eq!(printed, events);
        },
    );
}

/// The ring's bound is honest: past `event_history_capacity` the
/// event-history records evict oldest-first, the evicted stretch
/// reading as a numbering gap on the routed stream's never-reused
/// `seq`s — while `beat`'s standing record supersedes to the newest
/// emission and the durable journal is untouched.
#[test]
fn the_event_history_ring_evicts_oldest_first_at_the_bound() {
    with_components(
        vec![Box::new(Emitter {
            name: "em",
            n: 0,
            fail: false,
        })],
        MonitorConfig {
            event_history_capacity: 2,
            ..MonitorConfig::default()
        },
        |client, _addr| {
            client.advance(4).unwrap();
            let events = resource_events(client, "em");

            // Two retained `shift` records — the newest. The routed
            // stream numbers both classes in append order, so the
            // retained seqs 5 and 7 report the evicted 1–4 stretch as
            // a numbering gap, never silent loss.
            let shift: Vec<&ResourceEvent> = events
                .iter()
                .filter(|entry| emitted_named(entry, "shift"))
                .collect();
            assert_eq!(
                shift
                    .iter()
                    .map(|entry| (entry.seq, entry.tick, emitted_n(entry)))
                    .collect::<Vec<_>>(),
                vec![(5, Tick(3), 3), (7, Tick(4), 4)]
            );

            // `beat` stands as the newest emission only — superseded
            // three times over, it reports scan 4's record.
            let beat: Vec<&ResourceEvent> = events
                .iter()
                .filter(|entry| emitted_named(entry, "beat"))
                .collect();
            assert_eq!(beat.len(), 1);
            assert_eq!(beat[0].retention, EventRetention::Latest);
            assert_eq!(
                (beat[0].seq, beat[0].tick, emitted_n(beat[0])),
                (8, Tick(4), 4)
            );

            // The durable journal is a separate bound: all four
            // `fired` emissions still stand.
            assert_eq!(emitted(client, 0).len(), 4);
        },
    );
}

/// An emission the descriptor never declares still reaches the audit
/// record — journaled `event_emitted` exactly once under the durable
/// class, routed to no diagnostic store.
#[test]
fn an_undeclared_emission_journals_once_and_routes_nowhere_else() {
    with_components(
        vec![Box::new(Undeclared)],
        MonitorConfig::default(),
        |client, _addr| {
            client.advance(1).unwrap();

            let journal = emitted(client, 0);
            assert_eq!(journal.len(), 1);
            assert_eq!(
                journal[0].event,
                JournalEvent::EventEmitted {
                    event: EmittedEvent {
                        event: "mystery".to_string(),
                        component: "rogue".to_string(),
                        fields: [("n".to_string(), EventValue::Value(Value::Int(1)))]
                            .into_iter()
                            .collect(),
                    }
                }
            );

            // Attributed in the resource view as a durable entry — the
            // journal tail's `retention` mark.
            let events = resource_events(client, "rogue");
            assert_eq!(events.len(), 1);
            assert_eq!(events[0].retention, EventRetention::Journal);
            assert_eq!(events[0].tick, Tick(1));
        },
    );
}
