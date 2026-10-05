//! The recorded-link replay seam: a captured EtherCAT exchange stream
//! replayed deterministically against the transport/driver layers, with
//! no NIC and no hardware.
//!
//! ## Why a capture, not a script
//!
//! [`testing::FakeTransport`] proves the cyclic contract from a script
//! a test writes out by hand — one entry per intended outcome. That
//! proves the contract we *intended*. It cannot prove the contract we
//! got: the working-counter shortfalls, mailbox segmentation oddities,
//! and DC-sync drift a real coupler exhibits are exactly the shapes a
//! hand-written script never contains, and until one is recorded, each
//! live-rig incident is a one-off QA finding instead of a permanent
//! regression test.
//!
//! The reference stack this kind's evaluation drew on (`#550`) answers
//! this with pcap replay: `tests/` holds captured link runs replayed as
//! deterministic integration tests, so a known-good exchange, a
//! malformed frame, and a real edge case each become a case that runs
//! on any host with no NIC. This module is the same pattern for DCS's
//! narrower seam. A capture is a JSON document — [`Capture`] —
//! describing the discovered bus and a sequence of [`CycleRecord`]s;
//! [`Capture::opener`] turns one into a transport [`Opener`] and
//! [`Capture::transport`] into a [`ReplayTransport`] any caller can
//! drive.
//!
//! ## Scope
//!
//! Replay exercises the transport and driver layers, **not** live
//! links. A capture is a recorded *answer* sequence, not a decoded frame
//! stream: it says what each cycle returned, not what bytes went on the
//! wire, so it cannot prove the EtherCrab socket path, frame encoding,
//! or timing. Those stay the QA lane's job on real hardware
//! (`docs/lenovo-hardware-qa-plan.md`). What a capture does prove is the
//! named per-outcome accounting above the transport — the
//! `CycleOutcome::Complete`/`Late`/`Short` verdicts, held-image aging,
//! `exchange_miss_threshold` escalation, and staged-image publication —
//! against a stream recorded from a device rather than imagined by a
//! test.
//!
//! ## Capture shape
//!
//! ```json
//! {
//!   "name": "wago-750-354-known-good",
//!   "stations": [
//!     {"position": 0, "name": "WAGO 750-354", "vendor_id": 6,
//!      "product_id": 354, "revision": 3, "input_bytes": 2,
//!      "output_bytes": 2}
//!   ],
//!   "cycles": [
//!     {"outcome": "complete", "inputs": "0000"},
//!     {"outcome": "late", "inputs": "0100"},
//!     {"outcome": "short", "station": 0, "inputs": "0000"},
//!     {"outcome": "failed", "error": "link down: no frame returned"}
//!   ]
//! }
//! ```
//!
//! `inputs` is the returned input image as lowercase hex, optionally
//! `0x`-prefixed; an odd length, a non-hex byte, or an image longer than
//! the bus's input area is a [`CaptureError`] rather than a silently
//! truncated capture. Cycles past the end of the recording repeat its
//! **last** entry, so a capture of `n` cycles can drive an unbounded run
//! — which is what a stable link does in production, and what lets one
//! capture prove an escalation *and* the recovery after it.

use crate::transport::{BusTransport, CycleOutcome, DiscoveredStation, Opener, TransportError};
use dcs_core::LinkState;
use std::collections::VecDeque;
use std::sync::Arc;

/// Why a capture document could not be replayed. Every variant names
/// what was wrong with it — a capture that silently replays as
/// something other than what it records would defeat the point of
/// recording it.
#[derive(Debug, Clone, PartialEq)]
pub enum CaptureError {
    /// The capture document is not valid JSON, or not the object shape
    /// the module documents.
    Malformed(String),
    /// A station record is not usable as a [`DiscoveredStation`].
    Station(String),
    /// A cycle record's `inputs` is not a hex byte string, or is longer
    /// than the bus's input area.
    Image(String),
    /// A cycle record names an outcome the transport seam has no
    /// spelling for.
    Outcome(String),
}

impl std::fmt::Display for CaptureError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Malformed(detail) => write!(f, "the capture is malformed: {detail}"),
            Self::Station(detail) => {
                write!(f, "the capture's station record is unusable: {detail}")
            }
            Self::Image(detail) => write!(f, "the capture's input image is unusable: {detail}"),
            Self::Outcome(detail) => {
                write!(
                    f,
                    "the capture names an outcome the seam has no spelling for: {detail}"
                )
            }
        }
    }
}

impl std::error::Error for CaptureError {}

/// One recorded cycle's answer: the [`CycleOutcome`] the bus reported
/// and the input image the cycle returned.
#[derive(Debug, Clone, PartialEq)]
pub struct CycleRecord {
    /// The outcome the recorded exchange reported.
    pub outcome: Result<CycleOutcome, TransportError>,
    /// The returned input image. [`None`] leaves the buffer — and so
    /// every latched input — at its previous bytes, which is what a
    /// recorded drop looks like.
    pub inputs: Option<Vec<u8>>,
}

impl CycleRecord {
    /// A recorded exchange that completed, returning `inputs`.
    pub fn complete(inputs: impl Into<Vec<u8>>) -> Self {
        Self {
            outcome: Ok(CycleOutcome::Complete),
            inputs: Some(inputs.into()),
        }
    }

    /// A recorded exchange that completed past the deadline.
    pub fn late(inputs: impl Into<Vec<u8>>) -> Self {
        Self {
            outcome: Ok(CycleOutcome::Late),
            inputs: Some(inputs.into()),
        }
    }

    /// A recorded working-counter shortfall — attributed to `station`
    /// when the coupler named one.
    pub fn short(station: Option<usize>, inputs: impl Into<Vec<u8>>) -> Self {
        Self {
            outcome: Ok(CycleOutcome::Short { station }),
            inputs: Some(inputs.into()),
        }
    }

    /// A recorded exchange that did not complete.
    pub fn failed(error: TransportError) -> Self {
        Self {
            outcome: Err(error),
            inputs: None,
        }
    }
}

impl Default for CycleRecord {
    /// A clean exchange leaving the input image untouched — what a
    /// transport with no recording answers.
    fn default() -> Self {
        Self {
            outcome: Ok(CycleOutcome::Complete),
            inputs: None,
        }
    }
}

/// A recorded bus run: what discovery found and what each exchange
/// answered, in order.
#[derive(Debug, Clone, PartialEq)]
pub struct Capture {
    /// The capture's own name — a failing test's identity in its
    /// message.
    pub name: String,
    /// The stations the recorded run discovered, in bus position order.
    pub stations: Vec<DiscoveredStation>,
    /// The recorded exchange answers, in order. Empty is refused: a
    /// capture with no cycles proves nothing about the exchange path.
    pub cycles: Vec<CycleRecord>,
}

impl Capture {
    /// Parses a capture document — the JSON shape the module documents.
    pub fn parse(document: &str) -> Result<Self, CaptureError> {
        let value: serde_json::Value = serde_json::from_str(document)
            .map_err(|error| CaptureError::Malformed(error.to_string()))?;
        let object = value.as_object().ok_or_else(|| {
            CaptureError::Malformed("the capture is not a JSON object".to_string())
        })?;
        let name = object
            .get("name")
            .and_then(|name| name.as_str())
            .ok_or_else(|| CaptureError::Malformed("no `name`".to_string()))?
            .to_string();
        let stations = object
            .get("stations")
            .and_then(|stations| stations.as_array())
            .ok_or_else(|| CaptureError::Malformed("no `stations` array".to_string()))?
            .iter()
            .map(parse_station)
            .collect::<Result<Vec<_>, _>>()?;
        if stations.is_empty() {
            return Err(CaptureError::Station(
                "the capture discovered no stations".to_string(),
            ));
        }
        let input_len: usize = stations.iter().map(|station| station.input_bytes).sum();
        let cycles = object
            .get("cycles")
            .and_then(|cycles| cycles.as_array())
            .ok_or_else(|| CaptureError::Malformed("no `cycles` array".to_string()))?
            .iter()
            .map(|cycle| parse_cycle(cycle, input_len))
            .collect::<Result<Vec<_>, _>>()?;
        if cycles.is_empty() {
            return Err(CaptureError::Malformed(
                "the capture records no cycles".to_string(),
            ));
        }
        Ok(Self {
            name,
            stations,
            cycles,
        })
    }

    /// Parses the capture document at `path` — the same document a
    /// deployment's recorded binding names.
    pub fn load(path: &std::path::Path) -> Result<Self, CaptureError> {
        let document = std::fs::read_to_string(path)
            .map_err(|error| CaptureError::Malformed(format!("{}: {error}", path.display())))?;
        Self::parse(&document)
    }

    /// The capture's station list as the transport reports it.
    pub fn discovered(&self) -> Vec<DiscoveredStation> {
        self.stations.clone()
    }

    /// A fresh [`ReplayTransport`] over this capture.
    pub fn transport(&self) -> ReplayTransport {
        ReplayTransport::new(self.stations.clone(), self.cycles.clone())
    }

    /// A transport [`Opener`] serving this capture, for
    /// [`EthercatBuses::with_opener`](crate::EthercatBuses::with_opener):
    /// the deployment seam bound to a recorded run instead of a host
    /// interface.
    pub fn opener(&self) -> Opener {
        let capture = self.clone();
        Arc::new(move |_request| Ok(Box::new(capture.transport())))
    }

    /// One [`EthercatBuses`](crate::EthercatBuses) serving this capture
    /// on `bus`, bound to `interface` — the recorded counterpart of a
    /// deployment's logical-bus-to-host-interface mapping.
    pub fn buses(&self, bus: &str, interface: &str) -> crate::EthercatBuses {
        crate::EthercatBuses::with_opener(
            std::collections::BTreeMap::from([(bus.to_string(), interface.to_string())]),
            self.opener(),
        )
    }
}

/// One recorded station's discovery record.
fn parse_station(value: &serde_json::Value) -> Result<DiscoveredStation, CaptureError> {
    let number = |key: &str| -> Result<u64, CaptureError> {
        value
            .get(key)
            .and_then(|value| value.as_u64())
            .ok_or_else(|| {
                CaptureError::Station(format!("station record has no unsigned `{key}`: {value}"))
            })
    };
    let length = |key: &str| -> Result<usize, CaptureError> {
        let bytes = number(key)?;
        usize::try_from(bytes).map_err(|_| {
            CaptureError::Station(format!("station `{key}` does not fit a usize: {bytes}"))
        })
    };
    Ok(DiscoveredStation {
        position: number("position")? as usize,
        name: value
            .get("name")
            .and_then(|name| name.as_str())
            .unwrap_or_default()
            .to_string(),
        vendor_id: number("vendor_id")? as u32,
        product_id: number("product_id")? as u32,
        revision: number("revision")? as u32,
        input_bytes: length("input_bytes")?,
        output_bytes: length("output_bytes")?,
    })
}

/// One recorded cycle's answer.
fn parse_cycle(value: &serde_json::Value, input_len: usize) -> Result<CycleRecord, CaptureError> {
    let name = value
        .get("outcome")
        .and_then(|outcome| outcome.as_str())
        .ok_or_else(|| CaptureError::Outcome(format!("the cycle names no `outcome`: {value}")))?;
    let inputs = match value.get("inputs") {
        None | Some(serde_json::Value::Null) => None,
        Some(serde_json::Value::String(text)) => Some(parse_image(text, input_len)?),
        Some(other) => {
            return Err(CaptureError::Image(format!(
                "`inputs` is not a hex string: {other}"
            )));
        }
    };
    let outcome = match name {
        "complete" => Ok(CycleOutcome::Complete),
        "late" => Ok(CycleOutcome::Late),
        "short" => {
            let station = match value.get("station") {
                None | Some(serde_json::Value::Null) => None,
                Some(station) => Some(station.as_u64().ok_or_else(|| {
                    CaptureError::Outcome(format!("`station` is not a bus position: {station}"))
                })? as usize),
            };
            Ok(CycleOutcome::Short { station })
        }
        "failed" | "disconnected" => Err(TransportError::Disconnected(
            value
                .get("error")
                .and_then(|error| error.as_str())
                .unwrap_or("the recorded exchange did not complete")
                .to_string(),
        )),
        "timeout" => Err(TransportError::Timeout(
            value
                .get("error")
                .and_then(|error| error.as_str())
                .unwrap_or("the recorded frame did not return")
                .to_string(),
        )),
        other => {
            return Err(CaptureError::Outcome(format!(
                "`{other}` is not a recorded outcome"
            )));
        }
    };
    Ok(CycleRecord { outcome, inputs })
}

/// A hex byte string into the image bytes it spells.
fn parse_image(text: &str, input_len: usize) -> Result<Vec<u8>, CaptureError> {
    let trimmed = text.strip_prefix("0x").unwrap_or(text);
    if !trimmed.len().is_multiple_of(2) {
        return Err(CaptureError::Image(format!(
            "`{text}` has an odd number of hex digits"
        )));
    }
    let bytes = trimmed
        .as_bytes()
        .chunks(2)
        .map(|pair| {
            let pair = std::str::from_utf8(pair)
                .map_err(|_| CaptureError::Image(format!("`{text}` is not ASCII hex")))?;
            u8::from_str_radix(pair, 16)
                .map_err(|_| CaptureError::Image(format!("`{text}` is not hex")))
        })
        .collect::<Result<Vec<u8>, _>>()?;
    if bytes.len() > input_len {
        return Err(CaptureError::Image(format!(
            "{} bytes returned on a {input_len}-byte input area",
            bytes.len()
        )));
    }
    Ok(bytes)
}

/// A [`BusTransport`] answering from a [`Capture`]: the recorded run,
/// replayed.
///
/// Each `exchange` consumes the next recorded cycle, or repeats the
/// capture's **last** one once the recording is exhausted — a stable
/// link answers the same way, and a recording that ended mid-outage must
/// not recover into a clean `Complete` the capture never recorded. The
/// transport counts the cycles it served so a test can assert how far a
/// run went, and its [`link`](BusTransport::link) reads the *last
/// recorded* outcome rather than replaying history: the recording's tail
/// is what a capture says about the link's state.
#[derive(Debug)]
pub struct ReplayTransport {
    stations: Vec<DiscoveredStation>,
    cycles: VecDeque<CycleRecord>,
    last: Option<CycleRecord>,
    served: usize,
}

impl ReplayTransport {
    /// A transport replaying `cycles` over a bus presenting `stations`.
    /// An empty recording repeats a clean `Complete` — the same
    /// no-cycle behavior the scripted fake has.
    pub fn new(stations: Vec<DiscoveredStation>, cycles: Vec<CycleRecord>) -> Self {
        let mut queue = VecDeque::from(cycles);
        let last = queue.pop_back();
        Self {
            stations,
            cycles: queue,
            last,
            served: 0,
        }
    }

    /// How many exchanges this transport has served — recorded ones and
    /// repeats of the capture's last alike.
    pub fn served(&self) -> usize {
        self.served
    }

    /// The next recorded answer, consumed in order.
    fn advance(&mut self) -> CycleRecord {
        self.served += 1;
        self.cycles
            .pop_front()
            .or_else(|| self.last.clone())
            .unwrap_or_default()
    }

    /// The capture's last recorded outcome — the replayed link's own
    /// reading once the recording is exhausted.
    fn tail_link(&self) -> LinkState {
        match self.cycles.back().or(self.last.as_ref()) {
            None | Some(CycleRecord { outcome: Ok(_), .. }) => LinkState::Connected,
            Some(CycleRecord {
                outcome: Err(_), ..
            }) => LinkState::Disconnected,
        }
    }
}

impl BusTransport for ReplayTransport {
    fn discovered(&self) -> &[DiscoveredStation] {
        &self.stations
    }

    fn enter_op(&mut self) -> Result<(), TransportError> {
        Ok(())
    }

    fn exchange(
        &mut self,
        outputs: &[u8],
        inputs: &mut [u8],
    ) -> Result<CycleOutcome, TransportError> {
        let _ = outputs;
        let cycle = self.advance();
        let outcome = cycle.outcome?;
        if let Some(image) = cycle.inputs {
            let kept = image.len().min(inputs.len());
            inputs[..kept].copy_from_slice(&image[..kept]);
        }
        Ok(outcome)
    }

    fn recover(&mut self) -> Result<(), TransportError> {
        // A recorded run's recovery is the recording's own answer: once
        // the recording is exhausted the link answers again only if its
        // last recorded cycle completed.
        match self.last.as_ref().map(|cycle| &cycle.outcome) {
            None | Some(Ok(_)) => Ok(()),
            Some(Err(error)) => Err(error.clone()),
        }
    }

    fn link(&self) -> LinkState {
        self.tail_link()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const KNOWN_GOOD: &str = r#"{
        "name": "known-good",
        "stations": [
            {"position": 0, "name": "st0", "vendor_id": 173, "product_id": 950,
             "revision": 2, "input_bytes": 4, "output_bytes": 2}
        ],
        "cycles": [
            {"outcome": "complete", "inputs": "01000000"},
            {"outcome": "late", "inputs": "02000000"}
        ]
    }"#;

    #[test]
    fn a_capture_parses_into_its_stations_and_cycles() {
        let capture = Capture::parse(KNOWN_GOOD).unwrap();
        assert_eq!(capture.name, "known-good");
        assert_eq!(capture.stations.len(), 1);
        assert_eq!(capture.stations[0].input_bytes, 4);
        assert_eq!(capture.cycles.len(), 2);
        assert_eq!(capture.cycles[0].outcome, Ok(CycleOutcome::Complete));
        assert_eq!(capture.cycles[1].outcome, Ok(CycleOutcome::Late));
        assert_eq!(capture.cycles[1].inputs, Some(vec![2, 0, 0, 0]));
    }

    #[test]
    fn a_malformed_capture_is_refused_by_name() {
        assert!(matches!(
            Capture::parse("not json"),
            Err(CaptureError::Malformed(_))
        ));
        assert!(matches!(
            Capture::parse(r#"{"stations": [], "cycles": []}"#),
            Err(CaptureError::Malformed(_))
        ));
        assert!(matches!(
            Capture::parse(r#"{"name": "x", "cycles": []}"#),
            Err(CaptureError::Malformed(_))
        ));
        assert!(matches!(
            Capture::parse(r#"{"name": "x", "stations": []}"#),
            Err(CaptureError::Station(_))
        ));
        assert!(matches!(
            Capture::parse(
                r#"{"name": "x", "stations": [{"position": 0, "vendor_id": 1,
                "product_id": 2, "revision": 3, "input_bytes": 1, "output_bytes": 1}],
                "cycles": []}"#
            ),
            Err(CaptureError::Malformed(_))
        ));
        assert!(matches!(
            Capture::parse(r#"{"name": "x", "stations": [{"position": 0}], "cycles": [{}]}"#),
            Err(CaptureError::Station(_))
        ));
        assert!(matches!(
            Capture::parse(r#"{"name": "x", "stations": [], "cycles": [{"outcome": "complete"}]}"#),
            Err(CaptureError::Station(_))
        ));
    }

    #[test]
    fn an_unusable_input_image_is_refused_rather_than_truncated() {
        let station = r#"{"position": 0, "vendor_id": 1, "product_id": 2,
            "revision": 3, "input_bytes": 2, "output_bytes": 1}"#;
        let wrap = |inputs: &str| {
            format!(
                r#"{{"name": "x", "stations": [{station}],
                "cycles": [{{"outcome": "complete", "inputs": "{inputs}"}}]}}"#
            )
        };
        assert!(matches!(
            Capture::parse(&wrap("010")),
            Err(CaptureError::Image(_))
        ));
        assert!(matches!(
            Capture::parse(&wrap("zz")),
            Err(CaptureError::Image(_))
        ));
        assert!(matches!(
            Capture::parse(&wrap("01020304")),
            Err(CaptureError::Image(_))
        ));
        // A shorter image is legal — the rest of the area holds its
        // previous bytes, as a partial answer does.
        assert!(Capture::parse(&wrap("0x01")).is_ok());
    }

    #[test]
    fn an_unrecorded_outcome_is_refused_by_name() {
        let document = r#"{"name": "x", "stations": [{"position": 0, "vendor_id": 1,
            "product_id": 2, "revision": 3, "input_bytes": 1, "output_bytes": 1}],
            "cycles": [{"outcome": "glitched"}]}"#;
        assert!(matches!(
            Capture::parse(document),
            Err(CaptureError::Outcome(_))
        ));
    }

    #[test]
    fn the_replay_repeats_the_recording_s_last_cycle() {
        let capture = Capture::parse(KNOWN_GOOD).unwrap();
        let mut transport = capture.transport();
        let mut inputs = [0u8; 4];
        assert_eq!(
            transport.exchange(&[0, 0], &mut inputs),
            Ok(CycleOutcome::Complete)
        );
        assert_eq!(inputs[0], 1);
        for _ in 0..4 {
            assert_eq!(
                transport.exchange(&[0, 0], &mut inputs),
                Ok(CycleOutcome::Late),
                "past the recording the last recorded answer repeats"
            );
            assert_eq!(inputs[0], 2);
        }
        assert_eq!(transport.served(), 5);
        assert_eq!(transport.link(), LinkState::Connected);
    }

    #[test]
    fn a_recording_that_ends_in_an_outage_does_not_recover_by_itself() {
        let capture = Capture::parse(
            r#"{"name": "drop", "stations": [{"position": 0, "vendor_id": 1,
            "product_id": 2, "revision": 3, "input_bytes": 1, "output_bytes": 1}],
            "cycles": [{"outcome": "complete", "inputs": "01"},
                       {"outcome": "failed", "error": "link down"}]}"#,
        )
        .unwrap();
        let mut transport = capture.transport();
        let mut inputs = [0u8; 1];
        assert_eq!(
            transport.exchange(&[0], &mut inputs),
            Ok(CycleOutcome::Complete)
        );
        for _ in 0..3 {
            assert!(transport.exchange(&[0], &mut inputs).is_err());
        }
        assert_eq!(transport.served(), 4);
        assert_eq!(transport.link(), LinkState::Disconnected);
        assert!(transport.recover().is_err());
        // The held input image is what the failed cycles left behind.
        assert_eq!(inputs, [1]);
    }
}
