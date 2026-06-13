//! Library facade for `zicato-supervisor`.
//!
//! The crate also produces a single binary (`src/main.rs`) that wires
//! these modules together as a long-running process. Exposing them as a
//! library lets the integration tests in `tests/` exercise the same code
//! paths without spawning the executable.

pub mod action_log;
pub mod epoch;
pub mod fold_stats;
pub mod index_db;
pub mod log;
pub mod reader;
pub mod reap;
pub mod routes;
pub mod run_log;
pub mod server;
pub mod signal;
pub mod sse;
pub mod state;
pub mod static_assets;
pub mod statusz;
pub mod tournaments;
pub mod watchdog;
pub mod watcher;
