//! Library facade for `zicato-supervisor`.
//!
//! The crate also produces a single binary (`src/main.rs`) that wires
//! these modules together as a long-running process. Exposing them as a
//! library lets the integration tests in `tests/` exercise the same code
//! paths without spawning the executable.

pub mod action_log;
pub mod divergence;
pub mod epoch;
pub mod index_db;
pub mod ledger;
pub mod log;
pub mod promotion_gate;
pub mod range_containment;
pub mod reader;
pub mod reap;
pub mod routes;
pub mod server;
pub mod sha256;
pub mod signal;
pub mod state;
pub mod statusz;
pub mod watchdog;

#[cfg(test)]
mod test_process_group;
