//! The register bank a [`BusServer`](crate::BusServer) shares: numbered
//! registers holding typed values, stamped by an explicit logical tick.
//!
//! The bank is the simulated fieldbus device's visible state — the
//! register-map analogue of `dcs-sim`'s point storage, addressed by
//! `u16` register index rather than by point. Writes stamp the stored
//! sample with the bank's current tick; the tick advances only on
//! [`step`](RegisterBank::step), so identical write and step sequences
//! produce identical samples on every run. A stored sample's quality is
//! `Good` unless a development-tooling injection stamps it otherwise —
//! see [`inject_quality`](RegisterBank::inject_quality).
//!
//! ## Declared dynamics
//!
//! [`with_dynamics`](RegisterBank::with_dynamics) merges a list of
//! [`ProcessElement`] declarations — the same serde vocabulary a
//! `dcs-plant-server --dynamics` document carries — over the bank's
//! registers. The recorded binding encoding: an element's point-valued
//! fields carry register addresses, so a document's `"input": 20`
//! binds register 20. Element behavior is the driver's own, unchanged:
//! each [`step`](RegisterBank::step) advances every element by the
//! caller-supplied `dt` in declaration order — a `Good` input advances
//! the element and stamps its output register `Good`, a non-`Good`
//! input freezes the element's state and propagates its quality to the
//! output (a `flow_sum` propagating the worst of its inputs'
//! qualities). Element state lives in the bank for the bank's
//! lifetime: like the `dcs-sim-net` plant, the field is shared state
//! served by the device process — not checkpointed controller state.
//!
//! ## Declared field wiring
//!
//! [`with_wires`](RegisterBank::with_wires) declares terminal wiring
//! the bank plays: each `(driven, observing)` pair carries every write
//! landing on `driven` — a point-wise write or an exchange-published
//! output — onto `observing` in the same request, so an exchange
//! publishing a looped-back output answers the wired input's
//! transition in that exchange's own census. This is the register-bank
//! analogue of a rig's physical DO→DI wiring: the wire is field state
//! the device serves, not a controller-side route pacing the model's
//! `step` a scan later.

use crate::protocol::{BusError, RegisterInfo};
use dcs_core::{IoDriver, IoError, PointId, Quality, Sample, Tick, Value};
use dcs_sim::{
    ChannelId, ChannelMap, ConfigError, Direction, Fault, PointBinding, ProcessElement, SimDriver,
};
use std::collections::btree_map::Entry;
use std::collections::{BTreeMap, BTreeSet};
use std::fmt;

/// One register's declaration: its address and initial value. The
/// initial's [`Value`] variant is the register's declared kind — writes
/// carrying any other variant fail
/// [`BusError::KindMismatch`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RegisterDecl {
    /// The register address.
    pub register: u16,
    /// The register's value from power-on until the first write — or,
    /// for a register a dynamics element drives, until the element's
    /// `initial` seeds it.
    pub initial: Value,
}

/// Why a [`RegisterBank`] could not be built.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum BankError {
    /// Two declarations name the same register address.
    DuplicateRegister(u16),
    /// A declared field wire names a register the bank does not serve.
    UnknownWireRegister {
        /// The register the write lands on.
        driven: u16,
        /// The register the wire carries it to.
        observing: u16,
    },
    /// A declared field wire's ends carry different value kinds — the
    /// driven register's value cannot land on the observing register.
    WireKindMismatch {
        /// The register the write lands on.
        driven: u16,
        /// The register the wire carries it to.
        observing: u16,
    },
    /// A declared field wire loops a register onto itself.
    SelfWire(u16),
}

impl fmt::Display for BankError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateRegister(register) => {
                write!(f, "register {register} is declared more than once")
            }
            Self::UnknownWireRegister { driven, observing } => write!(
                f,
                "field wire {driven} -> {observing} names a register the bank does not declare"
            ),
            Self::WireKindMismatch { driven, observing } => write!(
                f,
                "field wire {driven} -> {observing} joins registers of different kinds"
            ),
            Self::SelfWire(register) => {
                write!(f, "field wire loops register {register} onto itself")
            }
        }
    }
}

impl std::error::Error for BankError {}

/// Why a [`RegisterBank`] serving declared dynamics could not be built.
#[derive(Debug, Clone, PartialEq)]
pub enum DynamicsError {
    /// A register declaration failed.
    Register(BankError),
    /// The element at `index` in the merged document failed
    /// validation.
    Element {
        /// The element's position in the merged document.
        index: usize,
        /// The register the element declares as its output.
        output: u64,
        /// The channel-map validation failure. Its `PointId`s are the
        /// register addresses the document bound — the [`Display`]
        /// impl words them as registers.
        error: ConfigError,
    },
}

/// `error`'s text carried onto register wording: the document's
/// endpoints bind registers, so the channel map's "point N" names
/// register N.
fn describe_element_error(error: &ConfigError) -> String {
    match error {
        ConfigError::UnknownPoint(point) => format!(
            "the element names register {}, which the device does not declare",
            point.0
        ),
        ConfigError::ElementPointKind { point, kind } => format!(
            "process element ends are Float registers, but register {} is {kind:?}",
            point.0
        ),
        ConfigError::ElementGateKind { point, kind } => format!(
            "a bool_flow element's gate must read a Bool register, but register {} is {kind:?}",
            point.0
        ),
        ConfigError::ElementContactKind { point, kind } => format!(
            "a threshold element's contact must drive a Bool register, but register {} is {kind:?}",
            point.0
        ),
        ConfigError::InvalidTimeConstant { point, value } => format!(
            "lag driving register {} has non-positive or non-finite time constant {value}",
            point.0
        ),
        ConfigError::InvalidDamping { point, value } => format!(
            "second-order lag driving register {} has non-positive or non-finite damping ratio {value}",
            point.0
        ),
        ConfigError::InvalidDelay { point, value } => format!(
            "dead-time element driving register {} has non-positive or non-finite delay {value}",
            point.0
        ),
        ConfigError::InvalidAmplitude { point, value } => format!(
            "noise element driving register {} has negative or non-finite amplitude {value}",
            point.0
        ),
        ConfigError::InvalidRate { point, rate, value } => format!(
            "bool_flow element driving register {} has non-finite {rate} {value}",
            point.0
        ),
        ConfigError::NonFiniteBias { point, value } => format!(
            "flow_sum element driving register {} has non-finite bias {value}",
            point.0
        ),
        ConfigError::InvalidGain { point, value } => format!(
            "scaled_flow element driving register {} has non-finite gain {value}",
            point.0
        ),
        ConfigError::InvalidBound {
            point,
            bound,
            value,
        } => format!(
            "threshold element driving register {} has non-finite {bound} bound {value}",
            point.0
        ),
        ConfigError::NonPositiveBand { point, on, off } => format!(
            "threshold element driving register {} declares no hysteresis band: on {on} equals off {off}",
            point.0
        ),
        ConfigError::NonFiniteInitial { point, value } => format!(
            "element driving register {} has non-finite initial value {value}",
            point.0
        ),
        ConfigError::ConflictingDriver { point } => {
            format!("register {} is driven by more than one element", point.0)
        }
        // Duplicate-point, duplicate-channel, and loopback errors
        // cannot arise from a register-built map: declarations are
        // deduplicated up front, channels are generated, and the bank
        // serves no loopbacks.
        other => other.to_string(),
    }
}

impl fmt::Display for DynamicsError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Register(error) => error.fmt(f),
            Self::Element {
                index,
                output,
                error,
            } => write!(
                f,
                "dynamics element {index} (driving register {output}) is invalid: {}",
                describe_element_error(error)
            ),
        }
    }
}

impl std::error::Error for DynamicsError {}

/// The declarations' channel-map form: each register binds the point
/// of the same number, so the merged element vocabulary validates and
/// steps unchanged.
///
/// Registers carry no direction — the map declares every binding `In`
/// uniformly, meaningful only to loopbacks the bank does not serve.
/// Channel identity is diagnostics bookkeeping; the generated channel
/// names the register.
fn channel_map(decls: impl IntoIterator<Item = RegisterDecl>) -> Result<ChannelMap, BankError> {
    let mut taken = BTreeMap::new();
    let mut map = ChannelMap::new();
    for decl in decls {
        match taken.entry(decl.register) {
            Entry::Occupied(_) => return Err(BankError::DuplicateRegister(decl.register)),
            Entry::Vacant(slot) => {
                slot.insert(());
            }
        }
        map.points.push(PointBinding {
            point: PointId(u64::from(decl.register)),
            channel: ChannelId {
                device: 0,
                name: decl.register.to_string(),
            },
            direction: Direction::In,
            initial: decl.initial,
        });
    }
    Ok(map)
}

/// A register-mapped field device's storage.
///
/// The bank is a [`SimDriver`] whose point ids are the register
/// addresses — register `r` binds `PointId(r)` — so the driver's
/// stepping, element evaluation, and quality conventions are the
/// bank's own unchanged. The driver's internal mutex serializes every
/// request the server's per-connection handler threads issue, and
/// nothing in it reads a clock or a random source.
///
/// `wires` is the declared field wiring — `(driven, observing)`
/// register pairs [`with_wires`](Self::with_wires) validated — which
/// every write follows before returning.
pub struct RegisterBank {
    driver: SimDriver,
    wires: Vec<(u16, u16)>,
}

impl RegisterBank {
    /// Builds a bank from register declarations, or reports the first
    /// [`BankError`]. Every register starts at its declared initial with
    /// [`Quality::Good`](dcs_core::Quality::Good) at [`Tick::ZERO`].
    pub fn new(decls: impl IntoIterator<Item = RegisterDecl>) -> Result<Self, BankError> {
        let map = channel_map(decls)?;
        Self::serve(map, Vec::new()).map_err(|error| match error {
            DynamicsError::Register(error) => error,
            // A declarations-only map merges no elements, so no element
            // failure can arise.
            DynamicsError::Element { .. } => unreachable!("no elements were merged"),
        })
    }

    /// Builds a bank serving `decls` plus declared dynamics, or reports
    /// the first [`DynamicsError`].
    ///
    /// `elements` is the same [`ProcessElement`] list a
    /// `dcs-plant-server --dynamics` document carries, bound to
    /// registers: every point-valued field carries a register address.
    /// Each element is merged and validated in document order, so a
    /// rejection names the element — its position and the register it
    /// drives — and the offending register or constant. An element's
    /// `initial` seeds its output register, superseding the declared
    /// register initial; a register may be driven by at most one
    /// element.
    pub fn with_dynamics(
        decls: impl IntoIterator<Item = RegisterDecl>,
        elements: Vec<ProcessElement>,
    ) -> Result<Self, DynamicsError> {
        let mut map = channel_map(decls).map_err(DynamicsError::Register)?;
        for (index, element) in elements.into_iter().enumerate() {
            let output = element.output().0;
            map.elements.push(element);
            map.validate().map_err(|error| DynamicsError::Element {
                index,
                output,
                error,
            })?;
        }
        Self::serve(map, Vec::new())
    }

    /// Declares the bank's field wiring and returns it.
    ///
    /// Each `(driven, observing)` pair is a wire between terminals:
    /// every write landing on `driven` — a point-wise
    /// [`write`](Self::write) or an exchange-published output — also
    /// lands on `observing` in the same request, so an exchange
    /// publishing a looped-back output answers the wired input's
    /// transition in that exchange's own census. Wires cascade — a
    /// write a wire carried onto another wired register follows that
    /// register's own wire — and a wiring cycle terminates at the
    /// register first visited, so a request never loops.
    ///
    /// The declaration is the device's own field wiring — the analogue
    /// of the physical terminal wiring a manifest records — not a
    /// controller-side route: it applies inside the exchange's publish
    /// step, before the census latches.
    ///
    /// Fails with [`BankError::UnknownWireRegister`] when an end names a
    /// register the bank does not declare,
    /// [`BankError::WireKindMismatch`] when the ends carry different
    /// value kinds, or [`BankError::SelfWire`] on a degenerate loop.
    pub fn with_wires(
        mut self,
        wires: impl IntoIterator<Item = (u16, u16)>,
    ) -> Result<Self, BankError> {
        let wires: Vec<(u16, u16)> = wires.into_iter().collect();
        for &(driven, observing) in &wires {
            if driven == observing {
                return Err(BankError::SelfWire(driven));
            }
            let (Ok(driven_sample), Ok(observing_sample)) =
                (self.read(driven), self.read(observing))
            else {
                return Err(BankError::UnknownWireRegister { driven, observing });
            };
            if driven_sample.value.kind() != observing_sample.value.kind() {
                return Err(BankError::WireKindMismatch { driven, observing });
            }
        }
        self.wires = wires;
        Ok(self)
    }

    /// The driver serving a fully validated register map.
    fn serve(map: ChannelMap, wires: Vec<(u16, u16)>) -> Result<Self, DynamicsError> {
        // Every inconsistency the map could carry was already reported:
        // declarations deduplicated by `channel_map`, elements validated
        // as they merged — `new`'s element-free map cannot fail either.
        Ok(Self {
            driver: SimDriver::new(map).expect("the merged register map validated"),
            wires,
        })
    }

    /// The bank's current logical tick.
    pub fn tick(&self) -> Tick {
        self.driver.tick()
    }

    /// Reads one register's stored sample, or
    /// [`BusError::UnknownRegister`]. The reported sample carries any
    /// quality a [`inject_quality`](Self::inject_quality) stamped.
    pub fn read(&self, register: u16) -> Result<Sample, BusError> {
        match self.driver.read(PointId(u64::from(register))) {
            Ok(sample) => Ok(sample),
            Err(IoError::UnknownPoint(_)) => Err(BusError::UnknownRegister { register }),
            // The bank injects quality faults only, and they never
            // fail a read.
            Err(_) => unreachable!("the bank injects no error faults"),
        }
    }

    /// Writes `value` to `register`, stamping it with the current tick,
    /// and returns that tick. A real write stores a `Good` sample, so
    /// it overwrites a standing quality injection.
    ///
    /// The write then follows the declared field wiring: each wire the
    /// register drives lands the same value on its observing register —
    /// a cascade through wired registers that terminates at the first
    /// already-visited register, so a wiring cycle never loops.
    ///
    /// Fails with [`BusError::UnknownRegister`] when the device serves
    /// no such register and [`BusError::KindMismatch`] when `value`'s
    /// kind differs from the register's declared kind — never a silent
    /// coercion.
    pub fn write(&self, register: u16, value: Value) -> Result<Tick, BusError> {
        let mut visited = BTreeSet::from([register]);
        let mut pending = vec![(register, value)];
        while let Some((register, value)) = pending.pop() {
            self.write_one(register, value)?;
            for &(driven, observing) in &self.wires {
                if driven == register && visited.insert(observing) {
                    pending.push((observing, value));
                }
            }
        }
        Ok(self.driver.tick())
    }

    /// Writes one register without following the field wiring — the
    /// step [`write`](Self::write) repeats per landed register.
    fn write_one(&self, register: u16, value: Value) -> Result<(), BusError> {
        let point = PointId(u64::from(register));
        match self.driver.write(point, value) {
            Ok(()) => {
                // The written sample is Good; drop the quality
                // injection it overwrites.
                let _ = self.driver.clear_fault(point);
                Ok(())
            }
            Err(IoError::UnknownPoint(_)) => Err(BusError::UnknownRegister { register }),
            Err(IoError::TypeMismatch {
                expected, found, ..
            }) => Err(BusError::KindMismatch {
                register,
                expected,
                found,
            }),
            // The bank injects quality faults only, and they never
            // refuse a write.
            Err(_) => unreachable!("the bank injects no error faults"),
        }
    }

    /// Stamps `register`'s reported sample with `quality` — the fault
    /// injection behind
    /// [`BusRequest::InjectQuality`](crate::BusRequest::InjectQuality).
    /// The stored value and tick are untouched: the injection declares
    /// how much the stored value can be trusted, it does not re-observe
    /// it. The declared quality stands on the sample — and so on every
    /// read and census — until [`clear_quality`](Self::clear_quality) or
    /// a real [`write`](Self::write), which stores a `Good` sample,
    /// overwrites it.
    ///
    /// Fails with [`BusError::UnknownRegister`] when the device serves
    /// no such register.
    pub fn inject_quality(&self, register: u16, quality: Quality) -> Result<(), BusError> {
        match self
            .driver
            .inject_fault(PointId(u64::from(register)), Fault::Quality(quality))
        {
            Ok(()) => Ok(()),
            Err(IoError::UnknownPoint(_)) => Err(BusError::UnknownRegister { register }),
            // `inject_fault` names only an unbound point.
            Err(_) => unreachable!("inject_fault fails only on an unknown point"),
        }
    }

    /// Restores `register`'s reported sample to
    /// [`Quality::Good`](dcs_core::Quality::Good) — the clear half of
    /// the injection pair, behind
    /// [`BusRequest::ClearQuality`](crate::BusRequest::ClearQuality).
    /// Clearing a register carrying no injection leaves the `Good` it
    /// already reports.
    ///
    /// Fails with [`BusError::UnknownRegister`] when the device serves
    /// no such register.
    pub fn clear_quality(&self, register: u16) -> Result<(), BusError> {
        match self.driver.clear_fault(PointId(u64::from(register))) {
            Ok(()) => Ok(()),
            Err(IoError::UnknownPoint(_)) => Err(BusError::UnknownRegister { register }),
            // `clear_fault` names only an unbound point.
            Err(_) => unreachable!("clear_fault fails only on an unknown point"),
        }
    }

    /// Advances the bank's tick by one and returns it — the explicit
    /// step behind [`BusRequest::Step`](crate::BusRequest::Step).
    ///
    /// The step also advances every merged dynamics element by the
    /// caller-supplied `dt`, in declaration order, following the
    /// element vocabulary's own rules: a `Good` input advances the
    /// element — a `bool_flow` stands its `on_rate` or `off_rate` by
    /// its `Bool` gate, a `flow_sum` sums its declared inputs plus
    /// `bias`, a `scaled_flow` stands at `gain` times its `Float`
    /// input, an `integrator` accumulates `u·dt`, a `threshold`
    /// evaluates its `Float` input against the declared `on`/`off`
    /// bounds and drives the contact onto its `Bool` register — and
    /// stamps its
    /// output register `Good`; a non-`Good` input freezes the element
    /// and propagates its quality to the output register's sample, a
    /// `flow_sum` propagating the worst of its inputs' qualities.
    /// Element state is bank state: it lives here for the bank's
    /// lifetime, shared by every attachment like the `dcs-sim-net`
    /// plant — never checkpointed controller state.
    ///
    /// `dt` must be finite and non-negative.
    ///
    /// # Panics
    ///
    /// Panics when `dt` is negative or non-finite; the server refuses
    /// such a step request with [`BusError::InvalidRequest`] before it
    /// reaches the bank.
    pub fn step(&self, dt: f64) -> Tick {
        self.driver.step(dt)
    }

    /// Every register's address and stored sample, ordered by address —
    /// the census [`BusRequest::ListRegisters`](crate::BusRequest::ListRegisters)
    /// answers with.
    pub fn registers(&self) -> Vec<RegisterInfo> {
        self.driver
            .points()
            .iter()
            .map(|info| RegisterInfo {
                // Every bound point came from a `u16` register
                // declaration.
                register: info.point.0 as u16,
                sample: info.sample,
            })
            .collect()
    }
}

impl fmt::Debug for RegisterBank {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("RegisterBank")
            .field("tick", &self.tick())
            .field("registers", &self.registers().len())
            .finish()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use dcs_core::{QualityReason, ValueKind};
    use dcs_sim::{BoolFlow, FirstOrderLag, FlowSum, Integrator, ScaledFlow, Threshold};

    fn bank() -> RegisterBank {
        RegisterBank::new([
            RegisterDecl {
                register: 0,
                initial: Value::Float(1.5),
            },
            RegisterDecl {
                register: 4,
                initial: Value::Bool(false),
            },
        ])
        .unwrap()
    }

    #[test]
    fn write_then_read_stamps_the_current_tick() {
        let bank = bank();
        assert_eq!(
            bank.read(0).unwrap(),
            Sample::good(Value::Float(1.5), Tick(0))
        );

        let tick = bank.write(0, Value::Float(2.5)).unwrap();
        assert_eq!(tick, Tick(0));
        assert_eq!(
            bank.read(0).unwrap(),
            Sample::good(Value::Float(2.5), Tick(0))
        );

        assert_eq!(bank.step(0.1), Tick(1));
        assert_eq!(bank.write(0, Value::Float(3.5)).unwrap(), Tick(1));
        assert_eq!(
            bank.read(0).unwrap(),
            Sample::good(Value::Float(3.5), Tick(1))
        );
        // A register untouched by the step keeps its stamped tick.
        assert_eq!(
            bank.read(4).unwrap(),
            Sample::good(Value::Bool(false), Tick(0))
        );
    }

    #[test]
    fn errors_are_named() {
        let bank = bank();
        assert_eq!(bank.read(9), Err(BusError::UnknownRegister { register: 9 }));
        assert_eq!(
            bank.write(0, Value::Bool(true)),
            Err(BusError::KindMismatch {
                register: 0,
                expected: ValueKind::Float,
                found: Value::Bool(true),
            })
        );
        // The failed write leaves the stored value untouched.
        assert_eq!(bank.read(0).unwrap().value, Value::Float(1.5));
    }

    #[test]
    fn injected_quality_stands_until_cleared_or_overwritten() {
        let bank = bank();
        bank.write(0, Value::Float(2.5)).unwrap();

        // The injection stamps the reported sample's quality, leaving
        // its value and tick untouched.
        bank.inject_quality(0, Quality::Bad(QualityReason::DeviceFault))
            .unwrap();
        assert_eq!(
            bank.read(0).unwrap(),
            Sample::new(
                Value::Float(2.5),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(0),
            )
        );
        // The census reports the stamped sample too.
        assert_eq!(
            bank.registers()[0].sample.quality,
            Quality::Bad(QualityReason::DeviceFault)
        );

        // The clear restores Good on the same stored value and tick.
        bank.clear_quality(0).unwrap();
        assert_eq!(
            bank.read(0).unwrap(),
            Sample::good(Value::Float(2.5), Tick(0))
        );

        // A real write overwrites an injection: the new sample is Good.
        bank.inject_quality(0, Quality::Uncertain(QualityReason::Stale))
            .unwrap();
        bank.write(0, Value::Float(3.5)).unwrap();
        assert_eq!(
            bank.read(0).unwrap(),
            Sample::good(Value::Float(3.5), Tick(0))
        );

        // Clearing a register carrying no injection is a no-op, and
        // both operations name an unserved register.
        bank.clear_quality(4).unwrap();
        assert_eq!(
            bank.read(4).unwrap(),
            Sample::good(Value::Bool(false), Tick(0))
        );
        assert_eq!(
            bank.inject_quality(9, Quality::Good),
            Err(BusError::UnknownRegister { register: 9 })
        );
        assert_eq!(
            bank.clear_quality(9),
            Err(BusError::UnknownRegister { register: 9 })
        );
    }

    #[test]
    fn duplicate_registers_are_rejected() {
        let decls = [
            RegisterDecl {
                register: 0,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 0,
                initial: Value::Float(1.0),
            },
        ];
        assert_eq!(
            RegisterBank::new(decls).map(|_| ()),
            Err(BankError::DuplicateRegister(0))
        );
        assert_eq!(
            RegisterBank::with_dynamics(decls, Vec::new()).map(|_| ()),
            Err(DynamicsError::Register(BankError::DuplicateRegister(0)))
        );
    }

    #[test]
    fn census_is_ordered_by_address() {
        let bank = RegisterBank::new([
            RegisterDecl {
                register: 7,
                initial: Value::Int(0),
            },
            RegisterDecl {
                register: 2,
                initial: Value::Bool(false),
            },
        ])
        .unwrap();
        let registers = bank.registers();
        assert_eq!(registers[0].register, 2);
        assert_eq!(registers[1].register, 7);
    }

    /// The station-loop fixture: a Bool-gated pump draw on command
    /// register 20 driving flow register 12, summed with a declared
    /// inflow bias into net register 14, integrated into level register
    /// 10.
    fn station_decls() -> Vec<RegisterDecl> {
        vec![
            RegisterDecl {
                register: 10,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 12,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 14,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 20,
                initial: Value::Bool(false),
            },
            RegisterDecl {
                register: 21,
                initial: Value::Bool(false),
            },
        ]
    }

    fn station_elements() -> Vec<ProcessElement> {
        vec![
            ProcessElement::BoolFlow(BoolFlow {
                input: PointId(20),
                output: PointId(12),
                on_rate: -10.0,
                off_rate: 0.0,
                initial: 0.0,
            }),
            ProcessElement::FlowSum(FlowSum {
                inputs: vec![PointId(12)],
                output: PointId(14),
                bias: 4.0,
                initial: 4.0,
            }),
            ProcessElement::Integrator(Integrator {
                input: PointId(14),
                output: PointId(10),
                initial: 50.0,
            }),
        ]
    }

    #[test]
    fn declared_dynamics_close_the_loop_over_registers() {
        let bank = RegisterBank::with_dynamics(station_decls(), station_elements()).unwrap();
        // Element initials seed the registers they drive.
        assert_eq!(bank.read(10).unwrap().value, Value::Float(50.0));
        assert_eq!(bank.read(14).unwrap().value, Value::Float(4.0));

        // With the gate released the well fills at the inflow rate.
        bank.step(0.5);
        assert_eq!(bank.read(10).unwrap().value, Value::Float(52.0));
        assert_eq!(bank.read(12).unwrap().value, Value::Float(0.0));

        // The command standing, the pump draws the level down.
        bank.write(20, Value::Bool(true)).unwrap();
        bank.step(0.5);
        assert_eq!(bank.read(12).unwrap().value, Value::Float(-10.0));
        assert_eq!(bank.read(14).unwrap().value, Value::Float(-6.0));
        assert_eq!(bank.read(10).unwrap().value, Value::Float(49.0));
    }

    #[test]
    fn a_non_good_input_freezes_the_element_and_propagates_quality() {
        let bank = RegisterBank::with_dynamics(station_decls(), station_elements()).unwrap();
        bank.inject_quality(20, Quality::Bad(QualityReason::DeviceFault))
            .unwrap();
        bank.step(0.5);
        // The bool_flow's frozen output holds its last value at the
        // injected quality — and the propagated quality freezes each
        // downstream element in turn: the sum and the level hold too.
        assert_eq!(
            bank.read(12).unwrap(),
            Sample::new(
                Value::Float(0.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1)
            )
        );
        assert_eq!(
            bank.read(14).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        assert_eq!(
            bank.read(10).unwrap(),
            Sample::new(
                Value::Float(50.0),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(1)
            )
        );
    }

    #[test]
    fn a_faulted_flow_input_propagates_worst_quality_through_the_sum() {
        let bank = RegisterBank::with_dynamics(station_decls(), station_elements()).unwrap();
        // Fault the summed flow itself: the sum's output register
        // reports it and the level freezes with it.
        bank.inject_quality(12, Quality::Uncertain(QualityReason::Stale))
            .unwrap();
        bank.step(0.5);
        assert_eq!(
            bank.read(14).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        assert_eq!(
            bank.read(10).unwrap().quality,
            Quality::Uncertain(QualityReason::Stale)
        );
        assert_eq!(bank.read(10).unwrap().value, Value::Float(50.0));
    }

    #[test]
    fn invalid_elements_are_named_errors_naming_the_element() {
        let decls = station_decls();
        // An endpoint the device does not serve.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::Integrator(Integrator {
                input: PointId(14),
                output: PointId(99),
                initial: 0.0,
            })],
        )
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "dynamics element 0 (driving register 99) is invalid: the element names register 99, which the device does not declare"
        );
        // A wrongly-kinded endpoint: a float element end on the Bool
        // command register.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::Integrator(Integrator {
                input: PointId(20),
                output: PointId(10),
                initial: 0.0,
            })],
        )
        .unwrap_err();
        assert!(error.to_string().contains("register 20 is Bool"), "{error}");
        // A bool_flow gate on a Float register.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::BoolFlow(BoolFlow {
                input: PointId(14),
                output: PointId(12),
                on_rate: 1.0,
                off_rate: 0.0,
                initial: 0.0,
            })],
        )
        .unwrap_err();
        assert!(
            error.to_string().contains("must read a Bool register"),
            "{error}"
        );
        // A non-finite rate names the element and its register.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::BoolFlow(BoolFlow {
                input: PointId(20),
                output: PointId(12),
                on_rate: f64::NAN,
                off_rate: 0.0,
                initial: 0.0,
            })],
        )
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "dynamics element 0 (driving register 12) is invalid: bool_flow element driving register 12 has non-finite on_rate NaN"
        );
        // A non-finite scaled_flow gain names the element and its
        // register the same way.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::ScaledFlow(ScaledFlow {
                input: PointId(14),
                output: PointId(12),
                gain: f64::INFINITY,
                initial: 0.0,
            })],
        )
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "dynamics element 0 (driving register 12) is invalid: scaled_flow element driving register 12 has non-finite gain inf"
        );
        // A negative time constant, and an output register two
        // elements contest.
        let mut contested = station_elements();
        contested.push(ProcessElement::FirstOrderLag(FirstOrderLag {
            input: PointId(14),
            output: PointId(10),
            time_constant: -1.0,
            initial: 0.0,
        }));
        let error = RegisterBank::with_dynamics(decls.clone(), contested).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("dynamics element 3 (driving register 10)"),
            "{error}"
        );

        // A threshold's contact on a Float register names the element
        // and the register it drives.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::Threshold(Threshold {
                input: PointId(10),
                output: PointId(12),
                on: 8.0,
                off: 7.5,
                initial: false,
            })],
        )
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "dynamics element 0 (driving register 12) is invalid: a threshold element's contact must drive a Bool register, but register 12 is Float"
        );
        // A non-finite bound names the element, its register, and the
        // bound that offended.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::Threshold(Threshold {
                input: PointId(10),
                output: PointId(20),
                on: f64::NAN,
                off: 7.5,
                initial: false,
            })],
        )
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "dynamics element 0 (driving register 20) is invalid: threshold element driving register 20 has non-finite on bound NaN"
        );
        // Equal bounds declare no hysteresis band.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::Threshold(Threshold {
                input: PointId(10),
                output: PointId(20),
                on: 7.5,
                off: 7.5,
                initial: false,
            })],
        )
        .unwrap_err();
        assert_eq!(
            error.to_string(),
            "dynamics element 0 (driving register 20) is invalid: threshold element driving register 20 declares no hysteresis band: on 7.5 equals off 7.5"
        );
        // And a threshold input on a Bool register is the
        // vocabulary's ordinary Float-end rejection.
        let error = RegisterBank::with_dynamics(
            decls.clone(),
            vec![ProcessElement::Threshold(Threshold {
                input: PointId(20),
                output: PointId(21),
                on: 8.0,
                off: 7.5,
                initial: false,
            })],
        )
        .unwrap_err();
        assert!(error.to_string().contains("register 20 is Bool"), "{error}");
    }

    /// The protection-loop document — the shared fixture both
    /// `--dynamics` seams merge: a `threshold` on level register 10
    /// driving the `sis-active` Bool register 30, gating the
    /// `bool_flow` emergency draw on 12, summed with the inflow on 11
    /// into net register 13, integrated back into the level.
    const PROTECTION_DYNAMICS: &str =
        include_str!("../../dcs-sim/fixtures/protection_dynamics.json");

    fn protection_decls() -> Vec<RegisterDecl> {
        vec![
            RegisterDecl {
                register: 10,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 11,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 12,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 13,
                initial: Value::Float(0.0),
            },
            RegisterDecl {
                register: 30,
                initial: Value::Bool(false),
            },
        ]
    }

    fn protection_elements() -> Vec<ProcessElement> {
        serde_json::from_str(PROTECTION_DYNAMICS).unwrap()
    }

    #[test]
    fn a_declared_threshold_drives_the_contact_register_over_the_bank() {
        let bank = RegisterBank::with_dynamics(protection_decls(), protection_elements()).unwrap();
        // Element initials seed the registers they drive: the level at
        // the integrator's initial, the contact released.
        assert_eq!(bank.read(10).unwrap().value, Value::Float(6.0));
        assert_eq!(bank.read(30).unwrap().value, Value::Bool(false));

        // The level climbing past `on` asserts the contact; the gated
        // draw engages on the same step and pulls the level back.
        bank.step(1.0);
        assert_eq!(bank.read(10).unwrap().value, Value::Float(10.0));
        assert_eq!(bank.read(30).unwrap().value, Value::Bool(false));
        bank.step(1.0);
        assert_eq!(bank.read(30).unwrap().value, Value::Bool(true));
        assert_eq!(bank.read(12).unwrap().value, Value::Float(-20.0));
        assert_eq!(bank.read(10).unwrap().value, Value::Float(-6.0));

        // Back below `off`, the contact releases and the draw stops.
        bank.step(1.0);
        assert_eq!(bank.read(30).unwrap().value, Value::Bool(false));
        assert_eq!(bank.read(12).unwrap().value, Value::Float(0.0));
    }

    #[test]
    fn a_non_good_threshold_input_holds_the_contact_and_propagates_quality() {
        let bank = RegisterBank::with_dynamics(protection_decls(), protection_elements()).unwrap();
        // Push the level past `on` so the contact stands asserted.
        bank.step(1.0);
        bank.step(1.0);
        assert_eq!(bank.read(30).unwrap().value, Value::Bool(true));

        // Fault the level register: the threshold freezes its standing
        // contact and stamps the injected quality on it — and the
        // propagated quality freezes the gated draw in turn.
        bank.inject_quality(10, Quality::Bad(QualityReason::DeviceFault))
            .unwrap();
        bank.step(1.0);
        assert_eq!(
            bank.read(30).unwrap(),
            Sample::new(
                Value::Bool(true),
                Quality::Bad(QualityReason::DeviceFault),
                Tick(3)
            )
        );
        assert_eq!(
            bank.read(12).unwrap().quality,
            Quality::Bad(QualityReason::DeviceFault)
        );
        assert_eq!(bank.read(12).unwrap().value, Value::Float(-20.0));
    }

    /// The wired-rig fixture: an output register wired onto an input
    /// register — the DO→DI loopback a rig's field wiring plays.
    fn wired_bank() -> RegisterBank {
        RegisterBank::new([
            RegisterDecl {
                register: 0,
                initial: Value::Bool(false),
            },
            RegisterDecl {
                register: 2,
                initial: Value::Bool(false),
            },
            RegisterDecl {
                register: 4,
                initial: Value::Float(0.0),
            },
        ])
        .unwrap()
        .with_wires([(0, 2)])
        .unwrap()
    }

    #[test]
    fn a_declared_wire_lands_each_write_on_the_observing_register() {
        let bank = wired_bank();

        // The write's tick stamps both ends — the wired register's
        // sample is the same request's, not a later step's.
        bank.write(0, Value::Bool(true)).unwrap();
        assert_eq!(
            bank.read(2).unwrap(),
            Sample::good(Value::Bool(true), Tick(0))
        );

        // The wire re-asserts on every write, at whatever tick the
        // bank stands on.
        bank.step(0.5);
        bank.write(0, Value::Bool(false)).unwrap();
        assert_eq!(
            bank.read(2).unwrap(),
            Sample::good(Value::Bool(false), Tick(1))
        );

        // And a direct write to the observing register stands until
        // the next driven write — the wire is field wiring, not a
        // read-time alias.
        bank.write(2, Value::Bool(true)).unwrap();
        assert_eq!(bank.read(2).unwrap().value, Value::Bool(true));
    }

    #[test]
    fn wires_cascade_and_a_cycle_terminates() {
        let bank = RegisterBank::new([
            RegisterDecl {
                register: 0,
                initial: Value::Bool(false),
            },
            RegisterDecl {
                register: 1,
                initial: Value::Bool(false),
            },
            RegisterDecl {
                register: 2,
                initial: Value::Bool(false),
            },
        ])
        .unwrap()
        .with_wires([(0, 1), (1, 2)])
        .unwrap();
        bank.write(0, Value::Bool(true)).unwrap();
        for register in [0, 1, 2] {
            assert_eq!(bank.read(register).unwrap().value, Value::Bool(true));
        }

        // A wiring loop settles instead of hanging the request: each
        // register takes the write once.
        let bank = RegisterBank::new([
            RegisterDecl {
                register: 0,
                initial: Value::Bool(false),
            },
            RegisterDecl {
                register: 1,
                initial: Value::Bool(false),
            },
        ])
        .unwrap()
        .with_wires([(0, 1), (1, 0)])
        .unwrap();
        bank.write(0, Value::Bool(true)).unwrap();
        assert_eq!(bank.read(1).unwrap().value, Value::Bool(true));
    }

    #[test]
    fn invalid_wires_are_named_errors() {
        let decls = || {
            RegisterBank::new([
                RegisterDecl {
                    register: 0,
                    initial: Value::Bool(false),
                },
                RegisterDecl {
                    register: 4,
                    initial: Value::Float(0.0),
                },
            ])
            .unwrap()
        };
        // An end the bank does not serve.
        assert_eq!(
            decls().with_wires([(0, 9)]).map(|_| ()),
            Err(BankError::UnknownWireRegister {
                driven: 0,
                observing: 9
            })
        );
        // Ends of different kinds.
        assert_eq!(
            decls().with_wires([(0, 4)]).map(|_| ()),
            Err(BankError::WireKindMismatch {
                driven: 0,
                observing: 4
            })
        );
        // A register looped onto itself.
        assert_eq!(
            decls().with_wires([(0, 0)]).map(|_| ()),
            Err(BankError::SelfWire(0))
        );
    }
}
