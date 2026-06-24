//! Pull-based Kanboard worker for running local ACP agents.
//!
//! The crate is split around the runtime flow: load configuration, poll
//! Kanboard, claim work, build an agent prompt, run an ACP turn, and expose
//! Kanboard helper tools through an MCP server.

/// ACP client bridge used to run Codex, Claude, or another ACP-compatible agent.
pub mod acp;
/// YAML configuration loading and validation.
pub mod config;
/// Shared application error and result types.
pub mod error;
/// Kanboard JSON-RPC client and small data helpers.
pub mod kanboard;
/// MCP stdio server exposing Kanboard tools to ACP agents.
pub mod kanboard_mcp;
/// Prompt rendering for Kanboard task descriptions and comments.
pub mod prompt;
/// Smartsheet REST client and Kanboard-compatible adapter helpers.
pub mod smartsheet;
/// Worker lifecycle: polling, claiming, executing, and routing cards.
pub mod worker;

/// Re-export the project-wide error types so callers can use
/// `kanboard_agent_worker::Result` without importing the `error` module.
pub use error::{AppError, Result};
