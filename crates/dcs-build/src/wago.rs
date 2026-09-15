//! The Wago EtherCAT QA rig — issue #335's composition for the Lenovo
//! hardware-QA milestone HQ-4 ("EtherCAT field path"), consumed by
//! `docs/lenovo-hardware-qa-plan.md`'s supervised commissioning
//! sequence.
//!
//! [`wago_rig`] emits the rig's [`PlantModel`] document. Two
//! checked-in emissions live at `crates/dcs-demo/fixtures/` — the
//! established builder-fixture location this issue names:
//!
//! - `wago_rig.json` — the hardware binding: one `ethercat` device on
//!   logical bus `ecat0`, `hardware: true`, declaring the manifest's
//!   expected 750-354 station identity, the 750-501 DO1/DO2 and
//!   750-400 DI1/DI2 channels mapped into the process images,
//!   `exchange_miss_threshold`, `safe_outputs`, and the
//!   `{"on_mismatch": "fail"}` startup policy the vocabulary admits.
//! - `wago_rig_sim.json` — the same control path re-emitted over a
//!   local `sim` device with identical channel names, plus one extra
//!   declaration: the `di1` ← `do1` point-to-point wire playing the
//!   rig's loopback — the simulated binding #328's regression clause
//!   exercises.
//!
//! ## What the hardware document does not declare
//!
//! `docs/wago-ethercat-rig-manifest.md` is authoritative: the physical
//! DO→DI loopback wiring is **unverified** — "none has been confirmed;
//! do not infer one". The `ethercat` document therefore declares the
//! channel mapping and the commissioning control path only; it carries
//! no point-to-point wire, because no software route may substitute
//! for a physical check. The simulation document declares the wire the
//! supervised physical check verifies — DI1 follows DO1, DI2 stays
//! clear — so the scripted run's telemetry sequence is exactly the
//! sequence commissioning expects to observe on the rig.
//!
//! The station `identity` fields are the manifest's expected values —
//! vendor `0x21` (Wago) is datasheet-derived; product and revision are
//! the declaration commissioning discovery verifies against the
//! answering station. A mismatch fails startup before OP per the
//! declared `fail` policy, never substituting simulation.
//!
//! ## The control path
//!
//! A `digital-output` instance drives `do1` from the writable internal
//! `do1-command` point — the receipted operator surface, since field
//! `Out` points refuse direct command writes. Two `digital-input`
//! instances condition `di1` (the loopback witness) and `di2` (the
//! channel-independence witness) onto internal observed points, and a
//! signal names every point for the monitor. The commissioning
//! sequence this supports: a receipted `WriteValue` to
//! `do1-command` → the component stages `do1` → the next exchange
//! publishes it → `di1` follows while `di2` stays clear.
//!
//! ## The declared point-id scheme
//!
//! Field points occupy `1..=4` (`di1`, `di2`, `do1`, `do2`); internal
//! points start at `10` (`do1-command`, `di1-witness`, `di2-witness`);
//! every point's signal sits at `10000 + point`. The scheme is
//! deterministic in declaration order, so identical builder
//! invocations emit identical documents.

use crate::ethercat::{EthercatSpec, ImageOffset, StationIdentity};
use crate::specs::{DigitalInputSpec, DigitalOutputSpec};
use crate::{BuildError, ChannelRef, Direction, PlantBuilder, PointId, SignalId, parameters};
use dcs_model::{ComponentId, DeviceId, PlantModel};

/// The logical bus name the `ethercat` device declares — deployment
/// configuration binds it to the rig's field NIC
/// (`enx00e04c751f7c`); the model never names an interface.
pub const ECAT_BUS: &str = "ecat0";

/// The Wago vendor id the manifest's identity check expects
/// (`0x00000021`, serialized decimal).
pub const WAGO_VENDOR: u32 = 0x21;
/// The expected product code for the 750-354 coupler — the
/// datasheet-derived value commissioning discovery verifies.
pub const WAGO_PRODUCT: u32 = 750354;
/// The expected revision — declared so the discovery check can fail it
/// honestly; the manifest records the real value as unknown until read
/// at commissioning.
pub const WAGO_REVISION: u32 = 1;
/// Consecutive missed cyclic exchanges before the device's reads
/// escalate to `IoError::Disconnected` — the cyclic contract's
/// declared threshold, matching the kind's reference declaration.
pub const EXCHANGE_MISS_THRESHOLD: u64 = 3;

/// The device kind the simulation binding declares — the local
/// `SimDriver`'s `sim` family.
pub const SIM_KIND: &str = "sim";

/// The monitor display group every rig signal files under.
pub const GROUP: &str = "wago-rig";

/// The declared point ids — the engineering identifiers the scripted
/// run and the commissioning sequence address.
pub mod points {
    use crate::PointId;

    /// The 750-400 channel-1 digital input (`Bool`, `In`) — the DO1
    /// loopback witness.
    pub const DI1: PointId = PointId(1);
    /// The 750-400 channel-2 digital input (`Bool`, `In`) — the
    /// channel-independence witness: it must not follow DO1.
    pub const DI2: PointId = PointId(2);
    /// The 750-501 channel-1 digital output (`Bool`, `Out`) — the
    /// commissioning path's driven point.
    pub const DO1: PointId = PointId(3);
    /// The 750-501 channel-2 digital output (`Bool`, `Out`) — held at
    /// its declared safe state by this composition.
    pub const DO2: PointId = PointId(4);
    /// The writable internal `In` point an operator's receipted
    /// `WriteValue` lands on — the command surface driving DO1.
    pub const DO1_COMMAND: PointId = PointId(10);
    /// The internal `Out` point carrying the `digital-input` instance's
    /// conditioned copy of DI1.
    pub const DI1_WITNESS: PointId = PointId(11);
    /// The internal `Out` point carrying the conditioned copy of DI2.
    pub const DI2_WITNESS: PointId = PointId(12);
}

/// The channel names the device declares — identical on both bindings,
/// so the point wiring binds unchanged.
pub mod channels {
    /// 750-400 channel 1 — `di1`.
    pub const DI1: &str = "di1";
    /// 750-400 channel 2 — `di2`.
    pub const DI2: &str = "di2";
    /// 750-501 channel 1 — `do1`.
    pub const DO1: &str = "do1";
    /// 750-501 channel 2 — `do2`.
    pub const DO2: &str = "do2";
}

/// Which field binding [`wago_rig`] emits the document for.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RigBinding {
    /// The hardware binding: the `ethercat` device on logical bus
    /// [`ECAT_BUS`] — `wago_rig.json`.
    Ethercat,
    /// The simulation binding: the same channels on a local `sim`
    /// device plus the declared `di1` ← `do1` loopback wire —
    /// `wago_rig_sim.json`.
    Sim,
}

/// Where everything the composition declares landed — the ids the
/// scripted run, the monitor, and any embedding surface address.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WagoRigLayout {
    /// The declared field device's id (`1` on both bindings).
    pub device: DeviceId,
    /// The DI1 field `In` point — the loopback witness.
    pub di1: PointId,
    /// The DI2 field `In` point — the channel-independence witness.
    pub di2: PointId,
    /// The DO1 field `Out` point — the driven output.
    pub do1: PointId,
    /// The DO2 field `Out` point — held at its safe state.
    pub do2: PointId,
    /// The writable internal `In` point the receipted operator command
    /// lands on.
    pub do1_command: PointId,
    /// The internal `Out` point carrying DI1's conditioned value.
    pub di1_witness: PointId,
    /// The internal `Out` point carrying DI2's conditioned value.
    pub di2_witness: PointId,
    /// The `digital-output` instance driving `do1`.
    pub command_output: ComponentId,
    /// The `digital-input` instance conditioning `di1`.
    pub di1_input: ComponentId,
    /// The `digital-input` instance conditioning `di2`.
    pub di2_input: ComponentId,
}

/// The composed rig: the emitted document plus the layout every
/// declared id landed on.
#[derive(Debug, Clone)]
pub struct WagoRig {
    /// The versioned plant-model document.
    pub model: PlantModel,
    /// The id map into it.
    pub layout: WagoRigLayout,
}

/// The manifest's expected station identity — what master discovery
/// verifies before outputs enable.
pub fn station_identity() -> StationIdentity {
    StationIdentity {
        vendor: WAGO_VENDOR,
        product: WAGO_PRODUCT,
        revision: WAGO_REVISION,
    }
}

/// The device's four channel declarations, binding-specific: the
/// `ethercat` arm records each channel's process-image offset (and the
/// outputs' declared safe states) in the device parameters; the `sim`
/// arm declares the same names and directions with no parameters.
fn declare_channels(
    plant: &mut PlantBuilder,
    binding: RigBinding,
) -> (DeviceId, ChannelRef, ChannelRef, ChannelRef, ChannelRef) {
    match binding {
        RigBinding::Ethercat => {
            let device = plant.ethercat(EthercatSpec::new(
                ECAT_BUS,
                station_identity(),
                EXCHANGE_MISS_THRESHOLD,
            ));
            (
                device,
                plant.ethercat_input::<bool>(
                    device,
                    channels::DI1,
                    ImageOffset::Bit { byte: 0, bit: 0 },
                ),
                plant.ethercat_input::<bool>(
                    device,
                    channels::DI2,
                    ImageOffset::Bit { byte: 0, bit: 1 },
                ),
                plant.ethercat_output::<bool>(
                    device,
                    channels::DO1,
                    ImageOffset::Bit { byte: 0, bit: 0 },
                    false,
                ),
                plant.ethercat_output::<bool>(
                    device,
                    channels::DO2,
                    ImageOffset::Bit { byte: 0, bit: 1 },
                    false,
                ),
            )
        }
        RigBinding::Sim => {
            let device = plant.device(SIM_KIND).id;
            (
                device,
                plant.channel::<bool>(device, channels::DI1, Direction::In),
                plant.channel::<bool>(device, channels::DI2, Direction::In),
                plant.channel::<bool>(device, channels::DO1, Direction::Out),
                plant.channel::<bool>(device, channels::DO2, Direction::Out),
            )
        }
    }
}

/// Composes the Wago rig under `binding` and emits its [`PlantModel`].
///
/// Both bindings share the channel names, the point/signal/component
/// sets, and the control-path wiring — only the device declaration and,
/// on [`RigBinding::Sim`], the appended field wire differ, so the
/// emitted documents prove the identical control path against two
/// registered driver kinds.
pub fn wago_rig(binding: RigBinding) -> Result<WagoRig, BuildError> {
    let mut plant = PlantBuilder::new();

    let (device, di1_ch, di2_ch, do1_ch, do2_ch) = declare_channels(&mut plant, binding);

    // The field points bound to the manifest's approved channels.
    let di1 = plant.field_input::<bool>(points::DI1, di1_ch, false);
    let di2 = plant.field_input::<bool>(points::DI2, di2_ch, false);
    let do1 = plant.field_output::<bool>(points::DO1, do1_ch);
    let do2 = plant.field_output::<bool>(points::DO2, do2_ch);

    // The command surface and the conditioned-value carriers.
    let do1_command = plant.internal_input::<bool>(points::DO1_COMMAND, false, true);
    let di1_witness = plant.internal_output::<bool>(points::DI1_WITNESS, false);
    let di2_witness = plant.internal_output::<bool>(points::DI2_WITNESS, false);

    // The reusable composition the commissioning sequence exercises.
    let command_output = plant.add(DigitalOutputSpec::new(parameters([])));
    let di1_input = plant.add(DigitalInputSpec::new(parameters([])));
    let di2_input = plant.add(DigitalInputSpec::new(parameters([])));

    plant.connect(do1_command, &command_output.input);
    plant.connect(&command_output.out, do1);
    plant.connect(di1, &di1_input.input);
    plant.connect(&di1_input.out, di1_witness);
    plant.connect(di2, &di2_input.input);
    plant.connect(&di2_input.out, di2_witness);
    if binding == RigBinding::Sim {
        // The declared loopback wire the simulated binding plays: DO1's
        // writes feed DI1's reads one step later — the field check the
        // hardware document deliberately leaves to physical
        // commissioning. DI2 is left unwired so the independence
        // witness stays clear.
        plant.connect(di1, do1);
    }

    // The monitoring names — every point signalled so the monitor
    // renders it; bool signals carry the deliberate empty unit.
    let signal = |plant: &mut PlantBuilder, point: PointId, name: &str, description: &str| {
        plant
            .signal(SignalId(10_000 + point.0), name, point)
            .unit("")
            .description(description)
            .group(GROUP);
    };
    signal(
        &mut plant,
        points::DI1,
        "rig-di1",
        "750-400 ch1 digital input — DO1 loopback witness; physical wiring \
         unverified until supervised commissioning confirms it",
    );
    signal(
        &mut plant,
        points::DI2,
        "rig-di2",
        "750-400 ch2 digital input — channel-independence witness; physical \
         wiring unverified until supervised commissioning",
    );
    signal(
        &mut plant,
        points::DO1,
        "rig-do1",
        "750-501 ch1 digital output — declared process-image mapping verified \
         at supervised commissioning",
    );
    signal(
        &mut plant,
        points::DO2,
        "rig-do2",
        "750-501 ch2 digital output — declared process-image mapping; held at \
         its declared safe state by this composition",
    );
    signal(
        &mut plant,
        points::DO1_COMMAND,
        "rig-do1-command",
        "operator command for 750-501 DO1 — the receipted write surface the \
         commissioning sequence drives",
    );
    signal(
        &mut plant,
        points::DI1_WITNESS,
        "rig-di1-witness",
        "digital-input-conditioned copy of rig-di1 — the observed loopback \
         transition",
    );
    signal(
        &mut plant,
        points::DI2_WITNESS,
        "rig-di2-witness",
        "digital-input-conditioned copy of rig-di2 — stays clear while DO1 \
         toggles",
    );

    let layout = WagoRigLayout {
        device,
        di1: di1.id(),
        di2: di2.id(),
        do1: do1.id(),
        do2: do2.id(),
        do1_command: do1_command.id(),
        di1_witness: di1_witness.id(),
        di2_witness: di2_witness.id(),
        command_output: command_output.id,
        di1_input: di1_input.id,
        di2_input: di2_input.id,
    };
    plant.build().map(|model| WagoRig { model, layout })
}
