//! The cyclic-contract half of the `ethercat` kind: [`BusMaster`] owns
//! one logical bus's staged output image, latched input image, miss
//! accounting, and exchange diagnostics over a
//! [`BusTransport`](crate::transport::BusTransport); [`EthercatDevice`]
//! is the per-device [`IoDriver`] surface one model device receives.
//!
//! The model's `ethercat` device declares *one* station: its
//! `identity` binds to a discovered station on the bus at attach, and
//! its `mapping` places each channel inside that station's process
//! image. Everything here is synchronous and transport-agnostic — the
//! [`CyclicIoDriver`] semantics are proven against
//! [`testing::FakeTransport`](crate::testing::FakeTransport) without
//! hardware, and the same code path drives
//! [`EthercrabTransport`](crate::EthercrabTransport) on a rig.

use crate::params::{DeviceParameters, ImageOffset, StationIdentity};
use crate::transport::{BusTransport, CycleOutcome, DiscoveredStation, TransportError};
use dcs_core::{
    CyclicIoDriver, Direction, DriverDiagnostics, ExchangeDiagnostics, IoDriver, IoError,
    LinkState, PointId, Sample, Tick, Value, ValueKind,
};
use dcs_model::DeviceId;
use std::collections::{BTreeMap, BTreeSet, HashSet};
use std::fmt;
use std::ops::Range;
use std::sync::{Arc, Mutex};

/// One bound I/O point a factory hands [`BusMaster::attach`] — the
/// device's point-facing half of an `io_point`, free of the assembly
/// crate's own type so this crate stays a leaf.
#[derive(Debug, Clone)]
pub struct BusPoint {
    /// The logical point.
    pub point: PointId,
    /// The bound channel's name on the device.
    pub channel: String,
    /// The point's declared direction — the model guarantees it agrees
    /// with the channel's.
    pub direction: Direction,
    /// The point's declared value kind — likewise guaranteed to agree
    /// with the channel's.
    pub kind: ValueKind,
}

/// Why [`EthercatBuses::attach`](crate::EthercatBuses::attach) could
/// not bind a device — the attach-time half of the `DeviceError`
/// vocabulary, mapped onto it by the registered factory so a failure
/// surfaces as the assembly error naming the device.
#[derive(Debug)]
pub enum AttachError {
    /// The declaration is inconsistent against the live bus — e.g. two
    /// devices claiming overlapping output bits, or a bound channel the
    /// mapping does not place.
    Parameters(String),
    /// The bus could not be opened, the device's identity matched no
    /// unclaimed discovered station, a channel's declared offset
    /// exceeds the discovered process-data layout, or OP entry was
    /// refused.
    Backend(String),
}

impl AttachError {
    /// An [`AttachError::Parameters`] with the given detail.
    pub fn parameters(detail: impl Into<String>) -> Self {
        Self::Parameters(detail.into())
    }

    /// An [`AttachError::Backend`] with the given detail.
    pub fn backend(detail: impl Into<String>) -> Self {
        Self::Backend(detail.into())
    }
}

impl fmt::Display for AttachError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Parameters(detail) => write!(f, "invalid device parameters: {detail}"),
            Self::Backend(detail) => write!(f, "device backend unusable: {detail}"),
        }
    }
}

impl std::error::Error for AttachError {}

/// A bound point's place in a bus image: its station's bus position,
/// its bit range within that direction's bus image, and its declared
/// kind.
#[derive(Debug, Clone)]
struct PointMap {
    direction: Direction,
    kind: ValueKind,
    station: usize,
    bits: Range<usize>,
}

/// One logical bus's shared mutable state, behind the master's mutex —
/// every [`EthercatDevice`] attached to the bus reads and stages
/// through it, and the owner device's `exchange` advances it.
struct BusState {
    transport: Box<dyn BusTransport>,
    /// Whether the bus completed its one-shot OP entry. `exchange`
    /// refuses to run before it.
    op_entered: bool,
    /// A failed exchange left the cyclic state path suspect: the next
    /// `exchange` re-enters PRE-OP/SAFE-OP/OP first.
    recover_pending: bool,
    /// The pending output image `write` stages into — the bytes the
    /// next completed exchange publishes.
    staged: Vec<u8>,
    /// The scratch input buffer the transport fills; a completed
    /// exchange's kept ranges are decoded into `latched`.
    received: Vec<u8>,
    /// The held input image: point → sample the last completed exchange
    /// latched, stamped with that exchange's tick.
    latched: BTreeMap<PointId, Sample>,
    /// Every point registered by attached devices.
    points: BTreeMap<PointId, PointMap>,
    /// Output-image bit ranges claimed by attached devices — a second
    /// device may not map an output channel onto claimed bits.
    out_claimed: Vec<Range<usize>>,
    /// Bus positions already bound to an attached device — each model
    /// device owns exactly one discovered station.
    claimed_stations: HashSet<usize>,
    /// Consecutive uncompleted exchanges — compared against each
    /// device's declared `exchange_miss_threshold` on read.
    misses: u64,
    /// Stations a working-counter shortfall last named — their points
    /// escalate to `Disconnected` until a clean exchange.
    short_stations: HashSet<usize>,
    /// An unattributable shortfall degraded the whole bus — every read
    /// escalates until a clean exchange.
    bus_degraded: bool,
    /// The exchange counters diagnostics reports.
    attempted: u64,
    succeeded: u64,
    wkc_mismatches: u64,
    missed_deadlines: u64,
    last_exchange_tick: Option<Tick>,
    last_error: Option<String>,
    /// Attached devices in attach order — the first is the bus's cyclic
    /// and diagnostics owner.
    attached: Vec<DeviceId>,
}

/// One logical EtherCAT bus: the images, the transport, and the failure
/// record every attached `ethercat` device shares.
///
/// Created when the bus's first device attaches — the opener has
/// already run discovery, so [`attach`](Self::attach) binds each
/// device's declared `identity` to a discovered station and
/// [`enter_op`](Self::enter_op) performs the one-shot PRE-OP → SAFE-OP
/// → OP entry. `exchange` then turns the process image exactly once
/// per call; `read`/`write` touch only the images.
pub struct BusMaster {
    /// The logical bus name — diagnostics and error text.
    bus: String,
    state: Mutex<BusState>,
}

impl fmt::Debug for BusMaster {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("BusMaster").field("bus", &self.bus).finish()
    }
}

/// The outcome [`BusMaster::attach`] returns: the registered device
/// view plus whether it is the bus's first attacher — the cyclic and
/// diagnostics owner.
pub(crate) struct Attachment {
    /// The per-device driver surface.
    pub device: Arc<EthercatDevice>,
    /// Whether this attach is the bus's first — the caller then runs
    /// [`BusMaster::enter_op`] once.
    pub first: bool,
}

/// A point kind's neutral value — the image content a channel holds
/// before the field or a write says otherwise.
fn neutral(kind: ValueKind) -> Value {
    match kind {
        ValueKind::Bool => Value::Bool(false),
        ValueKind::Int => Value::Int(0),
        ValueKind::Float => Value::Float(0.0),
    }
}

/// Decodes the point value stored at `bits` in `image`: a single bit
/// for `Bool`, `width` little-endian signed bytes for `Int`, and 4- or
/// 8-byte little-endian floats for `Float`.
fn decode(kind: ValueKind, image: &[u8], bits: &Range<usize>) -> Value {
    match kind {
        ValueKind::Bool => {
            debug_assert_eq!(bits.len(), 1);
            Value::Bool(image[bits.start / 8] & (1 << (bits.start % 8)) != 0)
        }
        ValueKind::Int => {
            debug_assert_eq!(bits.start % 8, 0);
            let width = bits.len() / 8;
            let mut raw = [0u8; 8];
            raw[..width].copy_from_slice(&image[bits.start / 8..bits.end / 8]);
            // Sign-extend from the declared width.
            if image[bits.end / 8 - 1] & 0x80 != 0 {
                raw[width..].fill(0xff);
            }
            Value::Int(i64::from_le_bytes(raw))
        }
        ValueKind::Float => {
            debug_assert_eq!(bits.start % 8, 0);
            let bytes = &image[bits.start / 8..bits.end / 8];
            match bits.len() / 8 {
                4 => Value::Float(f32::from_le_bytes(bytes.try_into().unwrap()) as f64),
                8 => Value::Float(f64::from_le_bytes(bytes.try_into().unwrap())),
                _ => unreachable!("the grammar admits only 32- or 64-bit floats"),
            }
        }
    }
}

/// Encodes `value` — already kind-checked against the point — into
/// `bits` of `image`, the inverse of [`decode`].
fn encode(image: &mut [u8], bits: &Range<usize>, value: Value) {
    match value {
        Value::Bool(set) => {
            debug_assert_eq!(bits.len(), 1);
            let mask = 1 << (bits.start % 8);
            if set {
                image[bits.start / 8] |= mask;
            } else {
                image[bits.start / 8] &= !mask;
            }
        }
        Value::Int(value) => {
            debug_assert_eq!(bits.start % 8, 0);
            let width = bits.len() / 8;
            image[bits.start / 8..bits.end / 8].copy_from_slice(&value.to_le_bytes()[..width]);
        }
        Value::Float(value) => {
            debug_assert_eq!(bits.start % 8, 0);
            let width = bits.len() / 8;
            match width {
                4 => image[bits.start / 8..bits.end / 8]
                    .copy_from_slice(&(value as f32).to_le_bytes()),
                8 => image[bits.start / 8..bits.end / 8].copy_from_slice(&value.to_le_bytes()),
                _ => unreachable!("the grammar admits only 32- or 64-bit floats"),
            }
        }
    }
}

/// The bus position of the station `identity` declares: the first
/// unclaimed discovered station matching vendor, product, and
/// revision. Devices declaring identical identities bind in attach
/// order to matching stations in bus order — the grammar's only
/// discriminator.
fn find_station(
    discovered: &[DiscoveredStation],
    claimed: &HashSet<usize>,
    identity: &StationIdentity,
) -> Option<usize> {
    discovered.iter().position(|station| {
        !claimed.contains(&station.position)
            && station.vendor_id == identity.vendor
            && station.product_id == identity.product
            && station.revision == identity.revision
    })
}

/// A channel's station-relative [`ImageOffset`] projected onto the bus
/// image of its direction: station areas concatenate in position
/// order — EtherCrab's `IIIIOOOO` group PDI, mirrored on the fake side.
fn image_bits(
    offset: &ImageOffset,
    direction: Direction,
    position: usize,
    discovered: &[DiscoveredStation],
) -> Range<usize> {
    let base: u64 = discovered[..position]
        .iter()
        .map(|station| match direction {
            Direction::In => station.input_bytes,
            Direction::Out => station.output_bytes,
        } as u64)
        .sum::<u64>()
        * 8;
    (base + offset.bit_offset) as usize
        ..(base + offset.bit_offset + u64::from(offset.bits)) as usize
}

impl BusMaster {
    /// A master over an opened transport: the staged output image and
    /// the receive buffer sized from the discovered process data.
    pub(crate) fn new(bus: &str, transport: Box<dyn BusTransport>) -> Self {
        Self {
            bus: bus.to_string(),
            state: Mutex::new(BusState {
                staged: vec![0; transport.output_len()],
                received: vec![0; transport.input_len()],
                transport,
                op_entered: false,
                recover_pending: false,
                latched: BTreeMap::new(),
                points: BTreeMap::new(),
                out_claimed: Vec::new(),
                claimed_stations: HashSet::new(),
                misses: 0,
                short_stations: HashSet::new(),
                bus_degraded: false,
                attempted: 0,
                succeeded: 0,
                wkc_mismatches: 0,
                missed_deadlines: 0,
                last_exchange_tick: None,
                last_error: None,
                attached: Vec::new(),
            }),
        }
    }

    /// Attaches one model device to the bus: binds its declared
    /// `identity` to an unclaimed discovered station, verifies every
    /// bound channel's declared offset fits the discovered process-data
    /// layout, registers its points' image locations, seeds the staged
    /// output image with the declared safe outputs, and latches
    /// neutral `Tick::ZERO` input samples — the pre-first-exchange
    /// held image.
    ///
    /// The first attacher owns the bus's cyclic surface and
    /// diagnostics; `first` tells the caller to run
    /// [`enter_op`](Self::enter_op) before the bus serves scans. Every
    /// check runs before any mutation: a mismatch leaves the bus
    /// exactly as it was, a hard startup failure before OP per the
    /// `fail` startup policy.
    pub(crate) fn attach(
        self: &Arc<Self>,
        device: DeviceId,
        params: &DeviceParameters,
        points: &[BusPoint],
    ) -> Result<Attachment, AttachError> {
        let mut state = self.state.lock().unwrap();
        let position = find_station(
            state.transport.discovered(),
            &state.claimed_stations,
            &params.identity,
        )
        .ok_or_else(|| {
            AttachError::backend(format!(
                "bus {:?} discovered {} station(s), none unclaimed matching the declared identity \
                 (vendor {:#010x}, product {:#010x}, revision {})",
                self.bus,
                state.transport.discovered().len(),
                params.identity.vendor,
                params.identity.product,
                params.identity.revision
            ))
        })?;
        let station = &state.transport.discovered()[position];
        let mut mapped = Vec::with_capacity(points.len());
        let mut claiming = state.out_claimed.clone();
        for point in points {
            let image = match point.direction {
                Direction::In => &params.inputs,
                Direction::Out => &params.outputs,
            };
            let Some(offset) = image.get(&point.channel) else {
                return Err(AttachError::parameters(format!(
                    "io point {} binds channel {:?} the mapping does not place",
                    point.point.0, point.channel
                )));
            };
            let (area, area_name) = match point.direction {
                Direction::In => (station.input_bytes, "input"),
                Direction::Out => (station.output_bytes, "output"),
            };
            let end = offset.bit_offset + u64::from(offset.bits);
            if end > area as u64 * 8 {
                return Err(AttachError::backend(format!(
                    "bus {:?} station {position}: channel {:?} maps bits {}..{end} beyond the \
                     discovered {area_name} area of {area} bytes",
                    self.bus, point.channel, offset.bit_offset
                )));
            }
            let bits = image_bits(
                offset,
                point.direction,
                position,
                state.transport.discovered(),
            );
            if point.direction == Direction::Out {
                if let Some(other) = claiming
                    .iter()
                    .find(|range| range.start < bits.end && bits.start < range.end)
                {
                    return Err(AttachError::parameters(format!(
                        "channel {:?} output bits {}..{} overlap bits {}..{} another device claimed",
                        point.channel, bits.start, bits.end, other.start, other.end
                    )));
                }
                claiming.push(bits.clone());
            }
            mapped.push((
                point.point,
                point.channel.clone(),
                PointMap {
                    direction: point.direction,
                    kind: point.kind,
                    station: position,
                    bits,
                },
            ));
        }
        for (point, channel, map) in mapped {
            match map.direction {
                Direction::In => {
                    state
                        .latched
                        .insert(point, Sample::good(neutral(map.kind), Tick::ZERO));
                }
                Direction::Out => {
                    let seed = params
                        .safe_outputs
                        .get(&channel)
                        .copied()
                        .unwrap_or_else(|| neutral(map.kind));
                    encode(&mut state.staged, &map.bits, seed);
                }
            }
            state.points.insert(point, map);
        }
        state.out_claimed = claiming;
        state.claimed_stations.insert(position);
        let first = state.attached.is_empty();
        state.attached.push(device);
        Ok(Attachment {
            device: Arc::new(EthercatDevice {
                device,
                bus: self.bus.clone(),
                master: Arc::clone(self),
                points: points.iter().map(|point| point.point).collect(),
                miss_threshold: params.exchange_miss_threshold,
                owner: first,
            }),
            first,
        })
    }

    /// The one-shot OP entry after the first device verified:
    /// PRE-OP → SAFE-OP → OP through the transport.
    pub(crate) fn enter_op(&self) -> Result<(), TransportError> {
        let mut state = self.state.lock().unwrap();
        if state.op_entered {
            return Ok(());
        }
        state.transport.enter_op()?;
        state.op_entered = true;
        Ok(())
    }

    /// One process-image exchange for scan `tick`: the staged output
    /// image publishes, the returned input image latches the kept
    /// ranges at `tick`. Recovery re-entry runs first when a previous
    /// exchange failed — at this boundary, never mid-scan.
    fn exchange_image(&self, tick: Tick) -> Result<(), IoError> {
        let mut state = self.state.lock().unwrap();
        // A failed exchange's `IoError` names a covered point for the
        // boundary's I/O-health attribution.
        let attribution = state.points.keys().min().copied().unwrap_or(PointId(0));
        state.attempted += 1;
        if state.recover_pending {
            match state.transport.recover() {
                Ok(()) => state.recover_pending = false,
                Err(error) => {
                    state.misses += 1;
                    state.last_error = Some(format!("recovery re-entry failed: {error}"));
                    return Err(IoError::Disconnected(attribution));
                }
            }
        }
        let outcome = {
            // Disjoint field borrows — the guard hides them.
            let state = &mut *state;
            state.transport.exchange(&state.staged, &mut state.received)
        };
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(error) => {
                // Nothing published or latched: the held image serves,
                // the staged image is retained, and the cyclic state
                // path re-enters at the next boundary.
                state.misses += 1;
                state.recover_pending = true;
                state.last_error = Some(error.to_string());
                return Err(IoError::Disconnected(attribution));
            }
        };
        state.misses = 0;
        state.succeeded += 1;
        state.last_exchange_tick = Some(tick);
        let keep = |station: usize| match outcome {
            CycleOutcome::Complete | CycleOutcome::Late => true,
            CycleOutcome::Short {
                station: Some(short),
            } => station != short,
            CycleOutcome::Short { station: None } => false,
        };
        // Decode the kept input ranges into the held image at the
        // acquisition stamp — atomically with respect to reads.
        let latched: Vec<(PointId, Sample)> = state
            .points
            .iter()
            .filter(|(_, map)| map.direction == Direction::In && keep(map.station))
            .map(|(&point, map)| {
                (
                    point,
                    Sample::good(decode(map.kind, &state.received, &map.bits), tick),
                )
            })
            .collect();
        for (point, sample) in latched {
            state.latched.insert(point, sample);
        }
        match outcome {
            CycleOutcome::Complete => {
                state.short_stations.clear();
                state.bus_degraded = false;
            }
            CycleOutcome::Late => {
                state.short_stations.clear();
                state.bus_degraded = false;
                state.missed_deadlines += 1;
            }
            CycleOutcome::Short { station } => {
                state.wkc_mismatches += 1;
                match station {
                    Some(station) => {
                        state.short_stations = [station].into_iter().collect();
                        state.bus_degraded = false;
                        state.last_error = Some(format!(
                            "working counter shortfall attributed to station {station}"
                        ));
                    }
                    None => {
                        state.short_stations.clear();
                        state.bus_degraded = true;
                        state.last_error =
                            Some("unattributable working counter shortfall".to_string());
                    }
                }
            }
        }
        Ok(())
    }

    /// A point read against one device's declared miss threshold:
    /// escalation to `Disconnected` at or past it, or while the point's
    /// station or the whole bus is degraded; otherwise the latched
    /// input image or the staged output image — never the transport.
    fn read_image(&self, point: PointId, miss_threshold: u64) -> Result<Sample, IoError> {
        let state = self.state.lock().unwrap();
        let map = state
            .points
            .get(&point)
            .ok_or(IoError::UnknownPoint(point))?;
        if state.misses >= miss_threshold
            || state.bus_degraded
            || state.short_stations.contains(&map.station)
        {
            return Err(IoError::Disconnected(point));
        }
        match map.direction {
            Direction::In => state
                .latched
                .get(&point)
                .copied()
                .ok_or(IoError::UnknownPoint(point)),
            // An output read serves the pending staged image — what the
            // next completed exchange will publish — stamped with the
            // last completed exchange's tick.
            Direction::Out => Ok(Sample::good(
                decode(map.kind, &state.staged, &map.bits),
                state.last_exchange_tick.unwrap_or(Tick::ZERO),
            )),
        }
    }

    /// Stages a point write into the pending output image — local
    /// image mutation only; the next completed exchange publishes it.
    /// `In` points have no writable surface: the field owns the input
    /// image.
    fn write_image(&self, point: PointId, value: Value) -> Result<(), IoError> {
        let mut state = self.state.lock().unwrap();
        let (direction, kind, bits) = {
            let map = state
                .points
                .get(&point)
                .ok_or(IoError::UnknownPoint(point))?;
            (map.direction, map.kind, map.bits.clone())
        };
        if direction != Direction::Out {
            return Err(IoError::UnknownPoint(point));
        }
        if value.kind() != kind {
            return Err(IoError::TypeMismatch {
                point,
                expected: kind,
                found: value,
            });
        }
        encode(&mut state.staged, &bits, value);
        Ok(())
    }

    /// The bus-level diagnostics: link state from the miss record and
    /// the transport's own read, the last failure's text, and the
    /// exchange counters — the cyclic half of the decision-22 surface.
    fn diagnostics(&self) -> DriverDiagnostics {
        let state = self.state.lock().unwrap();
        DriverDiagnostics {
            link: if state.misses > 0 || state.transport.link() == LinkState::Disconnected {
                LinkState::Disconnected
            } else {
                LinkState::Connected
            },
            last_error: state.last_error.clone(),
            exchange: Some(ExchangeDiagnostics {
                attempted: state.attempted,
                succeeded: state.succeeded,
                working_counter_mismatches: state.wkc_mismatches,
                last_exchange_tick: state.last_exchange_tick,
                missed_deadlines: state.missed_deadlines,
            }),
        }
    }
}

/// One `ethercat` model device's [`IoDriver`] surface: its own points,
/// its declared `exchange_miss_threshold`, and — for the bus's first
/// attacher — the shared master's cyclic surface and diagnostics.
pub struct EthercatDevice {
    device: DeviceId,
    bus: String,
    master: Arc<BusMaster>,
    /// The points this device serves — the fan-out routes only these.
    points: BTreeSet<PointId>,
    miss_threshold: u64,
    /// Whether this device is the bus's first attacher: the one device
    /// carrying the cyclic surface and the diagnostics, so a shared
    /// bus exchanges once per scan and counts once.
    owner: bool,
}

impl fmt::Debug for EthercatDevice {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("EthercatDevice")
            .field("device", &self.device)
            .field("bus", &self.bus)
            .field("owner", &self.owner)
            .finish()
    }
}

impl EthercatDevice {
    /// Whether this device is the bus's owner — exposed for the kind's
    /// own tests and the QA lane's inspection.
    pub fn is_owner(&self) -> bool {
        self.owner
    }

    /// The shared master — the `inspect` handle's concrete type.
    pub fn master(&self) -> &Arc<BusMaster> {
        &self.master
    }
}

impl IoDriver for EthercatDevice {
    fn read(&self, point: PointId) -> Result<Sample, IoError> {
        if !self.points.contains(&point) {
            return Err(IoError::UnknownPoint(point));
        }
        self.master.read_image(point, self.miss_threshold)
    }

    fn write(&self, point: PointId, value: Value) -> Result<(), IoError> {
        if !self.points.contains(&point) {
            return Err(IoError::UnknownPoint(point));
        }
        self.master.write_image(point, value)
    }

    /// `capture_state` stays `None` — a field-observing kind: the field
    /// itself is the state, per the checkpoint decision.
    fn diagnostics(&self) -> Option<DriverDiagnostics> {
        // Only the bus owner reports — the fan-out sums exchange
        // counters over reporting backends, so a shared bus must count
        // once, not once per attached device.
        self.owner.then(|| self.master.diagnostics())
    }

    /// The cyclic surface exists only on the bus owner, so one shared
    /// bus exchanges exactly once per scan under the fan-out.
    fn cyclic(&self) -> Option<&(dyn CyclicIoDriver + Sync)> {
        self.owner.then_some(self)
    }
}

impl CyclicIoDriver for EthercatDevice {
    /// One exchange on the shared master — the whole bus's staged
    /// image publishes and its input image latches at `tick`.
    fn exchange(&self, tick: Tick) -> Result<(), IoError> {
        self.master.exchange_image(tick)
    }
}
