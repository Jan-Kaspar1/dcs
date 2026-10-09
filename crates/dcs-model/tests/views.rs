//! Code-engineered drawings remain part of the validated public plant contract.

use dcs_core::PointId;
use dcs_model::{
    PlantModel, PlantView, PlantViewBinding, PlantViewNode, PlantViewPipe, PlantViewPipeEnd,
    PlantViewPort, PlantViewSymbol, SignalIndex, ValidationError,
};

const LEGACY: &str = include_str!("../fixtures/minimal.json");

fn model_with_views() -> PlantModel {
    let mut model = PlantModel::load(LEGACY).unwrap();
    let mut overview = PlantView::new("overview", "Process overview");
    let mut measurement = PlantViewNode::new(
        "temperature",
        PlantViewSymbol::Measurement,
        "Temperature",
        1010,
        660,
    );
    measurement.binding = Some(PlantViewBinding::Point(PointId(10)));
    overview.nodes = vec![
        measurement,
        PlantViewNode::new("vessel", PlantViewSymbol::Tank, "Process vessel", 100, 100),
    ];
    overview.pipes.push(PlantViewPipe {
        from: PlantViewPipeEnd {
            node: "vessel".to_string(),
            port: PlantViewPort::E,
        },
        to: PlantViewPipeEnd {
            node: "temperature".to_string(),
            port: PlantViewPort::W,
        },
    });
    let mut detail = PlantView::new("detail", "Measurement area");
    detail.parent = Some("overview".to_string());
    model.views = vec![overview, detail];
    model
}

#[test]
fn code_views_round_trip_publish_and_diff_without_changing_legacy_bytes() {
    let legacy = PlantModel::load(LEGACY).unwrap();
    assert!(legacy.views.is_empty());
    assert!(
        serde_json::to_value(&legacy)
            .unwrap()
            .get("views")
            .is_none()
    );
    let old_index: SignalIndex = serde_json::from_str(r#"{"points":[],"components":[]}"#).unwrap();
    assert!(old_index.views.is_empty());

    let model = model_with_views();
    let document = serde_json::to_value(&model).unwrap();
    assert_eq!(document["views"][0]["parent"], serde_json::Value::Null);
    assert_eq!(
        document["views"][0]["nodes"][0]["binding"],
        serde_json::json!({ "point": 10 })
    );
    assert!(
        jsonschema::validator_for(&PlantModel::json_schema())
            .unwrap()
            .is_valid(&document)
    );
    let loaded = PlantModel::load(&document.to_string()).unwrap();
    assert_eq!(loaded, model);
    let published: SignalIndex =
        serde_json::from_value(serde_json::to_value(loaded.signal_index()).unwrap()).unwrap();
    assert_eq!(published.views, model.views);
    let mut revised = model.clone();
    revised.views[0].nodes[0].label = "Outlet temperature".to_string();
    let diff = model.diff(&revised);
    assert!(!diff.is_empty());
    assert_eq!(diff.views.len(), 1);
    assert!(diff.devices.is_empty() && diff.io_points.is_empty() && diff.components.is_empty());
    assert_ne!(model.fingerprint(), revised.fingerprint());
}

#[test]
fn drawings_reject_broken_bindings_navigation_geometry_and_pipework() {
    fn rejects(edit: impl FnOnce(&mut PlantModel)) {
        let mut model = model_with_views();
        edit(&mut model);
        assert!(
            model
                .validate()
                .iter()
                .any(|error| matches!(error, ValidationError::InvalidPlantView { .. }))
        );
        assert!(PlantModel::load(&serde_json::to_string(&model).unwrap()).is_err());
    }
    rejects(|model| model.views[0].nodes[0].binding = Some(PlantViewBinding::Point(PointId(999))));
    rejects(|model| {
        model.views[0].nodes[1].binding = Some(PlantViewBinding::Equipment("missing".to_string()))
    });
    rejects(|model| model.views[0].nodes[0].symbol = PlantViewSymbol::Label);
    rejects(|model| model.io_points[0].value_type = dcs_core::ValueKind::Bool);
    rejects(|model| model.views[0].parent = Some("detail".to_string()));
    rejects(|model| model.views[1].parent = Some("missing".to_string()));
    rejects(|model| model.views[0].nodes[0].x = 1011);
    rejects(|model| model.views[0].nodes[0].y = u32::MAX);
    rejects(|model| model.views[0].nodes[1].id = "temperature".to_string());
    rejects(|model| model.views[0].pipes[0].to.node = "missing".to_string());
    rejects(|model| model.views.push(model.views[0].clone()));
}
