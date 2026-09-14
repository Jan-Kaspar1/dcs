//! The register bank a [`BusServer`](crate::BusServer) shares: numbered
//! registers holding typed values, stamped by an explicit logical tick.
//!
//! The bank is the simulated fieldbus device's visible state — the
//! register-map analogue of `dcs-sim`'s point storage, addressed by
//! `u16` register index rather than by point.
//! Writes stamp the stored sample with the bank's current tick; the tick
//! advances only on [`step`](RegisterBank::step), so identical write and
//! step sequences produce identical samples on every run.

use crate::protocol::{BusError, RegisterInfo};
use dcs_core::{Sample, Tick, Value};
use std::collections::BTreeMap;
use std::collections::btree_map::Entry;
use std::fmt;
use std::sync::Mutex;

/// One register's declaration: its address and initial value. The
/// initial's [`Value`] variant is the register's declared kind — writes
/// carrying any other variant fail
/// [`BusError::KindMismatch`].
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct RegisterDecl {
    /// The register address.
    pub register: u16,
    /// The register's value from power-on until the first write.
    pub initial: Value,
}

/// Why a [`RegisterBank`] could not be built.
#[derive(Debug, Clone, Copy, PartialEq)]
pub enum BankError {
    /// Two declarations name the same register address.
    DuplicateRegister(u16),
}

impl fmt::Display for BankError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::DuplicateRegister(register) => {
                write!(f, "register {register} is declared more than once")
            }
        }
    }
}

impl std::error::Error for BankError {}

/// The state behind the bank's mutex.
struct BankState {
    /// Register address → stored sample. A register's kind is its stored
    /// value's variant: writes are kind-checked, so the stored kind can
    /// never change.
    registers: BTreeMap<u16, Sample>,
    /// The device's logical tick; writes stamp it and
    /// [`step`](RegisterBank::step) advances it.
    tick: Tick,
}

/// A register-mapped field device's storage.
///
/// The bank uses interior mutability behind a `Mutex`, so the server's
/// per-connection handler threads share one `&RegisterBank` and every
/// request applies atomically. Nothing in the bank reads a clock or a
/// random source.
pub struct RegisterBank {
    state: Mutex<BankState>,
}

impl RegisterBank {
    /// Builds a bank from register declarations, or reports the first
    /// [`BankError`]. Every register starts at its declared initial with
    /// [`Quality::Good`](dcs_core::Quality::Good) at [`Tick::ZERO`].
    pub fn new(decls: impl IntoIterator<Item = RegisterDecl>) -> Result<Self, BankError> {
        let mut registers = BTreeMap::new();
        for decl in decls {
            match registers.entry(decl.register) {
                Entry::Occupied(_) => {
                    return Err(BankError::DuplicateRegister(decl.register));
                }
                Entry::Vacant(slot) => {
                    slot.insert(Sample::good(decl.initial, Tick::ZERO));
                }
            }
        }
        Ok(Self {
            state: Mutex::new(BankState {
                registers,
                tick: Tick::ZERO,
            }),
        })
    }

    /// The bank's current logical tick.
    pub fn tick(&self) -> Tick {
        self.state.lock().unwrap().tick
    }

    /// Reads one register's stored sample, or
    /// [`BusError::UnknownRegister`].
    pub fn read(&self, register: u16) -> Result<Sample, BusError> {
        self.state
            .lock()
            .unwrap()
            .registers
            .get(&register)
            .copied()
            .ok_or(BusError::UnknownRegister { register })
    }

    /// Writes `value` to `register`, stamping it with the current tick,
    /// and returns that tick.
    ///
    /// Fails with [`BusError::UnknownRegister`] when the device serves
    /// no such register and [`BusError::KindMismatch`] when `value`'s
    /// kind differs from the register's declared kind — never a silent
    /// coercion.
    pub fn write(&self, register: u16, value: Value) -> Result<Tick, BusError> {
        let state = &mut *self.state.lock().unwrap();
        let stored = state
            .registers
            .get_mut(&register)
            .ok_or(BusError::UnknownRegister { register })?;
        if value.kind() != stored.value.kind() {
            return Err(BusError::KindMismatch {
                register,
                expected: stored.value.kind(),
                found: value,
            });
        }
        *stored = Sample::good(value, state.tick);
        Ok(state.tick)
    }

    /// Advances the bank's tick by one and returns it — the explicit
    /// step behind [`BusRequest::Step`](crate::BusRequest::Step). The
    /// bank holds values only: stepping moves the stamping clock, no
    /// dynamics.
    pub fn step(&self) -> Tick {
        let state = &mut *self.state.lock().unwrap();
        state.tick = Tick(state.tick.0 + 1);
        state.tick
    }

    /// Every register's address and stored sample, ordered by address —
    /// the census [`BusRequest::ListRegisters`](crate::BusRequest::ListRegisters)
    /// answers with.
    pub fn registers(&self) -> Vec<RegisterInfo> {
        self.state
            .lock()
            .unwrap()
            .registers
            .iter()
            .map(|(&register, &sample)| RegisterInfo { register, sample })
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
    use dcs_core::ValueKind;

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

        assert_eq!(bank.step(), Tick(1));
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
}
