//! Shared error and result types for the worker.

use thiserror::Error;

/// Standard result type used throughout this crate.
///
/// `Result<T>` means a function returns `T` on success or an [`AppError`] on
/// failure. This keeps signatures short while still making failure explicit.
pub type Result<T> = std::result::Result<T, AppError>;

/// Errors that can happen while loading config, talking to Kanboard, running an
/// ACP agent, or serving MCP tools.
#[derive(Debug, Error)]
pub enum AppError {
    /// Configuration file or environment variable problem.
    #[error("config error: {0}")]
    Config(String),
    /// Kanboard API returned an error or unexpected response.
    #[error("Kanboard error: {0}")]
    Kanboard(String),
    /// ACP subprocess, protocol, or callback failure.
    #[error("ACP error: {0}")]
    Acp(String),
    /// MCP tool serving or tool execution failure.
    #[error("MCP error: {0}")]
    Mcp(String),
    /// Filesystem or process I/O failure.
    #[error(transparent)]
    Io(#[from] std::io::Error),
    /// JSON serialization/deserialization failure.
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    /// YAML configuration parsing failure.
    #[error(transparent)]
    Yaml(#[from] serde_yaml::Error),
    /// HTTP client failure before Kanboard returned a JSON-RPC response.
    #[error(transparent)]
    Http(#[from] reqwest::Error),
}
