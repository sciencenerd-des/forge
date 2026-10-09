//! Typed control-plane access for the Forge Rust operator client.
//!
//! The Python control plane intentionally exposes several distinct run
//! representations. Keep those boundaries explicit here: a runtime snapshot
//! is not a durable run record and neither is an offline launcher manifest.

pub mod a2a;
pub mod client;
pub mod config;
pub mod sse;
pub mod types;

pub use a2a::{A2aTask, A2aTaskStatus, RpcError};
pub use client::{ApiError, ForgeApi};
pub use config::{ForgeConfig, ForgeConfigError};
pub use types::*;
