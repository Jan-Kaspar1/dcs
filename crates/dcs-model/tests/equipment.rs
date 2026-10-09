//! Public equipment ownership, command surface, and revision contracts.

use dcs_core::PointId;
use dcs_model::{
    ChangeKind, ComponentId, Equipment, EquipmentControl, PlantModel, SignalIndex, ValidationError,
};

const LEGACY: &str = include_str!("../fixtures/minimal.json");

fn equipment_model() -> PlantModel {
    let mut model = PlantModel::load(LEGACY).unwrap();
    model.io_points[0].writable = true;
    model.equipment.push(Equipment {
        id: "feed".to_string(),
        label: "Feed actuator".to_string(),
        kind: "actuator".to_string(),
        components: vec![ComponentId(1)],
        points: vec![PointId(11), PointId(10)],
        controls: vec![EquipmentControl {
            point: PointId(10),
            label: "Demand".to_string(),
            false_label: None,
            true_label: None,
        }],
    });
    model
}

#[test]
fn equipment_loads_and_publishes_without_breaking_legacy_documents() {
    let legacy = PlantModel::load(LEGACY).unwrap();
    assert!(legacy.equipment.is_empty());
    assert!(
        serde_json::to_value(&legacy)
            .unwrap()
            .get("equipment")
            .is_none()
    );
    let legacy_index: SignalIndex =
        serde_json::from_str(r#"{"points":[],"components":[{"name":"gain:1","kind":"gain"}]}"#)
            .unwrap();
    assert!(legacy_index.equipment.is_empty());
    assert_eq!(legacy_index.components[0].id, None);

    let document = serde_json::to_value(equipment_model()).unwrap();
    let schema = PlantModel::json_schema();
    assert!(
        jsonschema::validator_for(&schema)
            .unwrap()
            .is_valid(&document)
    );
    let loaded = PlantModel::load(&document.to_string()).unwrap();
    let published: SignalIndex =
        serde_json::from_value(serde_json::to_value(loaded.signal_index()).unwrap()).unwrap();
    let equipment = &published.equipment[0];
    assert_eq!(equipment.id, "feed");
    assert_eq!(equipment.label, "Feed actuator");
    assert_eq!(equipment.kind, "actuator");
    assert_eq!(equipment.points, [PointId(11), PointId(10)]);
    assert_eq!(equipment.controls[0].point, PointId(10));
    assert_eq!(equipment.controls[0].label, "Demand");
    assert!(published.get(equipment.controls[0].point).unwrap().writable);
    let member = published
        .components
        .iter()
        .find(|record| record.id == Some(equipment.components[0]))
        .unwrap();
    assert_eq!(member.name, "gain:1");
}

#[test]
fn equipment_rejects_invalid_ownership_references_and_controls() {
    fn rejects(edit: impl FnOnce(&mut PlantModel), expected: ValidationError) {
        let mut model = equipment_model();
        edit(&mut model);
        let errors = model.validate();
        assert!(
            errors.contains(&expected),
            "expected {expected:?}, got {errors:?}"
        );
        assert!(PlantModel::load(&serde_json::to_string(&model).unwrap()).is_err());
    }
    for field in ["id", "label", "kind", "components"] {
        rejects(
            |model| match field {
                "id" => model.equipment[0].id = " ".to_string(),
                "label" => model.equipment[0].label.clear(),
                "kind" => model.equipment[0].kind.clear(),
                "components" => model.equipment[0].components.clear(),
                _ => unreachable!(),
            },
            ValidationError::EmptyEquipmentField {
                equipment: if field == "id" { " " } else { "feed" }.to_string(),
                field: field.to_string(),
            },
        );
    }
    rejects(
        |model| model.equipment.push(model.equipment[0].clone()),
        ValidationError::DuplicateEquipmentId {
            equipment: "feed".to_string(),
        },
    );
    rejects(
        |model| {
            let mut second = model.equipment[0].clone();
            second.id = "other".to_string();
            model.equipment.push(second);
        },
        ValidationError::EquipmentComponentOwned {
            equipment: "other".to_string(),
            owner: "feed".to_string(),
            component: ComponentId(1),
        },
    );
    rejects(
        |model| model.equipment[0].components.push(ComponentId(1)),
        ValidationError::DuplicateEquipmentComponent {
            equipment: "feed".to_string(),
            component: ComponentId(1),
        },
    );
    rejects(
        |model| model.equipment[0].components = vec![ComponentId(99)],
        ValidationError::UnknownEquipmentComponent {
            equipment: "feed".to_string(),
            component: ComponentId(99),
        },
    );
    rejects(
        |model| model.equipment[0].points.push(PointId(99)),
        ValidationError::UnknownEquipmentPoint {
            equipment: "feed".to_string(),
            point: PointId(99),
        },
    );
    for field in ["points", "controls"] {
        rejects(
            |model| {
                if field == "points" {
                    model.equipment[0].points.push(PointId(10));
                } else {
                    let control = model.equipment[0].controls[0].clone();
                    model.equipment[0].controls.push(control);
                }
            },
            ValidationError::DuplicateEquipmentPoint {
                equipment: "feed".to_string(),
                point: PointId(10),
                field: field.to_string(),
            },
        );
    }
    rejects(
        |model| {
            model.equipment[0]
                .points
                .retain(|point| *point != PointId(10))
        },
        ValidationError::EquipmentControlNotListed {
            equipment: "feed".to_string(),
            point: PointId(10),
        },
    );
    rejects(
        |model| model.io_points[0].writable = false,
        ValidationError::EquipmentControlNotWritable {
            equipment: "feed".to_string(),
            point: PointId(10),
        },
    );
    rejects(
        |model| model.equipment[0].controls[0].point = PointId(11),
        ValidationError::EquipmentControlNotWritable {
            equipment: "feed".to_string(),
            point: PointId(11),
        },
    );
    rejects(
        |model| {
            model.connections.remove(0);
        },
        ValidationError::EquipmentControlNotConnected {
            equipment: "feed".to_string(),
            point: PointId(10),
        },
    );
    rejects(
        |model| model.equipment[0].controls[0].true_label = Some("Run".to_string()),
        ValidationError::EquipmentControlLabelsNotBoolean {
            equipment: "feed".to_string(),
            point: PointId(10),
        },
    );
    rejects(
        |model| model.equipment[0].controls[0].label.clear(),
        ValidationError::EmptyEquipmentField {
            equipment: "feed".to_string(),
            field: "control.label".to_string(),
        },
    );
}

#[test]
fn metadata_only_equipment_revisions_are_visible() {
    let old = equipment_model();
    let mut revised = old.clone();
    revised.equipment[0].label = "New feed name".to_string();
    let diff = old.diff(&revised);
    assert!(!diff.is_empty());
    assert_eq!(diff.equipment.len(), 1);
    assert_eq!(diff.equipment[0].change, ChangeKind::Changed);
    assert_eq!(diff.equipment[0].element, "equipment \"feed\"");
    assert_eq!(diff.equipment[0].fields[0].field, "label");
    revised.equipment.clear();
    assert_eq!(old.diff(&revised).equipment[0].change, ChangeKind::Removed);
    assert_eq!(revised.diff(&old).equipment[0].change, ChangeKind::Added);
}
