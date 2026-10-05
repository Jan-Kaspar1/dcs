//! The water/wastewater library plant — issue #423's composed
//! multi-train document at the builder seam.
//!
//! Each M9/M10 train is proven by its own acceptance run on its own
//! model: the duty/standby station ([`crate::station`]), the dosing
//! skid ([`crate::dosing`]), the filter bank ([`crate::filter_bank`]),
//! and the aeration train ([`crate::aeration`]). What none of them
//! proves is `WW-FND-001` at plant scale: that the library's kind
//! families, the indexed port families, the shared-resource
//! coordinators, and the managed alarm set compose into one
//! [`PlantModel`] on one simulated plant under one redundant
//! controller pair, without contract drift between them.
//!
//! [`library_plant`] composes the four trains through their typed
//! helpers and merges the four emitted documents into one — the checked
//! -in copy lives at `crates/dcs-demo/fixtures/library_plant.json`
//! beside its merged dynamics document `library_plant_dynamics.json`,
//! the fixture convention every reference composition shares.
//!
//! ## How the merge keeps the ids disjoint
//!
//! Each train helper owns a point-id scheme rooted at its own small
//! field blocks (`10`, `20 + i`, …), so two trains' documents collide
//! if they are concatenated as they stand. The merge therefore gives
//! every train a declared id *frame* — a point base, a signal base, a
//! component base, and a device base — and rewrites the train's points,
//! signals, components, connections, and channel references into its
//! own frame. The frames are declared data, far wider than any train's
//! scheme reaches, so a train's declared scheme survives the merge
//! intact as an offset rather than being renumbered into something else
//! — and identical invocations emit identical bytes, because the merged
//! document is a function of the declared frames alone.
//!
//! ## What the merge does *not* do
//!
//! It joins four documents; it does not re-wire them. Each train keeps
//! its own shared-resource wirings — the station's `pump-group` duty
//! arbitration, the dosing skid's permissive chain, the bank's
//! `backwash-coordinator` exclusive grant, the train's
//! `header-coordinator`/`blower-group` coordination — and the merged
//! document adds no connection between trains. That is the recorded
//! choice the acceptance criterion's "no cross-train interference
//! beyond the declared shared-resource wirings" asks for: the trains
//! coexist on one plant and one document, each arbitrating its own
//! resources, and the merged run proves each keeps doing so.
//!
//! A plant that *did* couple its trains would add the coupling
//! connections here explicitly — through the same typed handles the
//! individual trains compose through — rather than have the merge infer
//! it.
//!
//! ## The merged dynamics document
//!
//! [`library_plant_dynamics`] merges each train's checked-in dynamics
//! declaration into one list, rewriting every element end into its
//! train's frame. The elements stay the `ProcessElement` vocabulary
//! each train's document already uses — the merge adds no kind and
//! expresses no physics of its own — so
//! `dcs-plant-server --dynamics` merges the result exactly as it merges
//! each train's own document.

use crate::BuildError;
use crate::aeration::{AerationTrainConfig, AerationTrainLayout, aeration_train};
use crate::dosing::{DosingSkidConfig, DosingSkidLayout, dosing_skid};
use crate::dynamics::DynamicsElement;
use crate::filter_bank::{FilterBankConfig, FilterBankLayout, filter_bank};
use crate::station::{PumpStationConfig, PumpStationLayout, pumping_station};
use dcs_core::{PointId, SignalId};
use dcs_model::{ComponentId, Connection, DeviceId, Endpoint, MODEL_VERSION, PlantModel, PortRef};

/// The declared id frame one train occupies inside the merged plant.
///
/// Each frame is a set of bases added to the train's own ids: a point
/// base, a signal base, a component base, and a device base. The
/// [`PLANT_FRAMES`] declaration fixes them, and each frame is far wider
/// than its train's scheme reaches — a `u64` engineering id is a plant's
/// own numbering, and the frames keep four independent schemes legible
/// side by side rather than renumbering them into one anonymous range.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct IdFrame {
    /// Added to every `PointId` the train declares.
    pub points: u64,
    /// Added to every `SignalId` the train declares.
    pub signals: u64,
    /// Added to every `ComponentId` the train declares.
    pub components: u64,
    /// Added to every `DeviceId` the train declares.
    pub devices: u64,
}

impl IdFrame {
    /// The `point`'s id inside the frame.
    pub fn point(self, point: PointId) -> PointId {
        PointId(point.0 + self.points)
    }

    /// The `signal`'s id inside the frame.
    pub fn signal(self, signal: SignalId) -> SignalId {
        SignalId(signal.0 + self.signals)
    }

    /// The `component`'s id inside the frame.
    pub fn component(self, component: ComponentId) -> ComponentId {
        ComponentId(component.0 + self.components)
    }

    /// The `device`'s id inside the frame.
    pub fn device(self, device: DeviceId) -> DeviceId {
        DeviceId(device.0 + self.devices)
    }
}

/// The width every train's point frame reserves: wider than any
/// reference train's scheme reaches — the filter bank's `10227` is the
/// largest — so a frame can never collide with its neighbour whatever a
/// train declares.
const POINT_FRAME: u64 = 1_000_000;
/// The width every train's signal frame reserves: the reference trains
/// place a signal at `SIGNAL_BASE + point`, so a signal frame must clear
/// both its train's own signal base and the point frame beside it.
const SIGNAL_FRAME: u64 = 100_000_000;
/// The width every train's component frame reserves: wider than the
/// filter bank's 140 instances.
const COMPONENT_FRAME: u64 = 10_000;
/// The width every train's device frame reserves: wider than any
/// reference train's four simulated devices.
const DEVICE_FRAME: u64 = 100;

/// The frames the four trains occupy, in composition order — the
/// station, the dosing skid, the filter bank, the aeration train.
pub const PLANT_FRAMES: [IdFrame; 4] = [
    IdFrame {
        points: 0,
        signals: 0,
        components: 0,
        devices: 0,
    },
    IdFrame {
        points: POINT_FRAME,
        signals: SIGNAL_FRAME,
        components: COMPONENT_FRAME,
        devices: DEVICE_FRAME,
    },
    IdFrame {
        points: 2 * POINT_FRAME,
        signals: 2 * SIGNAL_FRAME,
        components: 2 * COMPONENT_FRAME,
        devices: 2 * DEVICE_FRAME,
    },
    IdFrame {
        points: 3 * POINT_FRAME,
        signals: 3 * SIGNAL_FRAME,
        components: 3 * COMPONENT_FRAME,
        devices: 3 * DEVICE_FRAME,
    },
];

/// The station train's id frame inside the merged plant.
pub const STATION_FRAME: IdFrame = PLANT_FRAMES[0];
/// The dosing skid's id frame inside the merged plant.
pub const DOSING_FRAME: IdFrame = PLANT_FRAMES[1];
/// The filter bank's id frame inside the merged plant.
pub const FILTER_BANK_FRAME: IdFrame = PLANT_FRAMES[2];
/// The aeration train's id frame inside the merged plant.
pub const AERATION_FRAME: IdFrame = PLANT_FRAMES[3];

/// The composition of the four trains under declared per-train
/// configs — the `config` an engineer edits to size the plant.
pub struct LibraryPlantConfig {
    /// The duty/standby pumping station's declared config.
    pub station: PumpStationConfig,
    /// The chemical dosing skid's declared config.
    pub dosing: DosingSkidConfig,
    /// The granular-media filter bank's declared config.
    pub filter_bank: FilterBankConfig,
    /// The diffused-air aeration train's declared config.
    pub aeration: AerationTrainConfig,
}

impl LibraryPlantConfig {
    /// The reference plant: each train's own `reference()` config, so
    /// the merged document's trains are the ones their own checked-in
    /// documents declare.
    pub fn reference() -> Self {
        Self {
            station: PumpStationConfig::reference(),
            dosing: DosingSkidConfig::reference(),
            filter_bank: FilterBankConfig::reference(),
            aeration: AerationTrainConfig::reference(),
        }
    }
}

/// A train's own layout beside the [`IdFrame`] it occupies in the
/// merged plant.
///
/// The train's layout stays the one its own helper returned — the
/// declared scheme an engineer reads — and `point`/`component` translate
/// one of its ids into the merged document's. So a consumer addresses
/// the merged plant as `plant.layout.bank.point(bank.filters[0].headloss)`
/// and never hand-composes a base.
#[derive(Debug, Clone)]
pub struct Framed<T> {
    /// The train's own layout, in the train's own id space.
    pub train: T,
    /// The frame the train occupies in the merged document.
    pub frame: IdFrame,
}

impl<T> Framed<T> {
    /// The `point` the merged document declares for the train's own
    /// `point`.
    pub fn point(&self, point: PointId) -> PointId {
        self.frame.point(point)
    }

    /// The `component` the merged document declares for the train's own
    /// `component`.
    pub fn component(&self, component: ComponentId) -> ComponentId {
        self.frame.component(component)
    }
}

/// The id maps into the merged document, one per train, each carried
/// beside the frame its train occupies.
pub struct LibraryPlantLayout {
    /// The station, in [`STATION_FRAME`].
    pub station: Framed<PumpStationLayout>,
    /// The dosing skid, in [`DOSING_FRAME`].
    pub dosing: Framed<DosingSkidLayout>,
    /// The filter bank, in [`FILTER_BANK_FRAME`].
    pub bank: Framed<FilterBankLayout>,
    /// The aeration train, in [`AERATION_FRAME`].
    pub aeration: Framed<AerationTrainLayout>,
}

/// The composed plant: one merged [`PlantModel`] plus the id map into
/// it.
pub struct LibraryPlant {
    /// The merged versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it, one framed train layout per train.
    pub layout: LibraryPlantLayout,
}

/// Composes the four trains under `config` and merges them into one
/// [`PlantModel`].
///
/// Each train is composed through its own typed helper, so every
/// component in the merged document was registered through a typed spec
/// and every connection between two of that train's ends went through
/// typed handles; the merge then rewrites the four emitted documents
/// into disjoint [`IdFrame`]s and concatenates them. The merged
/// document carries the same [`validate`](PlantModel::validate) the
/// individual documents pass and the same
/// [`assemble`](dcs_assembly) shape the standard registry serves.
///
/// The merged dynamics declaration the composed plant's simulated field
/// runs on is [`library_plant_dynamics`].
///
/// # Panics
///
/// Any train's own config outside its declared point-id scheme — the
/// individual helpers' documented panics, reached through this one.
pub fn library_plant(config: &LibraryPlantConfig) -> Result<LibraryPlant, BuildError> {
    let station = pumping_station(&config.station)?;
    let dosing = dosing_skid(&config.dosing)?;
    let bank = filter_bank(&config.filter_bank)?;
    let aeration = aeration_train(&config.aeration)?;

    let mut model = PlantModel {
        version: MODEL_VERSION,
        devices: Vec::new(),
        io_points: Vec::new(),
        signals: Vec::new(),
        components: Vec::new(),
        connections: Vec::new(),
    };
    for (frame, train) in [
        (STATION_FRAME, &station.model),
        (DOSING_FRAME, &dosing.model),
        (FILTER_BANK_FRAME, &bank.model),
        (AERATION_FRAME, &aeration.model),
    ] {
        merge(&mut model, frame, train);
    }
    let errors = model.validate();
    if !errors.is_empty() {
        return Err(BuildError::Invalid(errors));
    }

    Ok(LibraryPlant {
        model,
        layout: LibraryPlantLayout {
            station: Framed {
                train: station.layout,
                frame: STATION_FRAME,
            },
            dosing: Framed {
                train: dosing.layout,
                frame: DOSING_FRAME,
            },
            bank: Framed {
                train: bank.layout,
                frame: FILTER_BANK_FRAME,
            },
            aeration: Framed {
                train: aeration.layout,
                frame: AERATION_FRAME,
            },
        },
    })
}

/// Appends `train` into `plant` with every id it declares moved into
/// `frame`.
///
/// The rewrite is total: device, point, signal, and component ids, the
/// channel references on the points, and both ends of every connection
/// — a point end and a port end alike. Nothing in `train` survives
/// unrenamed, so the merged document cannot carry a dangling reference
/// into another train's id space.
fn merge(plant: &mut PlantModel, frame: IdFrame, train: &PlantModel) {
    for device in &train.devices {
        let mut device = device.clone();
        device.id = frame.device(device.id);
        plant.devices.push(device);
    }
    for point in &train.io_points {
        let mut point = point.clone();
        point.id = frame.point(point.id);
        if let Some(channel) = &mut point.channel {
            channel.device = frame.device(channel.device);
        }
        plant.io_points.push(point);
    }
    for signal in &train.signals {
        let mut signal = signal.clone();
        signal.id = frame.signal(signal.id);
        signal.source = frame.point(signal.source);
        plant.signals.push(signal);
    }
    for component in &train.components {
        let mut component = component.clone();
        component.id = frame.component(component.id);
        plant.components.push(component);
    }
    for connection in &train.connections {
        plant.connections.push(Connection {
            from: shift_endpoint(frame, &connection.from),
            to: shift_endpoint(frame, &connection.to),
        });
    }
}

/// The endpoint `frame` renames.
fn shift_endpoint(frame: IdFrame, endpoint: &Endpoint) -> Endpoint {
    match endpoint {
        Endpoint::Point(point) => Endpoint::Point(frame.point(*point)),
        Endpoint::Port(PortRef { component, name }) => Endpoint::Port(PortRef {
            component: frame.component(*component),
            name: name.clone(),
        }),
    }
}

/// The merged dynamics declaration for the composed plant: each train's
/// checked-in `*_dynamics.json` list, parsed as `dcs-plant-server
/// --dynamics` parses it and rewritten into that train's frame, in
/// composition order.
///
/// The merge adds no element kind and expresses no physics of its own —
/// every element is one a train's own document already declares, so
/// the merged document's process behavior is the four documents'
/// behavior composed, never a fifth thing.
///
/// # Panics
///
/// A checked-in dynamics document that does not parse, which is a
/// checked-in-artifact defect rather than a caller's error.
pub fn library_plant_dynamics() -> Vec<DynamicsElement> {
    [
        (STATION_FRAME, "pump_station_dynamics.json"),
        (DOSING_FRAME, "dosing_skid_dynamics.json"),
        (FILTER_BANK_FRAME, "filter_bank_dynamics.json"),
        (AERATION_FRAME, "aeration_train_dynamics.json"),
    ]
    .into_iter()
    .flat_map(|(frame, name)| {
        let path = format!("{}/../dcs-demo/fixtures/{name}", env!("CARGO_MANIFEST_DIR"));
        let elements: Vec<DynamicsElement> =
            serde_json::from_str(&std::fs::read_to_string(&path).unwrap())
                .unwrap_or_else(|error| panic!("{path} does not parse: {error}"));
        elements
            .into_iter()
            .map(move |element| shift_element(frame, element))
    })
    .collect()
}

/// The dynamics element with every point end moved into `frame`.
fn shift_element(frame: IdFrame, element: DynamicsElement) -> DynamicsElement {
    use DynamicsElement::*;
    match element {
        FirstOrderLag(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            FirstOrderLag(e)
        }
        SecondOrderLag(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            SecondOrderLag(e)
        }
        Integrator(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            Integrator(e)
        }
        DeadTime(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            DeadTime(e)
        }
        Noise(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            Noise(e)
        }
        BoolFlow(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            BoolFlow(e)
        }
        FlowSum(mut e) => {
            e.inputs = e
                .inputs
                .into_iter()
                .map(|point| frame.point(point))
                .collect();
            e.output = frame.point(e.output);
            FlowSum(e)
        }
        ScaledFlow(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            ScaledFlow(e)
        }
        Threshold(mut e) => {
            e.input = frame.point(e.input);
            e.output = frame.point(e.output);
            Threshold(e)
        }
    }
}
