//! The composed water/wastewater library plant — issue #423's
//! plant-scale composition proof at the builder seam.
//!
//! Each M9/M10 train is proven by its own acceptance run on its own
//! model. What this target adds is the `WW-FND-001` premise at plant
//! scale: the four trains' kind families, indexed port families,
//! shared-resource coordinators, and managed alarm sets compose into
//! *one* [`PlantModel`] and *one* merged dynamics document, and the
//! merged document still validates, lints, assembles through the
//! standard registry, and emits byte-identically across invocations.
//!
//! The scripted behavioral half — each train's signature behavior
//! running concurrently against one shared simulated plant under one
//! redundant controller pair, with a state-file restart and a promotion
//! across all four at once — lives in
//! `crates/dcs-controller/tests/library_plant.rs`.

use dcs_assembly::{DriverRegistry, FanoutDriver, assemble, resolve_drivers};
use dcs_build::library_plant::{
    AERATION_FRAME, DOSING_FRAME, FILTER_BANK_FRAME, IdFrame, LibraryPlantConfig, STATION_FRAME,
    library_plant, library_plant_dynamics,
};
use dcs_build::{aeration, dosing, filter_bank, station};
use dcs_model::PlantModel;
use dcs_sim::ProcessElement;

/// The checked-in emitted document.
const MODEL_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/library_plant.json"
);
/// The checked-in merged dynamics declaration.
const DYNAMICS_JSON: &str = concat!(
    env!("CARGO_MANIFEST_DIR"),
    "/../dcs-demo/fixtures/library_plant_dynamics.json"
);

/// Composes the reference plant.
fn emit() -> dcs_build::library_plant::LibraryPlant {
    library_plant(&LibraryPlantConfig::reference()).unwrap()
}

/// The checked-in merged document, loaded through `dcs-model`'s
/// validating loader.
fn fixture_model() -> PlantModel {
    PlantModel::load(&std::fs::read_to_string(MODEL_JSON).unwrap()).unwrap()
}

/// The checked-in merged dynamics declaration, parsed as
/// `dcs-plant-server --dynamics` parses it.
fn fixture_dynamics() -> Vec<ProcessElement> {
    serde_json::from_str(&std::fs::read_to_string(DYNAMICS_JSON).unwrap()).unwrap()
}

/// Builds the driver side of the merged document through the standard
/// [`DriverRegistry`], merging the checked-in dynamics into the shared
/// sim map exactly as `dcs-plant-server --dynamics` does — each element
/// lands through `with_element` and revalidates the map.
fn build_driver(model: &PlantModel) -> FanoutDriver {
    let mut plan = resolve_drivers(model, &DriverRegistry::standard()).unwrap();
    for element in fixture_dynamics() {
        plan.sim_map = plan.sim_map.with_element(element);
        plan.sim_map.validate().unwrap();
    }
    plan.build().unwrap()
}

#[test]
fn the_helper_re_emits_the_checked_in_document_exactly() {
    let emitted = emit();
    assert_eq!(emitted.model, fixture_model());
}

#[test]
fn the_merged_document_validates_and_its_lint_is_exactly_the_four_trains_sum() {
    let model = fixture_model();
    assert_eq!(model.version, dcs_model::MODEL_VERSION);
    assert!(model.validate().is_empty(), "{:?}", model.validate());
    // The merged document introduces no finding of its own: its lint
    // is exactly the four trains' advisories — the undeclared
    // `stale_after_ticks` budgets each composition leaves as an opt-in
    // per-point declaration under decision 45. A merge that rewrote an
    // id wrongly, dropped a point, or crossed two channels would show
    // up here as a finding the four do not have.
    let merged: Vec<_> = model
        .lint()
        .iter()
        .map(|finding| (finding.rule, finding.element.clone()))
        .collect();
    let standalone: Vec<Vec<_>> = [
        "pump_station",
        "dosing_skid",
        "filter_bank",
        "aeration_train",
    ]
    .into_iter()
    .map(|name| {
        let path = format!(
            "{}/../dcs-demo/fixtures/{name}.json",
            env!("CARGO_MANIFEST_DIR")
        );
        let train = PlantModel::load(&std::fs::read_to_string(&path).unwrap()).unwrap();
        train
            .lint()
            .iter()
            .map(|finding| (finding.rule, finding.element.clone()))
            .collect()
    })
    .collect();
    let mut expected: Vec<_> = standalone.concat();
    // The merged document's ids are the framed ones, so compare the
    // rules and counts rather than the element strings: each train's
    // advisories survive the merge with their own count.
    expected.sort_by_key(|(rule, element)| (format!("{rule:?}"), element.clone()));
    let by_rule = |findings: &Vec<_>| -> std::collections::BTreeMap<String, usize> {
        let mut counts = std::collections::BTreeMap::new();
        for (rule, _) in findings {
            *counts.entry(format!("{rule:?}")).or_insert(0) += 1;
        }
        counts
    };
    assert_eq!(by_rule(&merged), by_rule(&expected));
    assert!(
        !merged.is_empty(),
        "the merged document keeps the four trains' advisories rather than silently dropping them"
    );
}

#[test]
fn document_assembles_through_the_standard_registry() {
    let model = fixture_model();
    let driver = build_driver(&model);
    assemble(&model, &dcs_controller::registry(), &driver).unwrap();
}

#[test]
fn every_trains_kind_families_survive_the_merge() {
    // Each train's declared kind family is present in the merged
    // document — the composition kept the library's kind families whole
    // rather than dropping one in the merge.
    let model = fixture_model();
    for kind in [
        // The station's families.
        "pump-group",
        "threshold-chain",
        "failover-select",
        "motor",
        // The dosing skid's families.
        "flow-paced-ratio",
        "deviation-monitor",
        "counter",
        "totalizer",
        "analog-output",
        // The filter bank's families.
        "backwash-coordinator",
        "backwash-sequence",
        "phase-monitor",
        "alarm-monitor",
        "digital-output",
        "valve",
        // The aeration train's families.
        "header-coordinator",
        "blower-group",
        "surge-guard",
        "demand-fallback",
        "feedforward-sum",
        "median-voter",
        "pid",
        "signal-filter",
    ] {
        assert!(
            model
                .components
                .iter()
                .any(|instance| instance.kind == kind),
            "the merged document carries no {kind} instance"
        );
    }
    // The four trains' managed alarm sets all survive.
    for kind in ["managed-latching-alarm", "managed-bool-latching-alarm"] {
        assert!(
            model
                .components
                .iter()
                .any(|instance| instance.kind == kind),
            "the merged document carries no {kind} instance"
        );
    }
    // Every kind the merged document names is a registered kind — the
    // standard registry assembled it, and an unknown kind would have
    // failed the assembly above rather than reaching here.
    assert!(
        model.components.len() > 200,
        "the merged plant is plant-scale"
    );
}

#[test]
fn the_frames_keep_the_four_schemes_disjoint() {
    // The merge's whole disjointness guarantee, asserted on the emitted
    // document rather than on the frame arithmetic alone: every point
    // the merged document declares is exactly one of the four trains'
    // framed points — no point invented, none dropped, and each train's
    // scheme surviving the merge intact as an offset.
    let merged = emit().model;
    let mut declared: Vec<u64> = merged.io_points.iter().map(|point| point.id.0).collect();
    declared.sort_unstable();
    let mut expected: Vec<u64> = Vec::new();
    for (frame, train) in standalone_trains() {
        expected.extend(train.io_points.iter().map(|point| frame.point(point.id).0));
    }
    expected.sort_unstable();
    assert_eq!(declared, expected);
}

#[test]
fn the_merge_adds_no_connection_between_trains() {
    // The recorded choice: the merge joins four documents, it does not
    // re-wire them. Each train keeps its own shared-resource wirings —
    // the station's `pump-group` duty arbitration, the dosing skid's
    // permissive chain, the bank's `backwash-coordinator` exclusive
    // grant, the train's `header-coordinator`/`blower-group`
    // coordination — and the merged document adds no connection
    // between trains. That is what makes "no cross-train interference
    // beyond the declared shared-resource wirings" a property of the
    // document rather than only of the run.
    let merged = emit().model;
    for connection in &merged.connections {
        assert_eq!(
            endpoint_frame(&connection.from),
            endpoint_frame(&connection.to),
            "connection {connection:?} crosses two trains' frames"
        );
    }
    // Each train's connection count survives whole.
    for (index, (_, train)) in standalone_trains().into_iter().enumerate() {
        let owned = merged
            .connections
            .iter()
            .filter(|connection| endpoint_frame(&connection.from) == index)
            .count();
        assert_eq!(
            owned,
            train.connections.len(),
            "a train's connection count must survive the merge whole"
        );
    }
}

/// The four trains' own emitted documents beside the frame each
/// occupies — the merge's input, so a test states its expectation
/// against what the individual helpers produced rather than restating
/// their numbers.
fn standalone_trains() -> Vec<(IdFrame, PlantModel)> {
    vec![
        (
            STATION_FRAME,
            station::pumping_station(&station::PumpStationConfig::reference())
                .unwrap()
                .model,
        ),
        (
            DOSING_FRAME,
            dosing::dosing_skid(&dosing::DosingSkidConfig::reference())
                .unwrap()
                .model,
        ),
        (
            FILTER_BANK_FRAME,
            filter_bank::filter_bank(&filter_bank::FilterBankConfig::reference())
                .unwrap()
                .model,
        ),
        (
            AERATION_FRAME,
            aeration::aeration_train(&aeration::AerationTrainConfig::reference())
                .unwrap()
                .model,
        ),
    ]
}

/// The composition-order index of the frame an endpoint of the merged
/// document belongs to: a point id falls in its frame's million-wide
/// point block, a component id in its frame's ten-thousand-wide
/// component block. The two agree because both blocks advance once per
/// train, so one integer names either.
fn endpoint_frame(endpoint: &dcs_model::Endpoint) -> usize {
    match endpoint {
        dcs_model::Endpoint::Point(point) => (point.0 / 1_000_000) as usize,
        dcs_model::Endpoint::Port(port) => (port.component.0 / 10_000) as usize,
    }
}

#[test]
fn identical_builder_invocations_emit_identical_documents() {
    let first = emit().model;
    let second = emit().model;
    assert_eq!(
        serde_json::to_string_pretty(&first).unwrap(),
        serde_json::to_string_pretty(&second).unwrap()
    );
    assert_eq!(first, second);
}

#[test]
fn document_serde_roundtrips() {
    let emitted = emit().model;
    let json = serde_json::to_string_pretty(&emitted).unwrap();
    assert_eq!(PlantModel::load(&json).unwrap(), emitted);
}

#[test]
fn the_merged_dynamics_document_loads_through_the_plant_server_contract() {
    // Each train's dynamics document merges into one list: the element
    // count is the four trains' sum, and every end is a merged-document
    // point in the train's own frame.
    let merged = library_plant_dynamics();
    let standalone: usize = [
        "pump_station",
        "dosing_skid",
        "filter_bank",
        "aeration_train",
    ]
    .into_iter()
    .map(|name| {
        let path = format!(
            "{}/../dcs-demo/fixtures/{name}_dynamics.json",
            env!("CARGO_MANIFEST_DIR")
        );
        serde_json::from_str::<Vec<ProcessElement>>(&std::fs::read_to_string(&path).unwrap())
            .unwrap()
            .len()
    })
    .sum();
    assert_eq!(merged.len(), standalone);
    assert_eq!(merged.len(), fixture_dynamics().len());
    // The driver's own map accepts the merged declaration element by
    // element, each revalidating the map as the server's merge does.
    let model = fixture_model();
    let driver = build_driver(&model);
    assert!(driver.sim().is_some());
}

#[test]
fn the_merged_dynamics_every_end_resolves_to_a_declared_point() {
    // Every element end the merged document names is a point the merged
    // model declares and the simulated field can serve — the check
    // `dcs-plant-server --dynamics`'s channel-map resolution applies.
    let model = fixture_model();
    let mut driven = std::collections::BTreeSet::new();
    for element in library_plant_dynamics() {
        let json = serde_json::to_value(&element).unwrap();
        let object = json.as_object().unwrap();
        let body = object.values().next().unwrap();
        let mut ends: Vec<u64> = Vec::new();
        for key in ["input", "output"] {
            if let Some(point) = body.get(key).and_then(|value| value.as_u64()) {
                ends.push(point);
            }
        }
        for point in body
            .get("inputs")
            .and_then(|value| value.as_array())
            .map(|inputs| {
                inputs
                    .iter()
                    .filter_map(|input| input.as_u64())
                    .collect::<Vec<_>>()
            })
            .unwrap_or_default()
        {
            ends.push(point);
        }
        let output = body
            .get("output")
            .and_then(|value| value.as_u64())
            .expect("every element names an output");
        assert!(driven.insert(output), "two elements drive point {output}");
        for point in ends {
            let declared = model
                .io_points
                .iter()
                .find(|declared| declared.id.0 == point)
                .unwrap_or_else(|| panic!("the merged dynamics names undeclared point {point}"));
            assert!(
                declared.channel.is_some(),
                "the merged dynamics names internal point {point}, which no simulated field serves"
            );
        }
    }
}

#[test]
fn each_trains_layout_addresses_the_merged_document() {
    // The framed layout is how a consumer reaches the merged plant:
    // translating one of a train's own ids lands on a point the merged
    // document actually declares.
    let plant = emit();
    let model = &plant.model;
    let declared =
        |point: dcs_build::PointId| model.io_points.iter().any(|declared| declared.id == point);
    // The station's duty point.
    assert!(declared(
        plant.layout.station.point(plant.layout.station.train.duty)
    ));
    // The dosing skid's flow measurement and dose setpoint.
    assert!(declared(
        plant
            .layout
            .dosing
            .point(plant.layout.dosing.train.flow_source)
    ));
    assert!(declared(
        plant.layout.dosing.point(plant.layout.dosing.train.dose)
    ));
    // The filter bank's per-filter headloss and its coordinator.
    let bank = &plant.layout.bank;
    assert!(declared(bank.point(bank.train.filters[0].headloss)));
    assert!(declared(bank.point(bank.train.filters[2].headloss)));
    assert!(bank.train.filters.iter().all(|filter| {
        declared(bank.point(filter.turbidity)) && declared(bank.point(filter.cbhl_deviation))
    }));
    // The aeration train's header pressure and its coordinator and
    // group.
    let aeration = &plant.layout.aeration;
    assert!(declared(aeration.point(aeration.train.pressure)));
    assert!(declared(aeration.point(aeration.train.pressure_sp)));
    assert_eq!(
        aeration.point(aeration.train.pressure).0,
        AERATION_FRAME.point(aeration.train.pressure).0
    );
    // The coordinator and group ids translate the same way, so a
    // consumer reaching for a shared-resource coordinator finds the
    // right kind in the merged document.
    let bank = &plant.layout.bank;
    let coordinator = plant
        .model
        .components
        .iter()
        .find(|instance| instance.id == bank.component(bank.train.coordinator))
        .expect("the bank's coordinator id resolves in the merged document");
    assert_eq!(coordinator.kind, "backwash-coordinator");
    let group = plant
        .model
        .components
        .iter()
        .find(|instance| instance.id == aeration.component(aeration.train.group))
        .expect("the train's group id resolves in the merged document");
    assert_eq!(group.kind, "blower-group");
}

#[test]
fn the_frames_are_declared_data_not_a_running_total() {
    // The frames are the merge's determinism guarantee: they are
    // readable as data rather than accumulated as a side effect, so two
    // invocations with the same frames emit the same document. The
    // identity frame leaves the first train's ids untouched — a train
    // composed at frame zero is byte-identical to its own standalone
    // document, which is what lets the merged plant and the four
    // individual plants be the same code.
    for (index, frame) in [
        STATION_FRAME,
        DOSING_FRAME,
        FILTER_BANK_FRAME,
        AERATION_FRAME,
    ]
    .into_iter()
    .enumerate()
    {
        assert_eq!(frame.points, index as u64 * 1_000_000);
        assert_eq!(frame.components, index as u64 * 10_000);
        assert_eq!(frame.devices, index as u64 * 100);
    }
    let standalone = standalone_trains()[0].1.io_points.clone();
    let merged: Vec<_> = emit()
        .model
        .io_points
        .iter()
        .filter(|point| STATION_FRAME.point(point.id).0 == point.id.0 && point.id.0 < 1_000_000)
        .cloned()
        .collect();
    assert_eq!(merged, standalone);
}
