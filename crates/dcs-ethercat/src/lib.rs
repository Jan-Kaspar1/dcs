//! The `ethercat` device kind's model-facing contract: the field-bus
//! identity, process-image layout, and startup policy a hardware-bound
//! EtherCAT device declares through its `Device.parameters`.
//!
//! The kind is *hardware-bound*: its devices carry the model-level
//! `hardware` marker and the deployment configuration binds their
//! declared logical `bus` name to a host interface outside the model
//! document (decision 47 — host topology never enters the model). The
//! parameter grammar itself — the vocabulary of the device kind — lives
//! in [`params`], parsed by the kind's registered factory in
//! `dcs-assembly` under the decision-29 convention.
//!
//! The backend that speaks EtherCAT is a hardware integration landing
//! under the Lenovo HQ-4 lane; until it does, a declared `ethercat`
//! device fails startup at assembly rather than silently substituting
//! simulation — a model declaring a hardware kind must fail startup if
//! the hardware cannot initialize.

#![warn(missing_docs)]

mod params;

pub use params::{
    ChannelDecl, DEVICE_KIND, DeviceParameters, ImageOffset, StartupPolicy, StationIdentity,
};
