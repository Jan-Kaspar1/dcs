//! The `--state-file` checkpoint sink — the state-file persist
//! isolation fix (#982), the same non-interference class the journal
//! sink's drain landed for #942 (adopting unmanaged finding #546).
//!
//! Before this fix the checkpoint write ran synchronously at its
//! capture points — once per completed scan cycle and again at every
//! accepted command's admission boundary — inside the monitor's shared
//! executor lock, so a slow or stalled sink lengthened every scan and
//! pinned the serving lane behind disk I/O, the no-serialization
//! rule's violation for the deployment's recovery-state output.
//!
//! [`StateSink`] splits the boundary in two: the capture rides the
//! executor lock exactly where it always did — after the scan's
//! recording, or inside the accepted admission before its receipt is
//! promised — while the persist itself (serialize, temporary-file
//! write, atomic rename) runs on the sink's dedicated writer behind a
//! [`Drain`]'s bounded queue. The producing side's [`StateSink::offer`]
//! is a non-blocking handoff: it returns the pushed checkpoint's
//! ordinal, and a queue full past its declared bound — or a writer
//! already carrying a recorded failure — refuses at the push naming
//! the file, the run's fatal point under the fatal-on-write-failure
//! convention.
//!
//! Ordering is the drain's FIFO on a single producer — every capture
//! is pushed under the shared lock in the order it was taken, and the
//! writer replaces the file in that order: a newer checkpoint can
//! never be overwritten by an older one, so the durable file never
//! regresses to stale state. A write that fails on the writer is
//! recorded into the drain's health — `failed` with the error's name
//! and `lost` accounting the captures the file never took — and every
//! later push refuses naming it.
//!
//! Where a caller must know the file caught up before answering — the
//! accepted `POST /command` receipt, the driven `POST /scan` answer,
//! the graceful exit's last checkpoint — [`StateSink::attest`] waits
//! the writer out on the caller's own thread, bounded by
//! [`STATE_DRAIN_WAIT`] at the serving layer, never the executor
//! lock. A wait that cannot attest fails by name.
//!
//! The file format is the serde `Checkpoint` document it always was:
//! serialized once per pushed capture and atomically renamed over the
//! path, so a crash mid-write leaves at worst a sibling `.tmp`
//! remainder, never a torn target the next resume would have to
//! reject.

use crate::drain::{Drain, DrainShared};
use dcs_runtime::Checkpoint;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

/// The default bound on the state-file sink's queue: captured
/// checkpoints may pile up at most this deep before a push refuses —
/// the `lagging`→fatal boundary. The bound must absorb the burst the
/// producing side can emit between writer beats: an admission wave is
/// bounded by the executor's own command-queue capacity, and a driven
/// `POST /scan` batch pushes once per requested scan, so the bound
/// sits at the command queue's own default. A sink stalled past it —
/// disk hung longer than sixty-four captures' worth of run — is the
/// named failure the pushes refuse on, not a scan the run lengthens.
pub const DEFAULT_STATE_DRAIN_CAPACITY: usize = 64;

/// The bound a durability-attesting answer gives the state sink's
/// writer — how long `POST /command`'s accepted admission, a driven
/// `POST /scan`'s last capture, or a graceful exit waits for the drain
/// before naming the failure. The wait is the caller's own, off the
/// executor lock; a healthy sink drains in microseconds, so reaching
/// the bound means the writer is stalled — and the queue's own bound
/// has already made the run's pushes fatal rather than let the file
/// trail silently. The journal sink's attestation wait shares the
/// shape and the value.
pub const STATE_DRAIN_WAIT: Duration = Duration::from_secs(30);

/// The `--state-file` sink: a bounded queue of captured [`Checkpoint`]s
/// drained by a dedicated writer that serializes each and atomically
/// replaces the file — the capture under the executor lock, the file
/// I/O off it.
pub struct StateSink {
    /// What a refusal or an attestation failure names — `state file
    /// <path>` — so the fatal report reads like the sink's own.
    label: String,
    /// The bounded queue and dedicated writer the captures drain
    /// through — see [`Drain`].
    drain: Drain<Checkpoint>,
}

impl StateSink {
    /// Spawns the sink's writer thread persisting `path`, `capacity`
    /// the queue's declared bound. Each queued checkpoint serializes
    /// and replaces the file by write-then-rename — atomic on one
    /// filesystem — strictly in push order.
    pub fn new(path: &Path, capacity: usize) -> Self {
        let label = format!("state file {}", path.display());
        let sink_path = path.to_path_buf();
        Self {
            drain: Drain::new(label.clone(), capacity, move |checkpoint| {
                write_state_file(&sink_path, checkpoint)
            }),
            label,
        }
    }

    /// Hands `checkpoint` to the writer's queue without ever waiting
    /// on the sink — `Ok(ordinal)`, the capture's position in the
    /// writer's push order and [`attest`](Self::attest)'s wait target,
    /// or `Err` naming the refusal the run turns fatal: the queue full
    /// past its declared bound, or a recorded writer failure. Callers
    /// serialize the capture-and-push under the executor lock, so the
    /// queue's order is the state the run actually passed through.
    pub fn offer(&self, checkpoint: Checkpoint) -> Result<u64, String> {
        self.drain.push(checkpoint).map_err(|error| error.message)
    }

    /// Waits — at most `timeout`, on the caller's thread alone — for
    /// the writer to have replaced the file with the capture `offer`
    /// numbered `ordinal`, or accounted it lost: `Ok` only when that
    /// capture durably landed. `Err` names the sink's recorded write
    /// failure, or the bound a stalled writer overran — the caller
    /// that promised durability fails by name rather than answering
    /// it. The wait never touches the executor lock.
    pub(crate) fn attest(&self, ordinal: u64, timeout: Duration) -> Result<(), String> {
        let health = self.drain.shared().wait_through(ordinal, timeout);
        if health.drained >= ordinal {
            return Ok(());
        }
        match self.drain.shared().failed() {
            Some(failure) => Err(failure),
            None => Err(format!(
                "cannot write {}: the sink did not reach the attested \
                 checkpoint within {timeout:?} — the writer is stalled",
                self.label
            )),
        }
    }

    /// The drain's shared counters — what the publication store stamps
    /// into every snapshot's `publication.state_sink` section and the
    /// monitor's live health read returns.
    pub(crate) fn shared(&self) -> Arc<DrainShared> {
        self.drain.shared()
    }

    /// Test seam: wrap a constructed drain — the stalled- and
    /// failing-writer coverage drives the writer through closures a
    /// real file cannot produce deterministically, the same seam the
    /// journal sink's tests use.
    #[cfg(test)]
    pub(crate) fn for_test(label: String, drain: Drain<Checkpoint>) -> Self {
        Self { label, drain }
    }
}

/// Persists `checkpoint` as `path`'s new contents: serialize, write to
/// a sibling temporary file, then rename over `path` — atomic on one
/// filesystem, so a crash mid-write never leaves a torn file the next
/// resume would have to reject. The checkpoint's serde document is
/// the format the controller's `--state-file` has always written —
/// unchanged.
fn write_state_file(path: &Path, checkpoint: &Checkpoint) -> io::Result<()> {
    let body = serde_json::to_vec(checkpoint)
        .map_err(|error| io::Error::other(format!("cannot serialize checkpoint: {error}")))?;
    let mut temporary = path.as_os_str().to_os_string();
    temporary.push(".tmp");
    let temporary = PathBuf::from(temporary);
    std::fs::write(&temporary, body)
        .and_then(|()| std::fs::rename(&temporary, path))
        .map_err(|error| {
            io::Error::new(
                error.kind(),
                format!("cannot write state file {}: {error}", path.display()),
            )
        })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::drain::DrainState;
    use dcs_core::Tick;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{Condvar, Mutex};
    use std::time::Instant;

    /// A scratch directory per test and process — tests run in
    /// parallel.
    fn scratch(test: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("dcs-state-file-{test}-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    /// A checkpoint carrying just enough identity for these tests —
    /// `tick` distinguishes the captures the ordering coverage asserts
    /// on.
    fn checkpoint(tick: u64) -> Checkpoint {
        Checkpoint {
            format_version: dcs_runtime::CHECKPOINT_FORMAT_VERSION,
            model_fingerprint: None,
            generation: None,
            tick: Tick(tick),
            components: Default::default(),
            driver: None,
            outputs: Default::default(),
            internal: Default::default(),
            forces: Default::default(),
            receipts: Vec::new(),
            command_admission: Default::default(),
            source_owns_field: None,
            line_owner: None,
            line_proof: None,
        }
    }

    /// The file's checkpoint, parsed back — the format the resume path
    /// reads.
    fn read_checkpoint(path: &Path) -> Checkpoint {
        serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap()
    }

    /// Polls `condition` until it holds or `deadline` elapses — the
    /// sink's writer runs concurrently with the test, so its effects
    /// arrive asynchronously.
    fn wait_until(mut condition: impl FnMut() -> bool, what: &str) {
        let deadline = Instant::now() + Duration::from_secs(10);
        while !condition() {
            assert!(Instant::now() < deadline, "timed out waiting for {what}");
            std::thread::sleep(Duration::from_millis(1));
        }
    }

    #[test]
    fn a_pushed_checkpoint_replaces_the_file_attested() {
        let dir = scratch("roundtrip");
        let path = dir.join("state.json");
        let sink = StateSink::new(&path, 8);

        let ordinal = sink.offer(checkpoint(7)).unwrap();
        sink.attest(ordinal, Duration::from_secs(10)).unwrap();
        assert_eq!(read_checkpoint(&path).tick, Tick(7));

        // The temporary sibling never lingers past the rename.
        let temporary = {
            let mut temporary = path.as_os_str().to_os_string();
            temporary.push(".tmp");
            PathBuf::from(temporary)
        };
        assert!(!temporary.exists());
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// #982's ordering contract: the writer replaces the file in push
    /// order — the FIFO the lock-serialized captures ride — so a newer
    /// checkpoint can never be overwritten by an older one.
    #[test]
    fn checkpoints_replace_the_file_in_push_order() {
        // The writer logs each replace's tick instead of touching a
        // file — the assertion is the pushes' order surviving to it.
        let written = Arc::new(Mutex::new(Vec::new()));
        let drain = Drain::new("state file test".to_string(), 8, {
            let written = Arc::clone(&written);
            move |checkpoint: &Checkpoint| {
                written.lock().unwrap().push(checkpoint.tick);
                Ok(())
            }
        });
        let sink = StateSink::for_test("state file test".to_string(), drain);

        let mut last = None;
        for tick in 1..=5_u64 {
            last = Some(sink.offer(checkpoint(tick)).unwrap());
        }
        sink.attest(last.unwrap(), Duration::from_secs(10)).unwrap();
        assert_eq!(
            *written.lock().unwrap(),
            (1..=5_u64).map(Tick).collect::<Vec<_>>(),
            "the writer's replace order is the pushes' order"
        );
    }

    /// #982's stalled sink: a writer parked inside its write drains
    /// nothing — yet `offer` keeps returning, because the handoff is a
    /// bounded queue's non-blocking send, not the file's write. The
    /// queue's declared capacity is the bound: a push finding it full
    /// is refused at the recording point, naming the sink — and the
    /// standing health reports `lagging` by name while the queue
    /// holds. Once the writer resumes, the queued captures replace the
    /// file in order and the file ends at the last pushed checkpoint.
    #[test]
    fn a_stalled_sink_never_blocks_an_offer_and_the_full_queue_refuses() {
        let dir = scratch("stalled-sink");
        let path = dir.join("state.json");
        // The writer parks inside every write until the gate opens —
        // a stalled share's stand-in — and `entered` lets the test
        // wait for the writer to be mid-write before filling the
        // queue behind it.
        let gate = Arc::new((Mutex::new(false), Condvar::new()));
        let entered = Arc::new(AtomicU64::new(0));
        let drain = Drain::new(format!("state file {}", path.display()), 2, {
            let gate = Arc::clone(&gate);
            let entered = Arc::clone(&entered);
            let sink_path = path.clone();
            move |checkpoint| {
                entered.fetch_add(1, Ordering::SeqCst);
                let (lock, cvar) = &*gate;
                let mut open = lock.lock().unwrap();
                while !*open {
                    open = cvar.wait(open).unwrap();
                }
                drop(open);
                write_state_file(&sink_path, checkpoint)
            }
        });
        let sink = StateSink::for_test(format!("state file {}", path.display()), drain);

        // The first capture reaches the writer, which parks inside
        // its write; the queue then holds two more. Every offer still
        // returns — nothing in the recording path waits on the sink.
        sink.offer(checkpoint(1)).unwrap();
        wait_until(
            || entered.load(Ordering::SeqCst) >= 1,
            "the drain writer to take the first checkpoint",
        );
        let pushed = Instant::now();
        sink.offer(checkpoint(2)).unwrap();
        let third = sink.offer(checkpoint(3)).unwrap();
        assert!(
            pushed.elapsed() < Duration::from_secs(1),
            "the offers waited on the sink: the lock's hold would stretch with it"
        );
        assert_eq!(
            sink.shared().health().state,
            DrainState::Lagging,
            "the sink's lag is the named health state while the queue holds captures"
        );

        // The next capture meets the full queue: the offer is refused
        // at the recording point — the error names the file and the
        // bound.
        let error = sink
            .offer(checkpoint(4))
            .expect_err("a full drain queue must refuse the offer");
        assert!(error.contains("state file"), "{error}");
        assert!(error.contains(path.to_str().unwrap()), "{error}");

        // Releasing the sink drains the standing queue in order: the
        // file lands the run's last pushed checkpoint — the refused
        // fourth simply never was.
        let (lock, cvar) = &*gate;
        *lock.lock().unwrap() = true;
        cvar.notify_all();
        sink.attest(third, Duration::from_secs(10)).unwrap();
        assert_eq!(
            read_checkpoint(&path).tick,
            Tick(3),
            "the durable file holds the last pushed capture, never a stale overwrite"
        );
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// #982's failing sink: a write error on the writer records the
    /// named failure — health `failed`, the queued remainder accounted
    /// `lost` — and every later offer is refused naming it, so the run
    /// fails at the recorded point. The file itself ends at the last
    /// durably replaced checkpoint: the write-then-rename never leaves
    /// a torn target the next resume would have to reject, and an
    /// attestation reaching the failed record answers the error by
    /// name.
    #[test]
    fn a_failing_sink_is_named_at_the_recorded_point_without_a_torn_file() {
        let dir = scratch("failing-sink");
        let path = dir.join("state.json");
        // The sink's second write fails — before any byte lands, so
        // no torn file is possible.
        let writes = Arc::new(AtomicU64::new(0));
        let fail_path = path.clone();
        let drain = Drain::new(
            format!("state file {}", path.display()),
            8,
            move |checkpoint| {
                if writes.fetch_add(1, Ordering::SeqCst) >= 1 {
                    return Err(io::Error::other(format!(
                        "cannot write state file {}: the simulated sink refuses",
                        fail_path.display()
                    )));
                }
                write_state_file(&fail_path, checkpoint)
            },
        );
        let sink = StateSink::for_test(format!("state file {}", path.display()), drain);

        // The first capture lands; the second's write fails. Which
        // offer first meets the recorded failure races the writer, so
        // offers may already be refused — the accounting is what must
        // hold.
        sink.offer(checkpoint(1)).unwrap();
        let second = sink.offer(checkpoint(2));
        for tick in 3..=4_u64 {
            let _ = sink.offer(checkpoint(tick));
        }
        wait_until(
            || sink.shared().health().state == DrainState::Failed,
            "the sink's failure to surface in the drain's health",
        );
        let health = sink.shared().health();
        assert_eq!(health.drained, 1, "one capture reached the file");
        assert!(
            health.lost >= 1,
            "the failed write's captures are accounted lost"
        );

        // Every offer after the recorded failure is refused — the run
        // dies at its next persist naming the file's error.
        let error = sink
            .offer(checkpoint(9))
            .expect_err("a failed sink must refuse the offer");
        assert!(error.contains(path.to_str().unwrap()), "{error}");
        assert!(error.contains("the simulated sink refuses"), "{error}");

        // The attestation that could no longer be honored names the
        // recorded write error — the durability wait's failure is the
        // file's own.
        if let Ok(ordinal) = second {
            let error = sink
                .attest(ordinal, Duration::from_secs(10))
                .expect_err("attesting a lost capture must fail");
            assert!(error.contains("the simulated sink refuses"), "{error}");
        }

        // The durable file ends cleanly at the replaced capture —
        // never a torn document.
        assert_eq!(read_checkpoint(&path).tick, Tick(1));
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// The ordinal-scoped attestation: `attest` waits only through the
    /// pushed record it was handed — a later capture still queued or
    /// mid-write cannot stretch it.
    #[test]
    fn attest_waits_through_its_own_ordinal_alone() {
        let dir = scratch("attest");
        let path = dir.join("state.json");
        // The writer parks on every write past the first: the first
        // capture lands, the second holds the writer while the test
        // attests the first's ordinal.
        let gate = Arc::new((Mutex::new(false), Condvar::new()));
        let writes = Arc::new(AtomicU64::new(0));
        let drain = Drain::new(format!("state file {}", path.display()), 4, {
            let gate = Arc::clone(&gate);
            let writes = Arc::clone(&writes);
            let sink_path = path.clone();
            move |checkpoint| {
                if writes.fetch_add(1, Ordering::SeqCst) >= 1 {
                    let (lock, cvar) = &*gate;
                    let mut open = lock.lock().unwrap();
                    while !*open {
                        open = cvar.wait(open).unwrap();
                    }
                }
                write_state_file(&sink_path, checkpoint)
            }
        });
        let sink = StateSink::for_test(format!("state file {}", path.display()), drain);

        let first = sink.offer(checkpoint(1)).unwrap();
        sink.offer(checkpoint(2)).unwrap();
        wait_until(
            || writes.load(Ordering::SeqCst) >= 2,
            "the writer to park on the second checkpoint",
        );
        // The parked second capture cannot stretch the first's
        // attestation.
        sink.attest(first, Duration::from_secs(10)).unwrap();
        assert_eq!(read_checkpoint(&path).tick, Tick(1));

        let (lock, cvar) = &*gate;
        *lock.lock().unwrap() = true;
        cvar.notify_all();
        let _ = std::fs::remove_dir_all(&dir);
    }

    /// #982 at the monitor boundary: a state sink parked mid-write
    /// cannot lengthen the scan path — `paced_scan` never touches the
    /// sink and `persist_state` only queues the capture — while the
    /// monitor's standing health reports the stall as `lagging` by
    /// name; once the queue fills past its declared bound the next
    /// persist refuses by name, the run's fatal point, instead of
    /// letting the file trail the run unaccounted.
    #[test]
    fn a_stalled_sink_cannot_lengthen_the_monitors_scan_path() {
        use crate::drain::Drain;
        use crate::{Monitor, MonitorConfig};
        use dcs_core::{Direction, IoDriver, IoError, PointId, Sample, Value, ValueKind};
        use dcs_runtime::{Executor, PointMap};
        use std::collections::HashMap;

        /// The same minimal in-memory driver the monitor tests use.
        struct StubDriver {
            points: Mutex<HashMap<PointId, Sample>>,
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
                *sample = Sample::good(value, Tick::ZERO);
                Ok(())
            }
        }

        let driver = StubDriver {
            points: Mutex::new(
                [(PointId(10), Sample::good(Value::Float(0.0), Tick::ZERO))]
                    .into_iter()
                    .collect(),
            ),
        };
        let map = PointMap::new().with_point(PointId(10), Direction::In, ValueKind::Float);
        let executor = Executor::new(&driver, map, Vec::new()).unwrap();
        let signals = dcs_model::PlantModel::load(include_str!("../fixtures/monitor.json"))
            .unwrap()
            .signal_index();
        let mut monitor =
            Monitor::bind_with("127.0.0.1:0", executor, signals, MonitorConfig::default()).unwrap();
        // The sink's writer parks inside its write — a stalled share's
        // stand-in — behind a queue bounded at two captures.
        let gate = Arc::new((Mutex::new(false), Condvar::new()));
        let entered = Arc::new(AtomicU64::new(0));
        let drain = Drain::new("state file test".to_string(), 2, {
            let gate = Arc::clone(&gate);
            let entered = Arc::clone(&entered);
            move |_checkpoint| {
                entered.fetch_add(1, Ordering::SeqCst);
                let (lock, cvar) = &*gate;
                let mut open = lock.lock().unwrap();
                while !*open {
                    open = cvar.wait(open).unwrap();
                }
                Ok(())
            }
        });
        monitor.with_state_sink(StateSink::for_test("state file test".to_string(), drain));

        // One capture parks the writer; the queue then holds two more —
        // the scan path never waits on any of it.
        let started = Instant::now();
        monitor.paced_scan();
        monitor.persist_state().unwrap();
        wait_until(
            || entered.load(Ordering::SeqCst) >= 1,
            "the drain writer to take the first checkpoint",
        );
        for _ in 0..2 {
            monitor.paced_scan();
            monitor.persist_state().unwrap();
        }
        assert!(
            started.elapsed() < Duration::from_secs(1),
            "the scan path waited on the stalled sink"
        );
        assert_eq!(
            monitor.state_sink_health().unwrap().state,
            dcs_core::StateSinkState::Lagging,
            "the sink's lag reports by name while the queue holds captures"
        );

        // The next persist meets the full queue: refused at the
        // recording point — the caller's fatal — naming the sink.
        let error = monitor
            .persist_state()
            .expect_err("a full drain queue must refuse the persist");
        assert!(error.contains("state file"), "{error}");

        let (lock, cvar) = &*gate;
        *lock.lock().unwrap() = true;
        cvar.notify_all();
    }
}
