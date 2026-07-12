//! Strict JSONL primitives for Pi RPC mode.
//!
//! This crate starts with the protocol seam so it can be tested without a
//! Node installation. Process management and the TUI build on these types.

pub mod codec;
pub mod process;
pub mod protocol;

pub use codec::JsonlDecoder;
pub use process::{PiClient, PiError};
pub use protocol::{PiCommand, PiIncoming};
