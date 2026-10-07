//! The shared acknowledgment-latch core for the four latching-alarm
//! kinds (`latching-alarm`, `bool-latching-alarm`,
//! `managed-latching-alarm`, `managed-bool-latching-alarm`).
//!
//! **Consumed-edge ack rule (documented once):** `unacknowledged`
//! latches on a fresh trip and clears on the scan `ack` reads `true`
//! after reading `false` — the acknowledgment's rising edge — even
//! while the alarm still stands. The edge is consumed: a held `ack`
//! level clears the latch once and cannot pre-acknowledge a later
//! trip — a fresh trip arriving while `ack` still stands latches
//! normally, and one landing on the edge's own scan latches too, the
//! pulse having acknowledged what stood before it. An `ack` left
//! asserted therefore hides nothing; acknowledging again takes a
//! `false` write first. Acknowledging never clears `alarm`: each kind
//! keeps computing its own standing state.
//!
//! Each kind keeps computing its own standing `state`/`fresh_trip`
//! and delegates these two shared fields — the observed `ack` level
//! and the acknowledgment latch — to [`AckLatch`], so the checkpoint
//! vocabulary (`ack`, `unacknowledged`) stays byte-identical across
//! all four kinds.

use dcs_core::{StateError, StateMap, Value};

/// The two shared ack-latch fields every latching-alarm kind carries:
/// the `ack` level observed on the previous scan — the baseline the
/// acknowledgment's rising edge is detected against — and the
/// `unacknowledged` latch itself.
#[derive(Debug, Clone, Copy, Default)]
pub(crate) struct AckLatch {
    /// The `ack` level observed on the previous scan. `false` before
    /// the first scan, so a `true` first read is an acknowledgment.
    seen: bool,
    /// The acknowledgment latch: set on a fresh trip, cleared on
    /// `ack`'s rising edge.
    latched: bool,
}

impl AckLatch {
    /// The unsignalled latch: no observed `ack`, nothing latched.
    pub(crate) fn new() -> Self {
        Self::default()
    }

    /// Advances the latch one scan under the consumed-edge rule and
    /// returns whether `unacknowledged` stands: `acknowledged` is the
    /// `ack` rising edge against the previously observed level, then
    /// `latched = (latched && !acknowledged) || fresh_trip`.
    pub(crate) fn update(&mut self, ack: bool, fresh_trip: bool) -> bool {
        let acknowledged = ack && !self.seen;
        self.seen = ack;
        self.latched = (self.latched && !acknowledged) || fresh_trip;
        self.latched
    }

    /// Advances the latch one scan with the managed kinds'
    /// suppression gate: the shared update, withheld while `suppressed`
    /// stands — the only gate on the latch.
    pub(crate) fn update_suppressed(
        &mut self,
        ack: bool,
        fresh_trip: bool,
        suppressed: bool,
    ) -> bool {
        self.update(ack, fresh_trip);
        if suppressed {
            self.latched = false;
        }
        self.latched
    }

    /// Whether `unacknowledged` stands.
    #[cfg(test)]
    pub(crate) fn latched(&self) -> bool {
        self.latched
    }

    /// Folds the two shared fields into `state` under the
    /// byte-identical checkpoint names every kind has always carried:
    /// `ack` for the observed level, `unacknowledged` for the latch.
    pub(crate) fn capture(&self, state: &mut StateMap) {
        state.insert("ack", Value::Bool(self.seen));
        state.insert("unacknowledged", Value::Bool(self.latched));
    }

    /// The restore-path read of [`capture`](Self::capture)'s fields.
    /// `ack` is absent from checkpoints predating the field; a held
    /// level then reads as an edge on the first restored scan — the
    /// same clearing the older level-rule applied every scan.
    pub(crate) fn restore(component: &str, state: &StateMap) -> Result<Self, StateError> {
        Ok(Self {
            seen: state.optional_bool(component, "ack")?.unwrap_or(false),
            latched: state.require_bool(component, "unacknowledged")?,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn held_ack_cannot_pre_acknowledge_a_later_trip() {
        let mut latch = AckLatch::new();
        // The QA finding's reproduction: `ack` held `true` while
        // nothing stands latches nothing, and the later trip still
        // annunciates.
        assert!(!latch.update(true, false));
        assert!(latch.update(true, true));
        // Releasing while the trip stands leaves the latch standing;
        // a second pulse acknowledges it.
        assert!(latch.update(false, false));
        assert!(!latch.update(true, false));
    }

    #[test]
    fn fresh_trip_on_the_ack_edge_scan_still_latches() {
        let mut latch = AckLatch::new();
        // The pulse acknowledged what stood before it, so the fresh
        // trip latches rather than passing silently.
        assert!(latch.update(true, true));
        // The held level consumes nothing further.
        assert!(latch.update(true, false));
    }

    #[test]
    fn suppression_withholds_and_never_re_arms() {
        let mut latch = AckLatch::new();
        assert!(!latch.update_suppressed(false, true, true));
        // Release evaluates fresh: the standing trip arrives as a new
        // latch transition.
        assert!(latch.update_suppressed(false, true, false));
    }

    #[test]
    fn capture_restore_round_trips_with_identical_field_names() {
        let mut latch = AckLatch::new();
        latch.update(false, true);
        let mut state = StateMap::new();
        latch.capture(&mut state);
        assert_eq!(state.get("ack"), Some(Value::Bool(false)));
        assert_eq!(state.get("unacknowledged"), Some(Value::Bool(true)));
        let restored = AckLatch::restore("lal", &state).unwrap();
        assert!(restored.latched());
        // Pre-`ack` checkpoints default the observed level to false.
        let mut legacy = StateMap::new();
        legacy.insert("unacknowledged", Value::Bool(true));
        let restored = AckLatch::restore("lal", &legacy).unwrap();
        assert!(restored.latched());
    }
}
