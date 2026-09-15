//! End-to-end test for `dcs-alarm-report` — the flood-and-performance
//! decision's computing surface. A scripted run over three managed
//! alarms produces the known transition record — an equipment-trip
//! burst, returns, acks, and shelve/suppress/out-of-service
//! transitions — then the monitor restarts over the same durable
//! journal file, and the tool computes every declared metric from the
//! file, deterministically. The served `GET /journal` view runs the
//! same computation without run-boundary markers.

use dcs_blocks::{AlarmLimits, ManagedAlarmConfig, ManagedAlarmIo, ManagedLatchingAlarm};
use dcs_core::{
    Command, CommandOutcome, Direction, IoDriver, IoError, JournalEvent, PointId, Sample, Tick,
    Value, ValueKind,
};
use dcs_model::{PlantModel, SignalIndex};
use dcs_monitor::alarm_report::AlarmReport;
use dcs_monitor::{Monitor, MonitorClient, MonitorConfig, read_journal_file};
use dcs_runtime::{Executor, PointMap, PointSpec};
use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::{Path, PathBuf};
use std::process::{Command as Process, Output};
use std::sync::Mutex;
use std::thread;

/// The tool under test, built by Cargo alongside the test harness.
const REPORT: &str = env!("CARGO_BIN_EXE_dcs-alarm-report");

/// The model fixture behind the rig: three `managed-latching-alarm`
/// instances — shelving/oos wired on the first, suppression/oos on the
/// second, the third declaring no managed inputs — so one driven scan
/// produces a genuine equipment-trip burst.
const MODEL: &str = include_str!("../fixtures/alarm_report.json");

const ALARM1_NAME: &str = "managed-latching-alarm:311";
const ALARM2_NAME: &str = "managed-latching-alarm:312";
const ALARM3_NAME: &str = "managed-latching-alarm:313";

// The rig's points, matching the fixture: inputs first (process values,
// writable operator points, the designed-suppression field point),
// then each alarm's five journaled status points.
const PV1: PointId = PointId(10);
const ACK1: PointId = PointId(11);
const SHELVE1: PointId = PointId(12);
const OOS1_IN: PointId = PointId(13);
const PV2: PointId = PointId(14);
const ACK2: PointId = PointId(15);
const SUPPRESS2: PointId = PointId(16);
const OOS2_IN: PointId = PointId(17);
const PV3: PointId = PointId(18);
const ACK3: PointId = PointId(19);
const ALARM1: PointId = PointId(30);
const UNACK1: PointId = PointId(31);
const SHELVED1: PointId = PointId(32);
const SUPPRESSED1: PointId = PointId(33);
const OOS1: PointId = PointId(34);
const ALARM2: PointId = PointId(35);
const UNACK2: PointId = PointId(36);
const SHELVED2: PointId = PointId(37);
const SUPPRESSED2: PointId = PointId(38);
const OOS2: PointId = PointId(39);
const ALARM3: PointId = PointId(40);
const UNACK3: PointId = PointId(41);
const SHELVED3: PointId = PointId(42);
const SUPPRESSED3: PointId = PointId(43);
const OOS3: PointId = PointId(44);

/// In-memory driver stub; the same minimal stand-in the other monitor
/// tests use — `dcs-monitor` sees only the `IoDriver` contract.
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
        *sample = Sample::good(value, sample.tick);
        Ok(())
    }
}

/// A scratch directory per test and process — tests run in parallel.
fn scratch(test: &str) -> PathBuf {
    let dir = std::env::temp_dir().join(format!("dcs-alarm-report-{test}-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn signal_index() -> SignalIndex {
    PlantModel::load(MODEL).unwrap().signal_index()
}

/// A journaled status `Out` point — the spec whose value transitions
/// join the durable journal as `point_changed` entries.
fn journaled_out() -> PointSpec {
    PointSpec {
        direction: Direction::Out,
        kind: ValueKind::Bool,
        internal: None,
        writable: false,
        stale_after_ticks: None,
        journaled: true,
    }
}

fn point_map() -> PointMap {
    let mut map = PointMap::new()
        .with_point(PV1, Direction::In, ValueKind::Float)
        .with_writable_internal(ACK1, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_writable_internal(SHELVE1, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_writable_internal(OOS1_IN, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(PV2, Direction::In, ValueKind::Float)
        .with_writable_internal(ACK2, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(SUPPRESS2, Direction::In, ValueKind::Bool)
        .with_writable_internal(OOS2_IN, Direction::In, ValueKind::Bool, Value::Bool(false))
        .with_point(PV3, Direction::In, ValueKind::Float)
        .with_writable_internal(ACK3, Direction::In, ValueKind::Bool, Value::Bool(false));
    for point in [
        ALARM1,
        UNACK1,
        SHELVED1,
        SUPPRESSED1,
        OOS1,
        ALARM2,
        UNACK2,
        SHELVED2,
        SUPPRESSED2,
        OOS2,
        ALARM3,
        UNACK3,
        SHELVED3,
        SUPPRESSED3,
        OOS3,
    ] {
        map = map.with_spec(point, journaled_out());
    }
    map
}

fn components() -> Vec<Box<dyn dcs_runtime::Component>> {
    let limits = || AlarmLimits {
        low: 10.0,
        high: 90.0,
        hysteresis: 5.0,
    };
    let alarm1 = ManagedLatchingAlarm::new(
        ALARM1_NAME,
        ManagedAlarmIo {
            input: PV1,
            ack: ACK1,
            shelve: Some(SHELVE1),
            oos: Some(OOS1_IN),
            suppress: None,
            alarm: ALARM1,
            unacknowledged: UNACK1,
            shelved: SHELVED1,
            suppressed: SUPPRESSED1,
            out_of_service: OOS1,
        },
        limits(),
        ManagedAlarmConfig {
            max_shelve_ticks: 600,
            priority: 1,
            class: 1,
            response_ticks: 30,
        },
    )
    .unwrap();
    let alarm2 = ManagedLatchingAlarm::new(
        ALARM2_NAME,
        ManagedAlarmIo {
            input: PV2,
            ack: ACK2,
            shelve: None,
            oos: Some(OOS2_IN),
            suppress: Some(SUPPRESS2),
            alarm: ALARM2,
            unacknowledged: UNACK2,
            shelved: SHELVED2,
            suppressed: SUPPRESSED2,
            out_of_service: OOS2,
        },
        limits(),
        ManagedAlarmConfig {
            max_shelve_ticks: 600,
            priority: 2,
            class: 1,
            response_ticks: 60,
        },
    )
    .unwrap();
    // The third instance declares no managed inputs — the plain trip
    // member of the burst.
    let alarm3 = ManagedLatchingAlarm::new(
        ALARM3_NAME,
        ManagedAlarmIo {
            input: PV3,
            ack: ACK3,
            shelve: None,
            oos: None,
            suppress: None,
            alarm: ALARM3,
            unacknowledged: UNACK3,
            shelved: SHELVED3,
            suppressed: SUPPRESSED3,
            out_of_service: OOS3,
        },
        limits(),
        ManagedAlarmConfig {
            max_shelve_ticks: 600,
            priority: 3,
            class: 1,
            response_ticks: 60,
        },
    )
    .unwrap();
    vec![Box::new(alarm1), Box::new(alarm2), Box::new(alarm3)]
}

/// Serves a monitor over a fresh executor writing the journal file at
/// `journal`, runs `body` against its client and bound address, and
/// shuts the server down before returning — one process lifetime of
/// the durable record.
fn serve<T>(
    driver: &StubDriver,
    journal: &Path,
    body: impl FnOnce(&MonitorClient, SocketAddr) -> T,
) -> T {
    let executor = Executor::new(driver, point_map(), components()).unwrap();
    let monitor = Monitor::bind_with(
        "127.0.0.1:0",
        executor,
        signal_index(),
        MonitorConfig {
            journal_file: Some(journal.to_path_buf()),
            ..MonitorConfig::default()
        },
    )
    .unwrap();
    let addr = monitor.local_addr();
    let client = MonitorClient::new(addr);
    let result = thread::scope(|scope| {
        scope.spawn(|| monitor.serve());
        // A failing assertion must not deadlock the scope join.
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| body(&client, addr)));
        monitor.shutdown();
        result
    });
    result.unwrap_or_else(|panic| std::panic::resume_unwind(panic))
}

/// A receipted write to a writable internal point — the operator's
/// ack/shelve/oos requests.
fn write(client: &MonitorClient, point: PointId, value: bool) {
    let receipt = client
        .command(&Command::WriteValue {
            point,
            kind: ValueKind::Bool,
            value: Value::Bool(value),
        })
        .unwrap();
    assert!(matches!(receipt.outcome, CommandOutcome::Accepted { .. }));
}

/// One invocation of the tool — the pretty JSON report on success.
fn report(addr: SocketAddr, journal: Option<&Path>, config: Option<&Path>) -> Output {
    let mut command = Process::new(REPORT);
    command.arg(addr.to_string());
    if let Some(path) = journal {
        command.arg("--journal-file").arg(path);
    }
    if let Some(path) = config {
        command.arg("--config").arg(path);
    }
    command.output().unwrap()
}

fn parsed(output: &Output) -> AlarmReport {
    assert!(
        output.status.success(),
        "dcs-alarm-report failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    serde_json::from_slice(&output.stdout).unwrap()
}

#[test]
fn the_scripted_run_yields_each_declared_metric_from_the_durable_record() {
    let dir = scratch("scripted");
    let journal_path = dir.join("journal.jsonl");
    let config_path = dir.join("report.json");
    // The declared threshold set — site policy as configuration: a
    // tick is a scan, the flood bound is 2 activations per 10 scans,
    // stale at 15, chattering at 3-in-5, fleeting under 2.
    std::fs::write(
        &config_path,
        r#"{
            "ticks_per_minute": 1,
            "flood_window_minutes": 10,
            "flood_alarms": 2,
            "stale_minutes": 15,
            "chattering_activations": 3,
            "chattering_window_minutes": 5,
            "fleeting_minutes": 2
        }"#,
    )
    .unwrap();

    let driver = StubDriver::new(&[
        (PV1, Value::Float(50.0)),
        (PV2, Value::Float(50.0)),
        (PV3, Value::Float(50.0)),
        (SUPPRESS2, Value::Bool(false)),
        (ALARM1, Value::Bool(false)),
        (UNACK1, Value::Bool(false)),
        (SHELVED1, Value::Bool(false)),
        (SUPPRESSED1, Value::Bool(false)),
        (OOS1, Value::Bool(false)),
        (ALARM2, Value::Bool(false)),
        (UNACK2, Value::Bool(false)),
        (SHELVED2, Value::Bool(false)),
        (SUPPRESSED2, Value::Bool(false)),
        (OOS2, Value::Bool(false)),
        (ALARM3, Value::Bool(false)),
        (UNACK3, Value::Bool(false)),
        (SHELVED3, Value::Bool(false)),
        (SUPPRESSED3, Value::Bool(false)),
        (OOS3, Value::Bool(false)),
    ]);

    // Run 1 — the scripted transition record:
    //
    //   t1   shelve alarm1 (shelved1 asserts)
    //   t2   the equipment trip: pv1/pv2/pv3 all over the limit and
    //        alarm2's designed suppression asserts — three activations
    //        plus two annunciations land in seq order (suppressed
    //        alarm2's unacknowledged is withheld)
    //   t3   ack alarms 1 and 3
    //   t4   alarm1 returns; unshelve it
    //   t5   alarm2 returns
    //   t6   suppression releases
    //   t7-12 alarm2 chatters: activations at 7, 9, 11 with one-scan
    //        returns — three fleeting episodes
    //   t12  both oos requests assert; the standing alarm2 unack is
    //        acked
    //   t13  oos1 releases
    //
    // alarm3 stays tripped — the standing alarm the restart carries.
    serve(&driver, &journal_path, |client, _addr| {
        write(client, SHELVE1, true);
        client.advance(1).unwrap();
        driver.write(PV1, Value::Float(95.0)).unwrap();
        driver.write(PV2, Value::Float(95.0)).unwrap();
        driver.write(PV3, Value::Float(95.0)).unwrap();
        driver.write(SUPPRESS2, Value::Bool(true)).unwrap();
        client.advance(1).unwrap();
        write(client, ACK1, true);
        write(client, ACK3, true);
        client.advance(1).unwrap();
        driver.write(PV1, Value::Float(50.0)).unwrap();
        write(client, SHELVE1, false);
        write(client, ACK1, false);
        write(client, ACK3, false);
        client.advance(1).unwrap();
        driver.write(PV2, Value::Float(50.0)).unwrap();
        client.advance(1).unwrap();
        driver.write(SUPPRESS2, Value::Bool(false)).unwrap();
        client.advance(1).unwrap();
        driver.write(PV2, Value::Float(95.0)).unwrap();
        client.advance(1).unwrap();
        driver.write(PV2, Value::Float(50.0)).unwrap();
        client.advance(1).unwrap();
        driver.write(PV2, Value::Float(95.0)).unwrap();
        client.advance(1).unwrap();
        driver.write(PV2, Value::Float(50.0)).unwrap();
        client.advance(1).unwrap();
        driver.write(PV2, Value::Float(95.0)).unwrap();
        client.advance(1).unwrap();
        driver.write(PV2, Value::Float(50.0)).unwrap();
        write(client, OOS1_IN, true);
        write(client, OOS2_IN, true);
        write(client, ACK2, true);
        client.advance(1).unwrap();
        write(client, OOS1_IN, false);
        write(client, ACK2, false);
        client.advance(1).unwrap();
    });

    // The restart: a fresh process lifetime over the same durable
    // file. The run boundary lands at open; the first scan's `from:
    // None` re-observations fold into the carried states — alarm3
    // still standing, alarm2's oos request dropped with the run —
    // while the fresh latch re-annunciates the standing alarm3.
    serve(&driver, &journal_path, |client, addr| {
        client.advance(1).unwrap();
        write(client, ACK3, true);
        client.advance(1).unwrap();
        write(client, ACK3, false);
        client.advance(1).unwrap();
        client.advance(1).unwrap();
        assert_eq!(client.snapshot().unwrap().tick, Tick(4));

        // The durable file is the record the acceptance reads: two
        // process lifetimes, the burst's every transition individually
        // present in seq order.
        let data = read_journal_file(&journal_path).unwrap();
        assert_eq!(data.boundaries.len(), 2);
        assert_eq!(data.boundaries[0].run, 1);
        assert_eq!(data.boundaries[1].run, 2);
        // Scoped to run 1's seqs: run 2's own tick domain re-uses low
        // tick numbers.
        let burst: Vec<(u64, PointId)> = data
            .entries
            .iter()
            .filter(|entry| entry.seq < data.boundaries[1].first_seq && entry.tick == Tick(2))
            .filter_map(|entry| match entry.event {
                JournalEvent::PointChanged { point, .. } => Some((entry.seq, point)),
                _ => None,
            })
            .collect();
        // Six burst transitions in ascending point order — the three
        // activations (30/35/40), two annunciations (31/41), and the
        // suppression flag (38) — each one a countable record.
        assert_eq!(
            burst.iter().map(|(_, point)| point.0).collect::<Vec<_>>(),
            vec![30, 31, 35, 38, 40, 41]
        );
        let seqs: Vec<u64> = data.entries.iter().map(|entry| entry.seq).collect();
        let mut sorted = seqs.clone();
        sorted.sort_unstable();
        assert_eq!(seqs, sorted, "the durable record is in seq order");

        // The report, computed from the file twice — identical records
        // produce identical metric output.
        let first = report(addr, Some(&journal_path), Some(&config_path));
        let second = report(addr, Some(&journal_path), Some(&config_path));
        assert_eq!(first.stdout, second.stdout);
        let computed = parsed(&first);

        // The source: the whole file, two runs, the elapsed-scans end.
        assert_eq!(computed.source.journal_entries, data.entries.len() as u64);
        assert_eq!(computed.source.journal_runs, Some(2));
        assert_eq!(computed.source.first_seq, Some(1));
        assert_eq!(computed.source.last_seq, Some(data.entries.len() as u64));
        assert_eq!(computed.source.first_tick, Some(Tick(1)));
        // The last entry is run 2's tick-3 ack release; the report's
        // end is the serving tick (run-2 tick 4) on the duration axis:
        // run 1's last entry at tick 13 offsets run 2 to 14, +4 = 18.
        assert_eq!(computed.source.last_tick, Some(Tick(3)));
        assert_eq!(computed.source.end, 18);
        assert_eq!(computed.source.span_ticks, 17);
        assert_eq!(
            (
                computed.thresholds.flood_window_ticks,
                computed.thresholds.flood_alarms,
                computed.thresholds.stale_ticks,
                computed.thresholds.chattering_window_ticks,
                computed.thresholds.fleeting_ticks
            ),
            (10, 2, 15, 5, 2)
        );

        // Rates: six activations — the burst's three plus alarm2's
        // three chatter trips, the suppressed burst member counted —
        // against four annunciations (the withheld latch, and alarm3's
        // restart re-annunciation).
        assert_eq!(computed.rates.activations, 6);
        assert_eq!(computed.rates.annunciations, 4);
        assert_eq!(computed.rates.peak_per_window, 6);
        assert_eq!(computed.rates.peak_window_start, Some(Tick(2)));

        // Flood windows: the record spans two tumbling windows; the
        // first holds five activations against the declared 2 bound.
        assert_eq!(computed.flood_windows.window_ticks, 10);
        assert_eq!(computed.flood_windows.windows, 2);
        assert_eq!(computed.flood_windows.over, 1);
        assert_eq!(computed.flood_windows.fraction_over, 0.5);

        // One flood period — the burst merged with the chatter trips:
        // activations at 2 (three), 7, 9, 11; annunciations inside the
        // period are the two burst latches plus alarm2's; first-out is
        // the lowest seq — alarm1's activation.
        assert_eq!(computed.flood_periods.len(), 1);
        let period = &computed.flood_periods[0];
        assert_eq!(period.start_tick, Tick(2));
        assert_eq!(period.end_tick, Tick(11));
        assert_eq!(period.activations, 6);
        assert_eq!(period.annunciations, 3);
        assert_eq!(period.first_out.component, ALARM1_NAME);
        assert_eq!(period.first_out.tick, Tick(2));

        // Standing and stale: alarm3 never returned — standing since
        // run-1 tick 2, sixteen elapsed scans across the restart, past
        // the declared 15-scan stale bound.
        assert_eq!(computed.standing.len(), 1);
        let standing = &computed.standing[0];
        assert_eq!(standing.component, ALARM3_NAME);
        assert_eq!(standing.signal, "mal-313-alarm");
        assert_eq!(standing.since_tick, Tick(2));
        assert_eq!(standing.standing_ticks, 16);
        assert!(standing.stale);
        assert!(!standing.approximate);
        assert!(standing.managed.is_empty());

        // Chattering and fleeting: alarm2's three activations inside
        // five scans, its three one-scan episodes fleeting.
        assert_eq!(computed.chattering.len(), 1);
        assert_eq!(computed.chattering[0].component, ALARM2_NAME);
        assert_eq!(computed.chattering[0].activations, 4);
        assert_eq!(computed.chattering[0].max_activations_in_window, 3);
        assert_eq!(computed.fleeting.len(), 1);
        assert_eq!(computed.fleeting[0].component, ALARM2_NAME);
        assert_eq!(computed.fleeting[0].episodes, 3);

        // Most-frequent: the chatterer first, ties by name.
        assert_eq!(
            computed
                .most_frequent
                .iter()
                .map(|row| (row.component.as_str(), row.activations))
                .collect::<Vec<_>>(),
            vec![(ALARM2_NAME, 4), (ALARM1_NAME, 1), (ALARM3_NAME, 1)]
        );

        // Managed-state accounting over the uniform vocabulary —
        // shelving duration, suppression, and out-of-service: alarm1
        // shelved t1..t4 (3 scans) and oos t12..t13 (1); alarm2
        // suppressed t2..t6 (4) and oos t12 until the restart's fresh
        // observation cleared it at run-2's first scan — axis 15 on
        // the elapsed-scans line (3); alarm3's states never asserted.
        let accounting: Vec<(&str, &str, u64, u64, bool)> = computed
            .managed_states
            .iter()
            .map(|row| {
                (
                    row.component.as_str(),
                    row.state.as_str(),
                    row.episodes,
                    row.total_ticks,
                    row.open,
                )
            })
            .collect();
        assert_eq!(
            accounting,
            vec![
                (ALARM1_NAME, "shelved", 1, 3, false),
                (ALARM1_NAME, "suppressed", 0, 0, false),
                (ALARM1_NAME, "out_of_service", 1, 1, false),
                (ALARM2_NAME, "shelved", 0, 0, false),
                (ALARM2_NAME, "suppressed", 1, 4, false),
                (ALARM2_NAME, "out_of_service", 1, 3, false),
                (ALARM3_NAME, "shelved", 0, 0, false),
                (ALARM3_NAME, "suppressed", 0, 0, false),
                (ALARM3_NAME, "out_of_service", 0, 0, false),
            ]
        );

        // Response times: the t3 acks pair the burst annunciations at
        // one scan each, alarm2's latch pairs its t12 ack at five, and
        // alarm3's restart re-annunciation pairs the run-2 ack at one
        // — all inside their declared response_ticks.
        let responses = &computed.responses;
        assert_eq!(responses.pairs.len(), 4);
        let pairs: Vec<(&str, u64)> = responses
            .pairs
            .iter()
            .map(|pair| (pair.component.as_str(), pair.ticks))
            .collect();
        assert_eq!(
            pairs,
            vec![
                (ALARM1_NAME, 1),
                (ALARM3_NAME, 1),
                (ALARM2_NAME, 5),
                (ALARM3_NAME, 1),
            ]
        );
        assert!(
            responses
                .pairs
                .iter()
                .all(|pair| pair.within_declared == Some(true))
        );
        assert_eq!(responses.pending, 0);
        assert_eq!(responses.unpaired_acks, 0);
        assert_eq!(responses.min_ticks, Some(1));
        assert_eq!(responses.max_ticks, Some(5));
        assert_eq!(responses.mean_ticks, Some(2.0));
        assert_eq!(responses.over_declared, 0);

        // The annunciated-priority distribution: one annunciation at
        // each of priorities 1 and 2, two at priority 3 — the restart
        // re-annunciation counted.
        assert_eq!(
            computed
                .priority_distribution
                .iter()
                .map(|row| (row.priority, row.annunciations))
                .collect::<Vec<_>>(),
            vec![(Some(1), 1), (Some(2), 1), (Some(3), 2)]
        );

        // The served-journal view computes the same counts — the
        // metrics all join — but carries no run-boundary markers:
        // `journal_runs` is absent and cross-run durations are the
        // documented ambiguity the file path exists to answer.
        let served = parsed(&report(addr, None, Some(&config_path)));
        assert_eq!(served.source.journal_runs, None);
        assert_eq!(served.rates.activations, 6);
        assert_eq!(served.rates.annunciations, 4);
    });

    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn a_missing_journal_file_fails_by_name() {
    let dir = scratch("missing");
    let driver = StubDriver::new(&[
        (PV1, Value::Float(50.0)),
        (PV2, Value::Float(50.0)),
        (PV3, Value::Float(50.0)),
        (SUPPRESS2, Value::Bool(false)),
        (ALARM1, Value::Bool(false)),
        (UNACK1, Value::Bool(false)),
        (SHELVED1, Value::Bool(false)),
        (SUPPRESSED1, Value::Bool(false)),
        (OOS1, Value::Bool(false)),
        (ALARM2, Value::Bool(false)),
        (UNACK2, Value::Bool(false)),
        (SHELVED2, Value::Bool(false)),
        (SUPPRESSED2, Value::Bool(false)),
        (OOS2, Value::Bool(false)),
        (ALARM3, Value::Bool(false)),
        (UNACK3, Value::Bool(false)),
        (SHELVED3, Value::Bool(false)),
        (SUPPRESSED3, Value::Bool(false)),
        (OOS3, Value::Bool(false)),
    ]);
    let journal_path = dir.join("serving.jsonl");
    serve(&driver, &journal_path, |client, addr| {
        client.advance(1).unwrap();
        // A review tool asked for a file that does not exist has
        // nothing to report on — the failure names it and exits
        // nonzero rather than reporting over the cold start.
        let missing = dir.join("absent.jsonl");
        let output = report(addr, Some(&missing), None);
        assert!(!output.status.success());
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(stderr.contains(missing.to_str().unwrap()), "{stderr}");

        // And the usage surface: no address, or a malformed config,
        // fail the same way — by name, never a panic.
        let output = Process::new(REPORT).output().unwrap();
        assert!(!output.status.success());
        let config_path = dir.join("bad.json");
        std::fs::write(&config_path, "{\"flood_alarms\": \"many\"}").unwrap();
        let output = report(addr, None, Some(&config_path));
        assert!(!output.status.success());
        assert!(
            String::from_utf8_lossy(&output.stderr).contains("report config"),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    });
    let _ = std::fs::remove_dir_all(&dir);
}
