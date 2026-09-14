//! The scripted-scenario simulated backend: [`ScriptedDriver`].
//!
//! `ScriptedDriver` is a second simulated device kind beside
//! [`SimDriver`](crate::SimDriver): its `In` points play back a
//! tick-indexed script of values and qualities — faults included — while
//! its `Out` points record every accepted write in a log callers inspect
//! through [`writes`](ScriptedDriver::writes). Its purpose is proving the
//! driver-registry "add a device" path and giving demos deterministic
//! field scenarios: the model declares the script in the device's
//! kind-specific parameters and the registered factory builds the driver.
//!
//! Like `SimDriver`, nothing here reads a clock: the script is indexed by
//! the driver's logical tick, which advances only when
//! [`step`](ScriptedDriver::step) is called, so identical write and step
//! sequences replay identically on every run.

use crate::driver::{decode_quality, encode_quality};
use crate::map::{ChannelId, Direction, PointBinding};
use dcs_core::{
    IoDriver, IoError, PointId, Quality, Sample, StateError, StateMap, Tick, Value, ValueKind,
};
use std::collections::{BTreeMap, HashMap, HashSet};
use std::fmt;
use std::sync::Mutex;

/// One entry in an `In` point's playback script: the sample readers
/// observe from `tick` until the next entry's tick.
///
/// A script's entries must be in strictly increasing tick order —
/// [`ScriptedDriver::new`] reports [`ScriptError::NonMonotonic`]
/// otherwise — and every `value` must match the bound point's declared
/// kind. An entry at [`Tick::ZERO`] is already in effect when the driver
/// is built; entries at later ticks apply as
/// [`step`](ScriptedDriver::step) reaches them.
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ScriptEntry {
    /// The driver tick this entry takes effect at.
    pub tick: Tick,
    /// The value readers observe; its variant must match the point's
    /// declared kind.
    pub value: Value,
    /// The quality readers observe — [`Quality::Good`] or a scripted
    /// fault.
    pub quality: Quality,
}

/// A write the driver accepted, in the order it landed.
///
/// The log is the inspection surface for a scripted device's `Out`
/// points — what a demo or test asserts the controller actually
/// commanded — though every accepted write is recorded, writes to `In`
/// points included (tests force field inputs the same way on
/// [`SimDriver`](crate::SimDriver)).
#[derive(Debug, Clone, PartialEq)]
pub struct RecordedWrite {
    /// The point written.
    pub point: PointId,
    /// The channel `point` is bound to.
    pub channel: ChannelId,
    /// The written value.
    pub value: Value,
    /// The driver tick the write landed at.
    pub tick: Tick,
}

/// Why a [`ScriptedDriver`] could not be built: the point bindings or the
/// scripts are inconsistent.
#[derive(Debug, Clone, PartialEq)]
pub enum ScriptError {
    /// Two bindings declare the same point id.
    DuplicatePoint(PointId),
    /// Two bindings declare the same channel.
    DuplicateChannel(ChannelId),
    /// A script names a point the driver serves no binding for.
    UnknownPoint(PointId),
    /// A script targets an `Out` point; scripts drive `In` points — an
    /// `Out` point's value comes from controller writes, not playback.
    OutputPoint {
        /// The mis-scripted point.
        point: PointId,
        /// The channel it is bound to.
        channel: ChannelId,
    },
    /// A script's entries are not in strictly increasing tick order.
    NonMonotonic {
        /// The scripted point.
        point: PointId,
        /// The channel it is bound to.
        channel: ChannelId,
        /// The offending entry's tick.
        tick: Tick,
        /// The previous entry's tick.
        previous: Tick,
    },
    /// A script entry's value kind differs from the bound point's
    /// declared kind.
    ValueKind {
        /// The scripted point.
        point: PointId,
        /// The channel it is bound to.
        channel: ChannelId,
        /// The kind the point declares.
        expected: ValueKind,
        /// The value the entry carries.
        found: Value,
    },
}

impl fmt::Display for ScriptError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicatePoint(point) => {
                write!(f, "point {} is bound more than once", point.0)
            }
            Self::DuplicateChannel(channel) => write!(
                f,
                "channel {:?} on device {} is bound more than once",
                channel.name, channel.device
            ),
            Self::UnknownPoint(point) => {
                write!(
                    f,
                    "script names point {} the driver does not serve",
                    point.0
                )
            }
            Self::OutputPoint { point, channel } => write!(
                f,
                "script names channel {:?} on device {}, which backs out point {} — scripts drive in points",
                channel.name, channel.device, point.0
            ),
            Self::NonMonotonic {
                point,
                channel,
                tick,
                previous,
            } => write!(
                f,
                "script for channel {:?} (point {}) is not in strictly increasing tick order: tick {} follows tick {}",
                channel.name, point.0, tick.0, previous.0
            ),
            Self::ValueKind {
                point,
                channel,
                expected,
                found,
            } => write!(
                f,
                "script for channel {:?} (point {}) expects {expected:?} values, found {found:?}",
                channel.name, point.0
            ),
        }
    }
}

impl std::error::Error for ScriptError {}

/// One bound point's runtime state.
struct PointState {
    /// The binding's direction: whether the controller reads or writes
    /// the point.
    direction: Direction,
    /// The point's declared kind, taken from the binding's initial value.
    kind: ValueKind,
    /// The channel the point is bound to, recorded on each write.
    channel: ChannelId,
    /// The sample readers observe: the latest applied script entry, the
    /// last accepted write, or the binding's neutral initial.
    sample: Sample,
    /// The point's script, tick-ordered; empty for unscripted points.
    script: Vec<ScriptEntry>,
    /// Cursor into `script`: the index of the first entry not yet
    /// applied. Entries are strictly increasing, so `script[next]` is
    /// the next entry waiting for its tick.
    next: usize,
}

/// Everything behind the driver's `Mutex` — `ScriptedDriver` is `Sync`
/// for the same reason [`SimDriver`](crate::SimDriver) is.
struct State {
    points: HashMap<PointId, PointState>,
    /// The write log [`ScriptedDriver::writes`] reports.
    writes: Vec<RecordedWrite>,
    tick: Tick,
}

/// The element name [`StateError`]s from the driver's state-capture
/// contract report.
const STATE_ELEMENT: &str = "scripted-driver";

/// A deterministic simulated I/O backend whose `In` points replay a
/// tick-indexed script and whose `Out` points record writes.
///
/// Each [`step`](Self::step) advances the driver's tick by one and applies
/// every script entry whose tick the new tick reaches; an applied entry
/// holds until the next one. An `In` point with no script — or whose
/// script has not reached its first entry — serves its binding's initial
/// value with [`Quality::Good`].
///
/// [`IoDriver::write`] on any bound point stores the value at the current
/// tick and appends a [`RecordedWrite`] to the log
/// [`writes`](Self::writes) returns. A write to an `In` point overrides
/// the observed sample until the next script entry applies, the same
/// field-forcing role writes serve on [`SimDriver`](crate::SimDriver).
///
/// The driver also implements the driver half of the state-capture
/// contract: a checkpoint carries the tick, every point's observed
/// sample, and the write log; script cursors are derived back from the
/// restored tick, so a restored driver continues playback identically.
pub struct ScriptedDriver {
    state: Mutex<State>,
}

impl ScriptedDriver {
    /// Builds a driver serving `points` — the same [`PointBinding`]
    /// vocabulary a [`ChannelMap`](crate::ChannelMap) uses — with `scripts`
    /// giving each scripted `In` point's tick-ordered entries.
    ///
    /// Validation rejects duplicate points and channels, scripts naming
    /// unserved or `Out` points, entries not in strictly increasing tick
    /// order, and entry values whose kind differs from the point's —
    /// each as a [`ScriptError`] naming the offending point and channel.
    pub fn new(
        points: Vec<PointBinding>,
        scripts: BTreeMap<PointId, Vec<ScriptEntry>>,
    ) -> Result<Self, ScriptError> {
        let mut states = HashMap::with_capacity(points.len());
        let mut channels = HashSet::with_capacity(points.len());
        for binding in points {
            if states.contains_key(&binding.point) {
                return Err(ScriptError::DuplicatePoint(binding.point));
            }
            if !channels.insert(binding.channel.clone()) {
                return Err(ScriptError::DuplicateChannel(binding.channel));
            }
            states.insert(
                binding.point,
                PointState {
                    direction: binding.direction,
                    kind: binding.kind(),
                    channel: binding.channel,
                    sample: Sample::good(binding.initial, Tick::ZERO),
                    script: Vec::new(),
                    next: 0,
                },
            );
        }
        for (point, entries) in scripts {
            let Some(state) = states.get_mut(&point) else {
                return Err(ScriptError::UnknownPoint(point));
            };
            if state.direction != Direction::In {
                return Err(ScriptError::OutputPoint {
                    point,
                    channel: state.channel.clone(),
                });
            }
            let mut previous = None;
            for entry in &entries {
                if entry.value.kind() != state.kind {
                    return Err(ScriptError::ValueKind {
                        point,
                        channel: state.channel.clone(),
                        expected: state.kind,
                        found: entry.value,
                    });
                }
                if let Some(previous) = previous
                    && entry.tick <= previous
                {
                    return Err(ScriptError::NonMonotonic {
                        point,
                        channel: state.channel.clone(),
                        tick: entry.tick,
                        previous,
                    });
                }
                previous = Some(entry.tick);
            }
            // Entries already in effect at construction — those at
            // Tick::ZERO — apply as the initial sample; the cursor parks
            // past them.
            state.next = entries.partition_point(|entry| entry.tick <= Tick::ZERO);
            if let Some(entry) = entries[..state.next].last() {
                state.sample = Sample::new(entry.value, entry.quality, Tick::ZERO);
            }
            state.script = entries;
        }
        Ok(Self {
            state: Mutex::new(State {
                points: states,
                writes: Vec::new(),
                tick: Tick::ZERO,
            }),
        })
    }

    /// The driver's current logical tick.
    pub fn tick(&self) -> Tick {
        self.state.lock().unwrap().tick
    }

    /// Advances the driver one tick of `dt` time units and returns the
    /// new tick, applying every script entry whose tick the new tick
    /// reaches. `dt` paces simulated time exactly as on
    /// [`SimDriver::step`](crate::SimDriver::step); playback itself is
    /// indexed by ticks, not `dt`.
    ///
    /// # Panics
    ///
    /// Panics when `dt` is negative or non-finite.
    pub fn step(&self, dt: f64) -> Tick {
        assert!(
            dt.is_finite() && dt >= 0.0,
            "step dt must be finite and non-negative, got {dt}"
        );
        let state = &mut *self.state.lock().unwrap();
        state.tick = Tick(state.tick.0 + 1);
        let tick = state.tick;
        for point in state.points.values_mut() {
            while let Some(entry) = point.script.get(point.next) {
                if entry.tick > tick {
                    break;
                }
                point.sample = Sample::new(entry.value, entry.quality, tick);
                point.next += 1;
            }
        }
        tick
    }

    /// The recorded writes, in the order they landed.
    pub fn writes(&self) -> Vec<RecordedWrite> {
        self.state.lock().unwrap().writes.clone()
    }
}

impl IoDriver for ScriptedDriver {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        self.state
            .lock()
            .unwrap()
            .points
            .get(&point)
            .map(|point_state| point_state.sample)
            .ok_or(IoError::UnknownPoint(point))
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let state = &mut *self.state.lock().unwrap();
        let point_state = state
            .points
            .get_mut(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if value.kind() != point_state.kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: point_state.kind,
                found: value,
            });
        }
        point_state.sample = Sample::good(value, state.tick);
        state.writes.push(RecordedWrite {
            point,
            channel: point_state.channel.clone(),
            value,
            tick: state.tick,
        });
        Ok(())
    }

    /// Captures the scripted field state: the driver tick, every bound
    /// point's observed sample (value, quality, tick), and the write log
    /// — `write.{i}.point` / `.value` / `.tick` entries under a
    /// `writes.len` count. Scripts and bindings are configuration, not
    /// state; restore recomputes the playback cursors from the tick.
    fn capture_state(&self) -> Option<StateMap> {
        let state = self.state.lock().unwrap();
        let mut captured = StateMap::new();
        captured.insert("tick", Value::Int(state.tick.0 as i64));
        let mut points: Vec<(&PointId, &PointState)> = state.points.iter().collect();
        points.sort_by_key(|(point, _)| **point);
        for (point, point_state) in points {
            let prefix = format!("point.{}", point.0);
            captured.insert(format!("{prefix}.value"), point_state.sample.value);
            captured.insert(
                format!("{prefix}.quality"),
                Value::Int(encode_quality(point_state.sample.quality)),
            );
            captured.insert(
                format!("{prefix}.tick"),
                Value::Int(point_state.sample.tick.0 as i64),
            );
        }
        captured.insert("writes.len", Value::Int(state.writes.len() as i64));
        for (index, write) in state.writes.iter().enumerate() {
            let prefix = format!("write.{index}");
            captured.insert(format!("{prefix}.point"), Value::Int(write.point.0 as i64));
            captured.insert(format!("{prefix}.value"), write.value);
            captured.insert(format!("{prefix}.tick"), Value::Int(write.tick.0 as i64));
        }
        Some(captured)
    }

    /// Restores state produced by an equivalent driver's
    /// [`capture_state`](IoDriver::capture_state).
    ///
    /// The whole map is validated first — required fields present with
    /// the declared kinds, codes decodable, every recorded write naming a
    /// served point of matching kind, and no fields this driver never
    /// captured — so a rejected restore changes nothing. Playback cursors
    /// are recomputed from the restored tick, so continued stepping
    /// replays identically to the captured run.
    fn restore_state(&self, state: &StateMap) -> Result<(), StateError> {
        let invalid = |field: String, value: Value| StateError::InvalidValue {
            element: STATE_ELEMENT.to_string(),
            field,
            value,
        };
        let current = &mut *self.state.lock().unwrap();

        let tick = state.require_i64(STATE_ELEMENT, "tick")?;
        if tick < 0 {
            return Err(invalid("tick".to_string(), Value::Int(tick)));
        }
        let writes_len = state.require_i64(STATE_ELEMENT, "writes.len")?;
        if writes_len < 0 {
            return Err(invalid("writes.len".to_string(), Value::Int(writes_len)));
        }

        let mut known = vec!["tick".to_string(), "writes.len".to_string()];
        let mut samples = HashMap::with_capacity(current.points.len());
        for (&point, point_state) in &current.points {
            let prefix = format!("point.{}", point.0);
            let value =
                state.require_kind(STATE_ELEMENT, &format!("{prefix}.value"), point_state.kind)?;
            let quality_code = state.require_i64(STATE_ELEMENT, &format!("{prefix}.quality"))?;
            let quality = decode_quality(quality_code)
                .ok_or_else(|| invalid(format!("{prefix}.quality"), Value::Int(quality_code)))?;
            let sample_tick = state.require_i64(STATE_ELEMENT, &format!("{prefix}.tick"))?;
            if sample_tick < 0 {
                return Err(invalid(format!("{prefix}.tick"), Value::Int(sample_tick)));
            }
            known.extend([
                format!("{prefix}.value"),
                format!("{prefix}.quality"),
                format!("{prefix}.tick"),
            ]);
            samples.insert(point, Sample::new(value, quality, Tick(sample_tick as u64)));
        }

        let mut writes = Vec::with_capacity(writes_len as usize);
        for index in 0..writes_len {
            let prefix = format!("write.{index}");
            let point_code = state.require_i64(STATE_ELEMENT, &format!("{prefix}.point"))?;
            if point_code < 0 {
                return Err(invalid(format!("{prefix}.point"), Value::Int(point_code)));
            }
            let point = PointId(point_code as u64);
            let Some(point_state) = current.points.get(&point) else {
                return Err(invalid(format!("{prefix}.point"), Value::Int(point_code)));
            };
            let value =
                state.require_kind(STATE_ELEMENT, &format!("{prefix}.value"), point_state.kind)?;
            let write_tick = state.require_i64(STATE_ELEMENT, &format!("{prefix}.tick"))?;
            if write_tick < 0 {
                return Err(invalid(format!("{prefix}.tick"), Value::Int(write_tick)));
            }
            known.extend([
                format!("{prefix}.point"),
                format!("{prefix}.value"),
                format!("{prefix}.tick"),
            ]);
            writes.push(RecordedWrite {
                point,
                channel: point_state.channel.clone(),
                value,
                tick: Tick(write_tick as u64),
            });
        }

        let known_refs: Vec<&str> = known.iter().map(String::as_str).collect();
        state.ensure_known_fields(STATE_ELEMENT, &known_refs)?;

        current.tick = Tick(tick as u64);
        for (point, sample) in samples {
            let point_state = current.points.get_mut(&point).unwrap();
            point_state.sample = sample;
            // Replay position follows from the restored tick: entries at
            // or before it are already in effect.
            point_state.next = point_state
                .script
                .partition_point(|entry| entry.tick <= current.tick);
        }
        current.writes = writes;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::QualityReason;

    fn binding(point: u64, direction: Direction, initial: Value) -> PointBinding {
        PointBinding {
            point: PointId(point),
            channel: ChannelId {
                device: 1,
                name: format!("ch{point}"),
            },
            direction,
            initial,
        }
    }

    fn good(tick: u64, value: Value) -> ScriptEntry {
        ScriptEntry {
            tick: Tick(tick),
            value,
            quality: Quality::Good,
        }
    }

    fn driver() -> ScriptedDriver {
        ScriptedDriver::new(
            vec![
                binding(10, Direction::In, Value::Float(0.0)),
                binding(11, Direction::In, Value::Int(0)),
                binding(20, Direction::Out, Value::Bool(false)),
            ],
            BTreeMap::from([
                (
                    PointId(10),
                    vec![
                        good(0, Value::Float(4.0)),
                        good(3, Value::Float(12.0)),
                        ScriptEntry {
                            tick: Tick(5),
                            value: Value::Float(0.0),
                            quality: Quality::Bad(QualityReason::DeviceFault),
                        },
                        good(7, Value::Float(8.0)),
                    ],
                ),
                (PointId(11), vec![good(2, Value::Int(42))]),
            ]),
        )
        .unwrap()
    }

    #[test]
    fn playback_follows_the_script_in_tick_order() {
        let driver = driver();
        // The tick-0 entry is already in effect; an unscripted-tick In
        // point holds its neutral initial.
        assert_eq!(
            driver.read(PointId(10)).unwrap(),
            Sample::good(Value::Float(4.0), Tick::ZERO)
        );
        assert_eq!(
            driver.read(PointId(11)).unwrap(),
            Sample::good(Value::Int(0), Tick::ZERO)
        );

        for _ in 0..2 {
            driver.step(0.1);
        }
        assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Float(4.0));
        assert_eq!(
            driver.read(PointId(11)).unwrap(),
            Sample::good(Value::Int(42), Tick(2))
        );

        driver.step(0.1);
        assert_eq!(
            driver.read(PointId(10)).unwrap(),
            Sample::good(Value::Float(12.0), Tick(3))
        );

        driver.step(0.1);
        driver.step(0.1);
        assert_eq!(
            driver.read(PointId(10)).unwrap(),
            Sample::new(
                Value::Float(0.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(5)
            )
        );

        // The last entry holds after the script runs out, stamped with
        // the tick it applied at.
        for _ in 0..10 {
            driver.step(0.1);
        }
        assert_eq!(
            driver.read(PointId(10)).unwrap(),
            Sample::good(Value::Float(8.0), Tick(7))
        );
    }

    #[test]
    fn identical_runs_replay_identically() {
        let one = driver();
        let two = driver();
        for _ in 0..10 {
            one.step(0.1);
            two.step(0.1);
        }
        for point in [PointId(10), PointId(11), PointId(20)] {
            assert_eq!(one.read(point), two.read(point));
        }
    }

    #[test]
    fn writes_are_recorded_in_order_with_channel_and_tick() {
        let driver = driver();
        driver.write(PointId(20), Value::Bool(true)).unwrap();
        driver.step(0.1);
        driver.write(PointId(20), Value::Bool(false)).unwrap();

        assert_eq!(
            driver.writes(),
            vec![
                RecordedWrite {
                    point: PointId(20),
                    channel: ChannelId {
                        device: 1,
                        name: "ch20".to_string(),
                    },
                    value: Value::Bool(true),
                    tick: Tick(0),
                },
                RecordedWrite {
                    point: PointId(20),
                    channel: ChannelId {
                        device: 1,
                        name: "ch20".to_string(),
                    },
                    value: Value::Bool(false),
                    tick: Tick(1),
                },
            ]
        );
        // The stored sample is readable too.
        assert_eq!(
            driver.read(PointId(20)).unwrap(),
            Sample::good(Value::Bool(false), Tick(1))
        );
    }

    #[test]
    fn write_to_an_in_point_overrides_until_the_next_entry() {
        let driver = driver();
        driver.write(PointId(10), Value::Float(99.0)).unwrap();
        assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Float(99.0));
        // The forced value is recorded like any other write.
        assert_eq!(driver.writes().len(), 1);

        for _ in 0..3 {
            driver.step(0.1);
        }
        // The tick-3 entry replaces the forced value.
        assert_eq!(driver.read(PointId(10)).unwrap().value, Value::Float(12.0));
    }

    #[test]
    fn write_of_wrong_kind_is_type_mismatch_and_not_recorded() {
        let driver = driver();
        let error = driver.write(PointId(20), Value::Float(1.0)).err().unwrap();
        assert_eq!(
            error,
            IoError::TypeMismatch {
                point: PointId(20),
                expected: ValueKind::Bool,
                found: Value::Float(1.0),
            }
        );
        assert!(driver.writes().is_empty());
        assert_eq!(driver.read(PointId(20)).unwrap().value, Value::Bool(false));
    }

    #[test]
    fn unserved_points_are_unknown() {
        let driver = driver();
        let unknown = PointId(99);
        assert_eq!(driver.read(unknown), Err(IoError::UnknownPoint(unknown)));
        assert_eq!(
            driver.write(unknown, Value::Bool(true)),
            Err(IoError::UnknownPoint(unknown))
        );
    }

    #[test]
    fn malformed_scripts_are_rejected_naming_point_and_channel() {
        let points = || {
            vec![
                binding(10, Direction::In, Value::Float(0.0)),
                binding(20, Direction::Out, Value::Bool(false)),
            ]
        };

        // Non-monotonic ticks.
        let error = ScriptedDriver::new(
            points(),
            BTreeMap::from([(
                PointId(10),
                vec![good(5, Value::Float(1.0)), good(5, Value::Float(2.0))],
            )]),
        )
        .err()
        .unwrap();
        assert!(matches!(
            error,
            ScriptError::NonMonotonic {
                point: PointId(10),
                ..
            }
        ));
        assert!(error.to_string().contains("ch10"), "{error}");

        // A value kind mismatching the channel.
        let error = ScriptedDriver::new(
            points(),
            BTreeMap::from([(PointId(10), vec![good(0, Value::Bool(true))])]),
        )
        .err()
        .unwrap();
        assert_eq!(
            error,
            ScriptError::ValueKind {
                point: PointId(10),
                channel: ChannelId {
                    device: 1,
                    name: "ch10".to_string(),
                },
                expected: ValueKind::Float,
                found: Value::Bool(true),
            }
        );

        // A script on an Out point.
        let error = ScriptedDriver::new(
            points(),
            BTreeMap::from([(PointId(20), vec![good(0, Value::Bool(true))])]),
        )
        .err()
        .unwrap();
        assert!(matches!(
            error,
            ScriptError::OutputPoint {
                point: PointId(20),
                ..
            }
        ));

        // A script on a point the driver does not serve.
        let error = ScriptedDriver::new(
            points(),
            BTreeMap::from([(PointId(77), vec![good(0, Value::Float(1.0))])]),
        )
        .err()
        .unwrap();
        assert_eq!(error, ScriptError::UnknownPoint(PointId(77)));

        // Duplicate points and channels.
        let mut dup_points = points();
        dup_points.push(binding(10, Direction::In, Value::Float(1.0)));
        assert_eq!(
            ScriptedDriver::new(dup_points, BTreeMap::new())
                .err()
                .unwrap(),
            ScriptError::DuplicatePoint(PointId(10))
        );
        let mut dup_channels = points();
        let mut extra = binding(30, Direction::In, Value::Int(0));
        extra.channel.name = "ch10".to_string();
        dup_channels.push(extra);
        assert_eq!(
            ScriptedDriver::new(dup_channels, BTreeMap::new())
                .err()
                .unwrap(),
            ScriptError::DuplicateChannel(ChannelId {
                device: 1,
                name: "ch10".to_string(),
            })
        );
    }

    #[test]
    fn captured_state_restores_into_a_fresh_driver_and_continues_playback() {
        let original = driver();
        original.write(PointId(20), Value::Bool(true)).unwrap();
        for _ in 0..4 {
            original.step(0.1);
        }
        let state = original.capture_state().unwrap();
        assert!(!state.is_empty());

        let json = serde_json::to_string(&state).unwrap();
        let state = serde_json::from_str::<StateMap>(&json).unwrap();

        let fresh = driver();
        fresh.restore_state(&state).unwrap();
        assert_eq!(fresh.tick(), original.tick());
        assert_eq!(fresh.writes(), original.writes());
        for point in [PointId(10), PointId(11), PointId(20)] {
            assert_eq!(fresh.read(point), original.read(point));
        }

        // Continued stepping replays identically: the tick-5 bad-quality
        // entry applies on the restored driver exactly as on the original.
        original.step(0.1);
        fresh.step(0.1);
        assert_eq!(fresh.read(PointId(10)), original.read(PointId(10)));
        assert_eq!(
            fresh.read(PointId(10)).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
    }

    #[test]
    fn restore_rejects_state_the_driver_did_not_produce() {
        let driver = driver();

        // A foreign field.
        let mut map = driver.capture_state().unwrap();
        map.insert("bogus", Value::Int(0));
        assert!(matches!(
            driver.restore_state(&map),
            Err(StateError::UnknownField { .. })
        ));

        // A point the driver does not serve.
        let mut map = driver.capture_state().unwrap();
        map.insert("point.99.value", Value::Float(0.0));
        map.insert("point.99.quality", Value::Int(0));
        map.insert("point.99.tick", Value::Int(0));
        assert!(driver.restore_state(&map).is_err());

        // A recorded write naming an unserved point.
        driver.write(PointId(20), Value::Bool(true)).unwrap();
        let mut map = driver.capture_state().unwrap();
        map.insert("write.0.point", Value::Int(99));
        assert!(matches!(
            driver.restore_state(&map),
            Err(StateError::InvalidValue { .. })
        ));
        // A rejected restore changed nothing.
        assert_eq!(driver.writes().len(), 1);
    }
}
