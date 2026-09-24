//! A bounded queue drained by a dedicated writer thread — the
//! mechanism the journal-append isolation decision (issue #942,
//! adopting unmanaged finding #546) lands for the durable journal
//! sink, and the seam the durable-history store drain (#907) reuses.
//!
//! The problem the decision records: a durable sink's append used to
//! run synchronously at the journal's recording point — inside the
//! monitor's shared executor lock — so disk or share latency lengthened
//! every scan and pinned the lock the whole control plane serializes
//! on, the no-serialization rule's violation for the deployment's most
//! important durability output.
//!
//! A [`Drain`] splits the recording point in two: the producing side —
//! the recorder under the executor lock — hands each record to a
//! bounded channel through a non-blocking `try_send` that can never
//! wait on the sink, while one writer thread receives the records in
//! order and runs the sink's real append. The queue's `capacity` is
//! the declared bound: a producer finding it full — the sink stalled
//! or slower than the run's recording rate — is refused at the push,
//! which the journal sink answers with the fatal-on-append-failure
//! rule it has always applied, now at the handoff instead of the file
//! write. A sink whose write itself errors records the failure into
//! the shared health, consumes and counts the queue's remainder as
//! `lost` — the honest loss accounting — and every later push is
//! refused naming it, so the run fails at the recorded point rather
//! than claiming journaled entries the file never took.
//!
//! Ordering is the channel's FIFO: a single producer's records reach
//! the writer in push order, which for the journal is `seq` order —
//! the file's gap-free continuity is preserved, and a failed write
//! ends the record at the last durably appended `seq` rather than a
//! torn or reordered one. Dropping the drain closes the channel and
//! joins the writer, so a graceful shutdown's file is complete through
//! the last accepted record; a killed process simply ends the file
//! where the writer had reached, which replay reads as the run's
//! recorded tail.
//!
//! The health surface — [`DrainShared::health`] — is the named
//! degraded/backpressure state the decision requires: `Healthy` while
//! the writer keeps up, `Lagging` while records wait in the queue, and
//! `Failed` after a sink error, with `lost` accounting what the file
//! never held. The store stamps it into each publication and a
//! durability-attesting answer waits on [`DrainShared::wait_drained`],
//! both off the executor lock.

use std::fmt;
use std::io;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{SyncSender, TrySendError, sync_channel};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

/// The drain's standing health state — the named backpressure
/// condition the producing side never waits out.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum DrainState {
    /// The writer keeps up — no record waits in the queue.
    Healthy,
    /// Records wait for the writer — the sink is behind the run's
    /// recording rate; the queue's bound is where a continued lag
    /// turns fatal at the push.
    Lagging,
    /// A sink write failed — every further push is refused and `lost`
    /// accounts the accepted records the sink never took.
    Failed,
}

/// One read of the drain's counters — the health report the store
/// stamps into each publication and the flush a durability-attesting
/// answer waits on.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct DrainHealth {
    /// The standing state.
    pub(crate) state: DrainState,
    /// Records the producing side handed to the queue.
    pub(crate) accepted: u64,
    /// Records the writer durably appended.
    pub(crate) drained: u64,
    /// Records the queue admitted but the sink never appended —
    /// nonzero only after a failure: the honest loss accounting.
    pub(crate) lost: u64,
    /// Records waiting in the queue now.
    pub(crate) depth: u64,
    /// The deepest the queue has run — approximate: the producers
    /// sample it without serializing.
    pub(crate) high_water: u64,
    /// The queue's configured bound — a push finding it full is
    /// refused.
    pub(crate) capacity: u64,
}

/// The counters producer and writer share — an `Arc` of it is what a
/// monitor hands its publication store and its durability-attesting
/// flush, so neither side ever waits on the writer's lock.
pub(crate) struct DrainShared {
    /// The queue's configured bound.
    capacity: u64,
    /// Records the producer handed to the queue.
    accepted: AtomicU64,
    /// Records the writer took off the queue — queued or appended.
    taken: AtomicU64,
    /// Records the sink durably took.
    drained: AtomicU64,
    /// Records admitted but never appended — counted on a writer
    /// failure, which consumes the standing queue as accounted loss.
    lost: AtomicU64,
    /// The deepest `accepted - taken` a push observed — a diagnostic
    /// sampled lock-free, so approximate under concurrency.
    high_water: AtomicU64,
    /// The first sink error — set before the failure's accounting
    /// runs and read by producers as the refuse-everything-else flag.
    failure: Mutex<Option<String>>,
}

impl DrainShared {
    /// The first sink error the writer recorded, if any.
    fn failed(&self) -> Option<String> {
        self.failure.lock().unwrap().clone()
    }

    /// The drain's counters as they stand now.
    pub(crate) fn health(&self) -> DrainHealth {
        let accepted = self.accepted.load(Ordering::SeqCst);
        let taken = self.taken.load(Ordering::SeqCst);
        DrainHealth {
            state: if self.failed().is_some() {
                DrainState::Failed
            } else if accepted > taken {
                DrainState::Lagging
            } else {
                DrainState::Healthy
            },
            accepted,
            drained: self.drained.load(Ordering::SeqCst),
            lost: self.lost.load(Ordering::SeqCst),
            depth: accepted - taken,
            high_water: self.high_water.load(Ordering::SeqCst),
            capacity: self.capacity,
        }
    }

    /// Waits — at most `timeout` — for every accepted record to be
    /// drained or accounted lost, returning the standing health
    /// either way: `drained + lost == accepted` says the sink caught
    /// up. The wait is the caller's own, never the producing side's —
    /// a durability-attesting answer or a graceful shutdown runs it.
    pub(crate) fn wait_drained(&self, timeout: Duration) -> DrainHealth {
        let deadline = Instant::now() + timeout;
        loop {
            let health = self.health();
            if health.drained + health.lost >= health.accepted || Instant::now() >= deadline {
                return health;
            }
            thread::sleep(Duration::from_millis(1).min(deadline - Instant::now()));
        }
    }
}

/// A push the queue refused — the record comes back with it so the
/// caller's accounting decides the consequence; `message` names the
/// drain and the cause for the fatal report the journal applies.
#[derive(Debug)]
pub(crate) struct DrainError<T> {
    /// The record the queue refused.
    pub(crate) record: T,
    /// The refusal, named for the caller's report.
    pub(crate) message: String,
}

impl<T> fmt::Display for DrainError<T> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.message)
    }
}

/// One bounded producer→writer channel: `push` never blocks, the
/// writer thread appends in push order, and drop drains the standing
/// queue and joins the writer.
///
/// Generic over the record and the sink's append — `FnMut(&T) ->
/// io::Result<()>` — so the same machinery serves any sink whose
/// writes must leave the executor lock's path; the journal file is
/// the first, the durable-history store drain (#907) the next.
pub(crate) struct Drain<T> {
    /// The producing side's queue handle — `Option` so `Drop` can
    /// close the channel before joining the writer.
    queue: Option<SyncSender<T>>,
    /// The shared counters — cloned into the publication store's
    /// probe and the flush path's wait.
    shared: Arc<DrainShared>,
    /// What a refusal names — e.g. `journal file <path>` — so a
    /// producer-side error reads like the sink's own.
    label: String,
    /// The writer thread — joined on drop after the channel closes.
    writer: Option<JoinHandle<()>>,
}

impl<T: Send + 'static> Drain<T> {
    /// Spawns the drain's writer thread running `write` over each
    /// received record in order; `capacity` is the queue's declared
    /// bound and `label` what a refusal names.
    pub(crate) fn new(
        label: String,
        capacity: usize,
        mut write: impl FnMut(&T) -> io::Result<()> + Send + 'static,
    ) -> Self {
        let (sender, receiver) = sync_channel::<T>(capacity);
        let shared = Arc::new(DrainShared {
            capacity: capacity as u64,
            accepted: AtomicU64::new(0),
            taken: AtomicU64::new(0),
            drained: AtomicU64::new(0),
            lost: AtomicU64::new(0),
            high_water: AtomicU64::new(0),
            failure: Mutex::new(None),
        });
        let writer_shared = Arc::clone(&shared);
        let writer_label = label.clone();
        let writer = thread::Builder::new()
            .name("dcs-drain".to_string())
            .spawn(move || {
                // The channel's FIFO is the record's ordering: for the
                // journal it is `seq` order, so the file's continuity
                // is the queue's. A recorded failure turns the
                // standing queue into accounted loss — the writer
                // consumes the rest without writing so the counters
                // settle and the producers' refusals keep naming the
                // first error.
                while let Ok(record) = receiver.recv() {
                    writer_shared.taken.fetch_add(1, Ordering::SeqCst);
                    if writer_shared.failed().is_some() {
                        writer_shared.lost.fetch_add(1, Ordering::SeqCst);
                        continue;
                    }
                    let outcome =
                        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| write(&record)));
                    let message = match outcome {
                        Ok(Ok(())) => {
                            writer_shared.drained.fetch_add(1, Ordering::SeqCst);
                            continue;
                        }
                        Ok(Err(error)) => error.to_string(),
                        // A panicking append counts like a failing one:
                        // the record is lost and the failure named so
                        // producers refuse at the push.
                        Err(_) => format!("{writer_label}: the sink writer panicked mid-append"),
                    };
                    writer_shared.lost.fetch_add(1, Ordering::SeqCst);
                    *writer_shared.failure.lock().unwrap() = Some(message);
                }
            })
            .expect("the drain writer thread spawns");
        Self {
            queue: Some(sender),
            shared,
            label,
            writer: Some(writer),
        }
    }

    /// Hands `record` to the writer's queue without ever waiting on
    /// the sink: `Ok` once the record is queued — the writer's order
    /// is the push order — or the refusal the run turns fatal: a full
    /// queue (the sink fell past the declared bound), a recorded sink
    /// failure, or a gone writer.
    pub(crate) fn push(&self, record: T) -> Result<(), DrainError<T>> {
        if let Some(message) = self.shared.failed() {
            return Err(DrainError { record, message });
        }
        self.shared.accepted.fetch_add(1, Ordering::SeqCst);
        match self.queue.as_ref().unwrap().try_send(record) {
            Ok(()) => {
                let depth = self.shared.accepted.load(Ordering::SeqCst)
                    - self.shared.taken.load(Ordering::SeqCst);
                self.shared.high_water.fetch_max(depth, Ordering::SeqCst);
                Ok(())
            }
            Err(TrySendError::Full(record)) => {
                self.shared.accepted.fetch_sub(1, Ordering::SeqCst);
                Err(DrainError {
                    record,
                    message: format!(
                        "cannot append to {}: the drain queue's bound ({}) is full — \
                         the sink fell behind the run's recording and the journal \
                         cannot lose an entry unaccounted",
                        self.label, self.shared.capacity
                    ),
                })
            }
            Err(TrySendError::Disconnected(record)) => {
                self.shared.accepted.fetch_sub(1, Ordering::SeqCst);
                let message = self.shared.failed().unwrap_or_else(|| {
                    format!("cannot append to {}: the sink writer is gone", self.label)
                });
                Err(DrainError { record, message })
            }
        }
    }

    /// The drain's shared counters — what the publication store and
    /// the durability-attesting flush read.
    pub(crate) fn shared(&self) -> Arc<DrainShared> {
        Arc::clone(&self.shared)
    }
}

impl<T> Drop for Drain<T> {
    /// Closes the channel so the writer drains every queued record and
    /// exits, then joins it — a graceful drop leaves the sink complete
    /// through the last accepted record. A writer still inside a
    /// stalled write keeps the join waiting: the same wait the sink
    /// would have owed the run.
    fn drop(&mut self) {
        drop(self.queue.take());
        if let Some(writer) = self.writer.take() {
            let _ = writer.join();
        }
    }
}
