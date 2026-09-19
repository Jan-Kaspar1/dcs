//! The no-hardware transport seam: a scripted [`BusTransport`]
//! substitute the contract tests drive exactly like the real one.
//!
//! [`FakeTransport`] answers `discovered` with the stations it was
//! given, records every call on a shared [`FakeLog`], and answers
//! `exchange` from a scripted [`FakeCycle`] queue — each script entry
//! declares the cycle outcome and the input image the cycle returns.
//! Nothing here touches a socket, so `dcs-ethercat`'s entire contract
//! surface — startup verification, exchange semantics, failure
//! accounting, recovery re-entry — is proven without hardware, and
//! the same test doubles drive the assembly integration tests.

use crate::transport::{BusTransport, CycleOutcome, DiscoveredStation, TransportError};
use dcs_core::LinkState;
use std::collections::VecDeque;
use std::sync::{Arc, Mutex};

/// One scripted exchange's answer: the cycle outcome and the input
/// image it returns. Entries beyond the script's end repeat a clean
/// `Complete` with whatever input bytes were last given — or zeroes.
#[derive(Debug)]
pub struct FakeCycle {
    /// The outcome the exchange reports.
    pub outcome: Result<CycleOutcome, TransportError>,
    /// The input image the exchange fills; `None` leaves the buffer —
    /// and therefore every latched input — at its previous bytes.
    pub inputs: Option<Vec<u8>>,
}

impl FakeCycle {
    /// A clean exchange returning `inputs` as the input image.
    pub fn complete(inputs: impl Into<Vec<u8>>) -> Self {
        Self {
            outcome: Ok(CycleOutcome::Complete),
            inputs: Some(inputs.into()),
        }
    }

    /// A completed exchange past the deadline returning `inputs`.
    pub fn late(inputs: impl Into<Vec<u8>>) -> Self {
        Self {
            outcome: Ok(CycleOutcome::Late),
            inputs: Some(inputs.into()),
        }
    }

    /// A working-counter shortfall attributed to `station` — `None`
    /// for an unattributable shortfall — still returning `inputs`.
    pub fn short(station: Option<usize>, inputs: impl Into<Vec<u8>>) -> Self {
        Self {
            outcome: Ok(CycleOutcome::Short { station }),
            inputs: Some(inputs.into()),
        }
    }

    /// A failed exchange: nothing published, nothing latched.
    pub fn failed(error: TransportError) -> Self {
        Self {
            outcome: Err(error),
            inputs: None,
        }
    }
}

impl Default for FakeCycle {
    fn default() -> Self {
        Self {
            outcome: Ok(CycleOutcome::Complete),
            inputs: None,
        }
    }
}

/// The transport's call record — how a test proves `exchange` ran
/// exactly once per scan, that `read`/`write` never reached the wire,
/// and what output image each exchange published.
#[derive(Debug, Default)]
pub struct FakeLog {
    /// `enter_op` calls.
    pub enter_ops: usize,
    /// `exchange` calls.
    pub exchanges: usize,
    /// `recover` calls.
    pub recovers: usize,
    /// Every output image an exchange was handed, in order.
    pub published: Vec<Vec<u8>>,
}

/// A scripted [`BusTransport`]: the fake PDU loop. Constructed with
/// the stations discovery reports and an optional exchange script,
/// driven through [`EthercatBuses::with_opener`](crate::EthercatBuses::with_opener)
/// or directly under a [`BusMaster`](crate::BusMaster).
#[derive(Debug)]
pub struct FakeTransport {
    /// The stations `discovered` reports — the bus a test pretends to
    /// be attached to.
    discovered: Vec<DiscoveredStation>,
    /// The link state `link` reports.
    link: std::cell::Cell<LinkState>,
    /// The exchange answers, in order.
    script: VecDeque<FakeCycle>,
    /// The `enter_op` refusal, when a test scripts one.
    enter_op_error: Option<TransportError>,
    /// The `recover` refusal, when a test scripts one.
    recover_error: Option<TransportError>,
    /// The call record.
    log: Arc<Mutex<FakeLog>>,
}

impl FakeTransport {
    /// A fake presenting `stations` that answers every exchange
    /// cleanly with a zeroed input image.
    pub fn new(stations: Vec<DiscoveredStation>) -> Self {
        Self::with_script(stations, VecDeque::new())
    }

    /// A fake presenting `stations` answering exchanges from `script`.
    pub fn with_script(
        stations: Vec<DiscoveredStation>,
        script: impl Into<VecDeque<FakeCycle>>,
    ) -> Self {
        Self::with_log(stations, script, Arc::new(Mutex::new(FakeLog::default())))
    }

    /// A fake reporting to a shared `log` — several fakes' calls land
    /// on one record, as when a test's opener builds each transport.
    pub fn with_log(
        stations: Vec<DiscoveredStation>,
        script: impl Into<VecDeque<FakeCycle>>,
        log: Arc<Mutex<FakeLog>>,
    ) -> Self {
        Self {
            discovered: stations,
            link: std::cell::Cell::new(LinkState::Connected),
            script: script.into(),
            enter_op_error: None,
            recover_error: None,
            log,
        }
    }

    /// The shared call record — live, so a test watches exchanges
    /// happen as the executor drives them.
    pub fn log(&self) -> Arc<Mutex<FakeLog>> {
        Arc::clone(&self.log)
    }

    /// Overrides the link state `link` reports.
    pub fn set_link(&self, link: LinkState) {
        self.link.set(link);
    }

    /// Scripts `enter_op` to refuse with `error` — OP entry failing
    /// after a device verified.
    pub fn fail_enter_op(&mut self, error: TransportError) -> &mut Self {
        self.enter_op_error = Some(error);
        self
    }

    /// Scripts `recover` to refuse with `error` — a re-entry that
    /// itself fails, extending the outage.
    pub fn fail_recover(&mut self, error: TransportError) -> &mut Self {
        self.recover_error = Some(error);
        self
    }
}

impl BusTransport for FakeTransport {
    fn discovered(&self) -> &[DiscoveredStation] {
        &self.discovered
    }

    fn enter_op(&mut self) -> Result<(), TransportError> {
        self.log.lock().unwrap().enter_ops += 1;
        match self.enter_op_error.take() {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    fn exchange(
        &mut self,
        outputs: &[u8],
        inputs: &mut [u8],
    ) -> Result<CycleOutcome, TransportError> {
        let cycle = self.script.pop_front().unwrap_or_default();
        {
            let mut log = self.log.lock().unwrap();
            log.exchanges += 1;
            log.published.push(outputs.to_vec());
        }
        let outcome = cycle.outcome?;
        if let Some(image) = cycle.inputs {
            let kept = image.len().min(inputs.len());
            inputs[..kept].copy_from_slice(&image[..kept]);
        }
        Ok(outcome)
    }

    fn recover(&mut self) -> Result<(), TransportError> {
        self.log.lock().unwrap().recovers += 1;
        match self.recover_error.take() {
            Some(error) => Err(error),
            None => Ok(()),
        }
    }

    fn link(&self) -> LinkState {
        self.link.get()
    }
}
