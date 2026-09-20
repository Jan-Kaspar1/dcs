//! The bounded read-model store a monitor publishes each completed scan
//! into — the read side of the decision that the controller owns
//! execution while monitoring/UI delivery is a bounded, disposable
//! consumer: publications live outside the executor lock, so a slow,
//! stalled, or absent reader can never reach the scan.
//!
//! [`Store`] holds the read-side streams under one small lock of its
//! own — taken only for a fetch or a swap, never held across socket I/O
//! or response serialization:
//!
//! - the **latest publication** plus a bounded retained window of
//!   recent ones — one immutable [`Publication`] per completed scan,
//!   carrying the materialized [`TelemetrySnapshot`], the history and
//!   journal deltas appended since the previous publication, and the
//!   receipt log as of that scan;
//! - the **served history rings** — the per-point [`HistorySample`]
//!   retention `GET /history` reads;
//! - the **served journal** — the [`JournalEntry`] retention
//!   `GET /journal` reads, plus the pinned run-boundary markers the
//!   ring's bound would otherwise evict: a `run_boundary` entry is the
//!   semantic marker the run-attribution contract stands on, so aging
//!   one out of the tail migrates it to the pinned stream instead of
//!   dropping it — bounded by the process lifetimes the journal
//!   records, not by event volume;
//! - the **routed emission stores** — the bounded
//!   [`EventRecord`](dcs_core::EventRecord) ring `History`-declared
//!   emissions land in and the latest-emission view `Latest`-declared
//!   emissions stand in, both joined into `GET /resources`'s
//!   per-instance `events` beside the journal tail; and
//! - the **receipt mirror** — the latest-value copy `GET /receipts`
//!   answers, refreshed wherever the control-plane lock changes the
//!   log so a between-scans submission stays immediately visible.
//!
//! Event and history appends are incremental — a journaled
//! control-plane event between scans (a refused command, a role
//! change) reaches the served stream at once — while each
//! [`Publication`] is materialized once, at a completed scan's
//! boundary under the executor lock, then swapped in. Every stream is
//! bounded and every publication, sample, and entry carries a
//! never-reused `seq`: a consumer that falls behind the retained
//! window observes the numbering gap — the named [`PublicationGap`]
//! on a seq-cursor publication read, a skipped seq stretch on the
//! history and journal streams — or coalesces onto the latest state,
//! rather than ever backpressuring the run. With no consumers at all
//! the counters keep advancing while storage stays bounded; the
//! snapshot's `publication` section ([`PublicationHealth`]) reports
//! the store's overload accounting as of each publish.

use dcs_core::{
    CommandReceipt, EmittedEvent, EventRecord, EventRetention, HistorySample, JournalEntry,
    JournalEvent, PointHistory, PointId, PublicationHealth, Sample, TelemetrySnapshot, Tick,
};
use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::sync::{Arc, Mutex};

/// One immutable post-scan read model — the unit the store publishes.
///
/// A publication is materialized once, at its scan's boundary, and
/// never mutated: consumers hold `Arc`s to it while the store ages the
/// retained window forward, so a retained earlier publication reports
/// its scan's state exactly as published no matter how many scans
/// follow.
#[derive(Debug, PartialEq)]
pub struct Publication {
    /// The publication's monotonic sequence — assigned in publish
    /// order from 1 and never reused, so a seq-cursor consumer detects
    /// the window's evictions as a numbering gap.
    pub seq: u64,
    /// The scan tick the read model was materialized at — the
    /// publication's freshness metadata.
    pub tick: Tick,
    /// The materialized telemetry snapshot — the latest-value section.
    /// Its `publication` section reports the store's overload counters
    /// as of this publish.
    pub snapshot: TelemetrySnapshot,
    /// The command receipt log as of this publication — shared,
    /// immutable, with the store's latest-value mirror.
    pub receipts: Arc<Vec<CommandReceipt>>,
    /// The per-point history samples appended since the previous
    /// publication, in ascending point order — the history delta.
    pub history: Vec<PointHistory>,
    /// The journal entries appended since the previous publication —
    /// the event delta, including entries the control plane journaled
    /// between scans.
    pub journal: Vec<JournalEntry>,
    /// The routed emission records appended since the previous
    /// publication — the `History`/`Latest` classes' delta beside the
    /// journal's, in routed-append order.
    pub events: Vec<EventRecord>,
}

/// The named gap a lagging publication consumer observes: the seqs its
/// cursor would read next aged out of the retained window.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PublicationGap {
    /// The newest publication sequence no longer retained — the whole
    /// stretch through it is lost to this reader, which coalesces onto
    /// the retained tail or the latest publication instead of ever
    /// backpressuring the run.
    pub through: u64,
}

/// A seq-cursor read's page — what [`Monitor::publications_since`]
/// answers.
///
/// [`Monitor::publications_since`]: crate::Monitor::publications_since
#[derive(Debug, PartialEq)]
pub struct PublicationPage {
    /// Publications newer than the read cursor, oldest first — the
    /// retained tail when a gap stands.
    pub publications: Vec<Arc<Publication>>,
    /// The named gap: `Some` when publications the cursor would read
    /// next already aged out of the retained window.
    pub gap: Option<PublicationGap>,
}

/// One point's ring of recent samples.
struct Ring {
    /// The `seq` the next appended sample takes.
    next_seq: u64,
    samples: VecDeque<HistorySample>,
}

impl Ring {
    fn new() -> Self {
        Self {
            next_seq: 1,
            samples: VecDeque::new(),
        }
    }
}

struct Inner {
    /// The newest publication — what the snapshot read endpoints
    /// serve. `None` only before the bind-time publication a
    /// [`Monitor`](crate::Monitor) always performs.
    latest: Option<Arc<Publication>>,
    /// The bounded retained window of recent publications, oldest
    /// first — what a seq-cursor read pages through.
    window: VecDeque<Arc<Publication>>,
    /// Served history rings keyed by point id; the `BTreeMap` keeps
    /// `/history` output in ascending point order.
    rings: BTreeMap<PointId, Ring>,
    /// Served journal entries, oldest first.
    journal: VecDeque<JournalEntry>,
    /// The `run_boundary` entries the served ring's bound evicted,
    /// pinned aside rather than dropped: the markers are the
    /// run-attribution contract's only record that a new process
    /// lifetime began, so they outlive ordinary event volume. Every
    /// pinned entry precedes the ring's front in `seq`, and their
    /// count is bounded by the process lifetimes the journal records —
    /// one per run — not by `journal_capacity`.
    boundaries: VecDeque<JournalEntry>,
    /// The bounded event-history ring: `History`-retained emission
    /// records, oldest first — the diagnostic stream the resource
    /// view's `events` joins beside the journal tail.
    event_history: VecDeque<EventRecord>,
    /// The latest-emission view: the newest `Latest`-retained record
    /// per (component, event) identity — each newer emission
    /// supersedes the last. Bounded by the declared event surface:
    /// one entry per identity a kind declares.
    latest_events: BTreeMap<(String, String), EventRecord>,
    /// Routed emission records appended since the last publish — the
    /// next publication's event delta, drained there.
    pending_events: VecDeque<EventRecord>,
    /// The `seq` the next routed emission takes — one stream numbering
    /// both routed classes, never reused, so ring eviction reads as a
    /// numbering gap like the journal's.
    next_event_seq: u64,
    /// The latest receipt-log mirror — refreshed wherever the
    /// control-plane lock changes the log, so `GET /receipts` answers
    /// the log as it stands, not as of the last scan.
    receipts: Arc<Vec<CommandReceipt>>,
    /// History samples appended since the last publish — the next
    /// publication's history delta, drained there.
    pending_history: BTreeMap<PointId, VecDeque<HistorySample>>,
    /// Journal entries appended since the last publish — the next
    /// publication's event delta, drained there.
    pending_journal: VecDeque<JournalEntry>,
    /// The `run_boundary` entries the pending delta's bound evicted —
    /// the same pinning rule the served ring follows, so a publication
    /// delta keeps its lifetime markers under a between-scans flood.
    pending_boundaries: VecDeque<JournalEntry>,
    /// The `seq` the next publication takes — never reused.
    next_seq: u64,
    /// Publications produced since the store was created — the
    /// `published` overload counter.
    published: u64,
    /// Publications evicted from the retained window — the
    /// `coalesced` overload counter.
    coalesced: u64,
    /// The retained window's bound.
    window_capacity: usize,
    /// Per-point history retention bound.
    history_capacity: usize,
    /// Journal retention bound.
    journal_capacity: usize,
    /// Event-history retention bound — the `History`-retained ring's.
    event_history_capacity: usize,
}

/// Evicts `ring` down to `capacity` oldest-first, migrating each
/// evicted `run_boundary` marker into `pinned` instead of dropping it —
/// the pinning rule the served ring and the pending delta share. Every
/// migrated entry precedes `ring`'s front in `seq`, so `pinned` stays
/// `seq`-ordered and entirely older than the ring.
fn evict_journal(
    ring: &mut VecDeque<JournalEntry>,
    pinned: &mut VecDeque<JournalEntry>,
    capacity: usize,
) {
    while ring.len() > capacity {
        let evicted = ring.pop_front().unwrap();
        if matches!(evicted.event, JournalEvent::RunBoundary { .. }) {
            pinned.push_back(evicted);
        }
    }
}

impl Inner {
    /// Stamps `event` with the routed stream's next `seq` and `tick`,
    /// appends the record to the pending event delta — bounded like
    /// the ring — and returns it for the caller's store.
    fn route(&mut self, event: EmittedEvent, retention: EventRetention, tick: Tick) -> EventRecord {
        let record = EventRecord {
            seq: self.next_event_seq,
            tick,
            retention,
            event,
        };
        self.next_event_seq += 1;
        self.pending_events.push_back(record.clone());
        while self.pending_events.len() > self.event_history_capacity {
            self.pending_events.pop_front();
        }
        record
    }
}

/// The store's routed-event streams — what [`Store::routed_events`]
/// fetches for the resource view's per-instance `events` join.
#[derive(Debug, Default, Clone, PartialEq)]
pub(crate) struct RoutedEvents {
    /// The retained `History` ring's records, oldest first.
    pub history: Vec<EventRecord>,
    /// The `Latest` view's standing records — the newest emission per
    /// (component, event) identity — in identity order.
    pub latest: Vec<EventRecord>,
}

/// The publication store's shared handle — a monitor and its recorder
/// hold clones; every operation takes the one small inner lock for the
/// fetch, append, or swap and releases it before any serialization or
/// socket I/O.
#[derive(Clone)]
pub(crate) struct Store {
    inner: Arc<Mutex<Inner>>,
}

impl Store {
    /// An empty store with the given retention bounds — a
    /// [`Monitor`](crate::Monitor)'s bind publishes the seed read
    /// model immediately after, so a store without any publication
    /// exists only inside construction.
    pub(crate) fn new(
        history_capacity: usize,
        journal_capacity: usize,
        window_capacity: usize,
        event_history_capacity: usize,
    ) -> Self {
        Self {
            inner: Arc::new(Mutex::new(Inner {
                latest: None,
                window: VecDeque::new(),
                rings: BTreeMap::new(),
                journal: VecDeque::new(),
                boundaries: VecDeque::new(),
                event_history: VecDeque::new(),
                latest_events: BTreeMap::new(),
                pending_events: VecDeque::new(),
                next_event_seq: 1,
                receipts: Arc::new(Vec::new()),
                pending_history: BTreeMap::new(),
                pending_journal: VecDeque::new(),
                pending_boundaries: VecDeque::new(),
                next_seq: 1,
                published: 0,
                coalesced: 0,
                window_capacity,
                history_capacity,
                journal_capacity,
                event_history_capacity,
            })),
        }
    }

    /// Appends `point`'s fresh scan sample to its served ring and to
    /// the pending history delta the next publication drains. The
    /// sample's `seq` comes from the ring — never reused, so a
    /// `since`-cursor consumer detects eviction as a numbering gap.
    pub(crate) fn push_sample(&self, point: PointId, sample: Sample) {
        let mut inner = self.inner.lock().unwrap();
        let capacity = inner.history_capacity;
        let ring = inner.rings.entry(point).or_insert_with(Ring::new);
        let stamped = HistorySample {
            seq: ring.next_seq,
            sample,
        };
        ring.next_seq += 1;
        ring.samples.push_back(stamped);
        while ring.samples.len() > capacity {
            ring.samples.pop_front();
        }
        let pending = inner.pending_history.entry(point).or_default();
        pending.push_back(stamped);
        while pending.len() > capacity {
            pending.pop_front();
        }
    }

    /// Appends one journaled entry to the served ring and to the
    /// pending event delta the next publication drains — incremental,
    /// so a between-scans control-plane event reaches the served
    /// stream immediately. Both streams evict oldest-first past the
    /// bound, except a `run_boundary` marker never drops: eviction
    /// migrates it to the pinned stream its serve merges back in
    /// `seq` order.
    pub(crate) fn push_journal(&self, entry: JournalEntry) {
        let mut guard = self.inner.lock().unwrap();
        let inner = &mut *guard;
        inner.journal.push_back(entry.clone());
        evict_journal(
            &mut inner.journal,
            &mut inner.boundaries,
            inner.journal_capacity,
        );
        inner.pending_journal.push_back(entry);
        evict_journal(
            &mut inner.pending_journal,
            &mut inner.pending_boundaries,
            inner.journal_capacity,
        );
    }

    /// Routes a `History`-retained emission: stamped with the routed
    /// stream's next `seq` and the producing scan's `tick`, appended
    /// to the bounded event-history ring — evicting oldest-first past
    /// the declared bound — and to the pending event delta the next
    /// publication drains.
    pub(crate) fn push_event_history(&self, event: EmittedEvent, tick: Tick) {
        let mut inner = self.inner.lock().unwrap();
        let record = inner.route(event, EventRetention::History, tick);
        inner.event_history.push_back(record);
        while inner.event_history.len() > inner.event_history_capacity {
            inner.event_history.pop_front();
        }
    }

    /// Routes a `Latest`-retained emission: stamped like the ring's
    /// records and stood as the newest record for its (component,
    /// event) identity — superseding the previous one — also appended
    /// to the pending event delta the next publication drains.
    pub(crate) fn push_latest_event(&self, event: EmittedEvent, tick: Tick) {
        let mut inner = self.inner.lock().unwrap();
        let key = (event.component.clone(), event.event.clone());
        let record = inner.route(event, EventRetention::Latest, tick);
        inner.latest_events.insert(key, record);
    }

    /// The served routed-event streams in one fetch: the retained
    /// `History` ring, oldest first, and the `Latest` view — the
    /// newest standing record per (component, event) identity — keyed
    /// by identity order. The resource view joins both beside the
    /// journal tail.
    pub(crate) fn routed_events(&self) -> RoutedEvents {
        let inner = self.inner.lock().unwrap();
        RoutedEvents {
            history: inner.event_history.iter().cloned().collect(),
            latest: inner.latest_events.values().cloned().collect(),
        }
    }

    /// Refreshes the latest-value receipt mirror to `receipts` — called
    /// wherever the control-plane lock changes the log (a submission, a
    /// boundary settlement, a checkpoint-adopted log) so the
    /// `GET /receipts` answer stays current between scans.
    pub(crate) fn sync_receipts(&self, receipts: &[CommandReceipt]) {
        self.inner.lock().unwrap().receipts = Arc::new(receipts.to_vec());
    }

    /// Materializes and publishes one immutable read model for a
    /// completed scan: the snapshot — stamped with the store's overload
    /// counters as of this publish — plus the drained history and
    /// journal deltas and the receipt log as it stands. The new
    /// publication becomes `latest` and enters the retained window,
    /// evicting oldest-first past the bound; evictions count into the
    /// `coalesced` overload counter a lagging consumer observes as the
    /// named gap.
    pub(crate) fn publish(
        &self,
        tick: Tick,
        mut snapshot: TelemetrySnapshot,
        receipts: &[CommandReceipt],
    ) -> Arc<Publication> {
        let mut inner = self.inner.lock().unwrap();
        let seq = inner.next_seq;
        inner.next_seq += 1;
        inner.published += 1;
        inner.receipts = Arc::new(receipts.to_vec());
        // The counters the stamped section reports are this publish's
        // own outcome — computed before the window push and evictions.
        inner.coalesced += (inner.window.len() + 1).saturating_sub(inner.window_capacity) as u64;
        snapshot.publication = Some(PublicationHealth {
            published: inner.published,
            coalesced: inner.coalesced,
            depth: (inner.window.len() + 1).min(inner.window_capacity) as u64,
            window: inner.window_capacity as u64,
        });
        let publication = Arc::new(Publication {
            seq,
            tick,
            snapshot,
            receipts: inner.receipts.clone(),
            history: inner
                .pending_history
                .iter_mut()
                .map(|(&point, samples)| PointHistory {
                    point,
                    samples: samples.drain(..).collect(),
                })
                .collect(),
            journal: inner
                .pending_boundaries
                .drain(..)
                .collect::<Vec<_>>()
                .into_iter()
                .chain(inner.pending_journal.drain(..))
                .collect(),
            events: inner.pending_events.drain(..).collect(),
        });
        inner.window.push_back(publication.clone());
        while inner.window.len() > inner.window_capacity {
            inner.window.pop_front();
        }
        inner.latest = Some(publication.clone());
        publication
    }

    /// The newest publication — what the snapshot read endpoints
    /// serialize. `None` only before the bind-time publication.
    pub(crate) fn latest(&self) -> Option<Arc<Publication>> {
        self.inner.lock().unwrap().latest.clone()
    }

    /// The retained history of `points` — every point in the latest
    /// publication when empty — keeping only samples with a `seq`
    /// above `since`, in ascending point order regardless of request
    /// order, so equal runs answer identically.
    pub(crate) fn history(&self, points: &[PointId], since: u64) -> Vec<PointHistory> {
        let inner = self.inner.lock().unwrap();
        let selected: BTreeSet<PointId> = if points.is_empty() {
            inner
                .latest
                .as_ref()
                .map(|publication| {
                    publication
                        .snapshot
                        .points
                        .iter()
                        .map(|telemetry| telemetry.point)
                        .collect()
                })
                .unwrap_or_default()
        } else {
            points.iter().copied().collect()
        };
        selected
            .into_iter()
            .map(|point| PointHistory {
                point,
                samples: inner
                    .rings
                    .get(&point)
                    .map(|ring| {
                        ring.samples
                            .iter()
                            .filter(|sample| sample.seq > since)
                            .copied()
                            .collect()
                    })
                    .unwrap_or_default(),
            })
            .collect()
    }

    /// Retained journal entries with a `seq` above `since`, oldest
    /// first — the pinned run-boundary markers ahead of the ring's
    /// tail. Every pinned `seq` precedes the ring's front, so the
    /// concatenation stays in `seq` order and an evicted stretch still
    /// reads as a numbering gap.
    pub(crate) fn journal(&self, since: u64) -> Vec<JournalEntry> {
        let inner = self.inner.lock().unwrap();
        inner
            .boundaries
            .iter()
            .chain(inner.journal.iter())
            .filter(|entry| entry.seq > since)
            .cloned()
            .collect()
    }

    /// The current receipt-log mirror — `GET /receipts`' answer.
    pub(crate) fn receipts(&self) -> Arc<Vec<CommandReceipt>> {
        self.inner.lock().unwrap().receipts.clone()
    }

    /// Reads the retained publication window from a seq cursor:
    /// publications with `seq` above the cursor, oldest first, plus the
    /// named gap when the cursor's successors already aged out — the
    /// explicit signal a lagging consumer coalesces from instead of
    /// backpressuring the run.
    pub(crate) fn page_since(&self, seq: u64) -> PublicationPage {
        let inner = self.inner.lock().unwrap();
        let gap = match inner.window.front() {
            Some(oldest) if seq.saturating_add(1) < oldest.seq => Some(PublicationGap {
                through: oldest.seq - 1,
            }),
            // A zero-bound window holds nothing: every publication the
            // cursor wants is gone.
            None => inner
                .latest
                .as_ref()
                .filter(|latest| seq < latest.seq)
                .map(|latest| PublicationGap {
                    through: latest.seq,
                }),
            _ => None,
        };
        PublicationPage {
            publications: inner
                .window
                .iter()
                .filter(|publication| publication.seq > seq)
                .cloned()
                .collect(),
            gap,
        }
    }

    /// The store's overload counters as they stand now — the same
    /// report the latest publication's snapshot `publication` section
    /// carries as of its publish.
    pub(crate) fn health(&self) -> PublicationHealth {
        let inner = self.inner.lock().unwrap();
        PublicationHealth {
            published: inner.published,
            coalesced: inner.coalesced,
            depth: inner.window.len() as u64,
            window: inner.window_capacity as u64,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{EventValue, IoHealth, Value};
    use std::collections::BTreeMap;

    /// A bare snapshot — the publication only carries it.
    fn snapshot(tick: Tick) -> TelemetrySnapshot {
        TelemetrySnapshot {
            tick,
            points: Vec::new(),
            components: Vec::new(),
            descriptors: Vec::new(),
            io_health: IoHealth::default(),
            forces: Vec::new(),
            parameters: Vec::new(),
            command_queue: Default::default(),
            command_verdicts: Vec::new(),
            publication: None,
        }
    }

    /// One emission of `event` from `component` carrying `n` — the
    /// routed store's test fixture.
    fn emission(component: &str, event: &str, n: i64) -> EmittedEvent {
        EmittedEvent {
            event: event.to_string(),
            component: component.to_string(),
            fields: [("n".to_string(), EventValue::Value(Value::Int(n)))]
                .into_iter()
                .collect::<BTreeMap<_, _>>(),
        }
    }

    #[test]
    fn history_emissions_evict_oldest_first_at_the_bound() {
        // A two-slot ring keeps the newest records; the never-reused
        // seqs show the evicted stretch as a numbering gap.
        let store = Store::new(0, 0, 4, 2);
        for n in 1..=4 {
            store.push_event_history(emission("em", "shift", n), Tick(n as u64));
        }
        let routed = store.routed_events();
        assert_eq!(
            routed
                .history
                .iter()
                .map(|record| (record.seq, record.tick))
                .collect::<Vec<_>>(),
            vec![(3, Tick(3)), (4, Tick(4))]
        );
        assert!(
            routed
                .history
                .iter()
                .all(|record| record.retention == EventRetention::History)
        );
    }

    #[test]
    fn latest_emissions_stand_one_record_per_identity() {
        // Each newer emission supersedes the identity's standing
        // record; a second identity sits beside it. The standing
        // record carries the superseding emission's seq and tick.
        let store = Store::new(0, 0, 4, 8);
        for n in 1..=3 {
            store.push_latest_event(emission("em", "beat", n), Tick(n as u64));
        }
        store.push_latest_event(emission("other", "beat", 9), Tick(4));

        let routed = store.routed_events();
        assert_eq!(routed.latest.len(), 2);
        let em = &routed.latest[0];
        assert_eq!(em.event.component, "em");
        assert_eq!(em.event.event, "beat");
        assert_eq!((em.seq, em.tick), (3, Tick(3)));
        assert_eq!(
            em.event.fields["n"],
            EventValue::Value(Value::Int(3)),
            "the newest emission superseded"
        );
        assert_eq!(routed.latest[1].event.component, "other");
        // Both classes draw on the one routed-event stream's seqs.
        assert!(routed.latest.iter().map(|record| record.seq).eq([3, 4]));
    }

    #[test]
    fn routed_emissions_ride_the_publication_delta() {
        // Every routed emission — both classes — appends to the event
        // delta the next publication drains, in routed order.
        let store = Store::new(0, 0, 4, 8);
        store.push_event_history(emission("em", "shift", 1), Tick(1));
        store.push_latest_event(emission("em", "beat", 1), Tick(1));
        let publication = store.publish(Tick(1), snapshot(Tick(1)), &[]);
        assert_eq!(
            publication
                .events
                .iter()
                .map(|record| (record.retention, record.seq))
                .collect::<Vec<_>>(),
            vec![(EventRetention::History, 1), (EventRetention::Latest, 2)]
        );
        // Drained: the next publication carries no stale delta.
        let publication = store.publish(Tick(2), snapshot(Tick(2)), &[]);
        assert!(publication.events.is_empty());
    }
}
