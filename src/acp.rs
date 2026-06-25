//! Agent Client Protocol process management.
//!
//! This module starts an ACP-compatible local agent through the official Rust
//! SDK, sends one prompt turn, and collects streamed assistant text. File and
//! terminal work are left to the launched agent's native utilities.

use std::path::{Path, PathBuf};

use agent_client_protocol::{
    AcpAgent, Agent as AcpAgentRole, Client, ConnectionTo, Dispatch, on_receive_dispatch,
    schema::{
        ClientCapabilities, CloseSessionRequest, ContentBlock, ContentChunk, EnvVariable,
        Implementation, InitializeRequest, LoadSessionRequest, McpServer, McpServerStdio,
        NewSessionRequest, NewSessionResponse, ProtocolVersion, SessionNotification, SessionUpdate,
        StopReason,
    },
    util::MatchDispatch,
};
use tokio::time::{Duration, timeout};

use crate::{
    AppError, Result,
    config::{AgentConfig, AppConfig, BoardConfig},
};

type AcpResult<T> = agent_client_protocol::Result<T>;

/// Result of one completed ACP prompt turn.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentTurn {
    /// ACP stop reason reported by the agent.
    pub stop_reason: String,
    /// Agent session id to reuse for follow-up turns on the same card.
    pub session_id: String,
    /// Aggregated assistant text streamed during the turn.
    pub text: String,
}

/// Live ACP subprocess session configuration.
///
/// The actual subprocess and JSON-RPC connection are owned by the SDK for the
/// duration of `run_turn`.
#[derive(Debug, Clone)]
pub struct AcpSession {
    /// Canonical working directory exposed to the agent.
    root: PathBuf,
    /// Command and arguments used to launch the ACP process.
    command: Vec<String>,
    /// Current ACP session id, if one has already been created or loaded.
    session_id: Option<String>,
    /// Maximum duration allowed for one agent prompt turn.
    timeout_seconds: u64,
    /// Worker configuration forwarded to the embedded board MCP server.
    app_config: AppConfig,
}

impl AcpSession {
    /// Prepare an ACP subprocess session.
    pub async fn create(
        config: &AgentConfig,
        app_config: &AppConfig,
        session_id: impl Into<Option<String>>,
    ) -> Result<Self> {
        Ok(Self {
            root: PathBuf::from(&config.pwd).canonicalize()?,
            command: command_for_config(config)?,
            session_id: session_id.into(),
            timeout_seconds: config.timeout_seconds,
            app_config: app_config.clone(),
        })
    }

    /// Return the launched ACP command and arguments.
    pub fn command(&self) -> &[String] {
        &self.command
    }

    /// Send one prompt to the agent and collect the resulting turn text.
    pub async fn run_turn(&mut self, prompt: &str) -> Result<AgentTurn> {
        let turn = timeout(
            Duration::from_secs(self.timeout_seconds),
            run_sdk_turn(
                self.command.clone(),
                self.root.clone(),
                self.session_id.clone(),
                prompt.to_string(),
                self.app_config.clone(),
            ),
        )
        .await
        .map_err(|_| {
            AppError::Acp(format!(
                "ACP agent timed out after {} seconds",
                self.timeout_seconds
            ))
        })??;
        self.session_id = Some(turn.session_id.clone());
        Ok(turn)
    }
}

/// Resolve the ACP command for an agent configuration.
///
/// Explicit `agent.command` values are used as-is. Known agent names fall back
/// to their standard ACP launcher binary names.
pub fn command_for_config(config: &AgentConfig) -> Result<Vec<String>> {
    if !config.command.is_empty() {
        return Ok(config.command.clone());
    }
    match config.name.to_ascii_lowercase().as_str() {
        "codex" => Ok(vec!["codex-acp".to_string()]),
        "claude" => Ok(vec!["claude-agent-acp".to_string()]),
        name => Err(AppError::Acp(format!(
            "agent.command is required for ACP agent {name:?}"
        ))),
    }
}

/// Serialize configured boards for the MCP server environment.
pub fn boards_env(boards: &[BoardConfig]) -> String {
    serde_json::to_string(boards).unwrap_or_else(|_| "[]".to_string())
}

/// Serialize optional Smartsheet config for the MCP server environment.
pub fn smartsheet_env(config: &Option<crate::config::SmartsheetConfig>) -> String {
    serde_json::to_string(config).unwrap_or_else(|_| "null".to_string())
}

/// Run one ACP turn using the official SDK connection and session APIs.
async fn run_sdk_turn(
    command: Vec<String>,
    root: PathBuf,
    session_id: Option<String>,
    prompt: String,
    app_config: AppConfig,
) -> Result<AgentTurn> {
    let agent = agent_for_config(&command, &root, &app_config)?;
    let mcp_servers = mcp_servers(&root, &app_config)?;
    Client
        .builder()
        .name("kanboard-agent-worker")
        .on_receive_dispatch(
            async move |message: Dispatch, cx: ConnectionTo<AcpAgentRole>| {
                message.respond_with_error(
                    agent_client_protocol::util::internal_error("unhandled ACP client message"),
                    cx,
                )
            },
            on_receive_dispatch!(),
        )
        .connect_with(agent, async move |cx| {
            initialize(&cx).await?;
            let response = start_or_load_session(&cx, &root, session_id, mcp_servers).await?;
            let session_id = response.session_id.clone();
            let mut session = cx.attach_session(response, Vec::new())?;
            session.send_prompt(prompt)?;
            let (stop_reason, text) = read_agent_turn(&mut session).await?;
            let _ = cx
                .send_request(CloseSessionRequest::new(session_id.clone()))
                .block_task()
                .await;
            Ok(AgentTurn {
                stop_reason: stop_reason_to_str(&stop_reason).to_string(),
                session_id: session_id.to_string(),
                text: text.trim().chars().take(6000).collect(),
            })
        })
        .await
        .map_err(|error| AppError::Acp(error.to_string()))
}

/// Build an SDK process wrapper for the configured ACP executable.
fn agent_for_config(command: &[String], root: &Path, app_config: &AppConfig) -> Result<AcpAgent> {
    let (program, args) = command
        .split_first()
        .ok_or_else(|| AppError::Acp("agent command cannot be empty".to_string()))?;
    Ok(AcpAgent::new(McpServer::Stdio(
        McpServerStdio::new("agent", program)
            .args(args.to_vec())
            .env(vec![
                EnvVariable::new("KANBOARD_URL", app_config.server.url.clone()),
                EnvVariable::new("KANBOARD_USER", app_config.server.user.clone()),
                EnvVariable::new("KANBOARD_TOKEN", app_config.server.token.clone()),
                EnvVariable::new("KANBOARD_WORKER_BOARDS", boards_env(&app_config.boards)),
                EnvVariable::new("SMARTSHEET_CONFIG", smartsheet_env(&app_config.smartsheet)),
                EnvVariable::new("KANBOARD_AGENT_PWD", root.to_string_lossy().to_string()),
            ]),
    )))
}

/// Advertise client capabilities to the agent.
async fn initialize(cx: &ConnectionTo<AcpAgentRole>) -> AcpResult<()> {
    cx.send_request(
        InitializeRequest::new(ProtocolVersion::LATEST)
            .client_capabilities(ClientCapabilities::new())
            .client_info(Implementation::new(
                "kanboard-agent-worker",
                env!("CARGO_PKG_VERSION"),
            )),
    )
    .block_task()
    .await
    .map(|_| ())
}

/// Create a new ACP session or attach to a previously saved one.
async fn start_or_load_session(
    cx: &ConnectionTo<AcpAgentRole>,
    root: &Path,
    session_id: Option<String>,
    mcp_servers: Vec<McpServer>,
) -> AcpResult<NewSessionResponse> {
    if let Some(session_id) = session_id {
        let response = cx
            .send_request(
                LoadSessionRequest::new(session_id.clone(), root).mcp_servers(mcp_servers),
            )
            .block_task()
            .await?;
        return Ok(NewSessionResponse::new(session_id)
            .modes(response.modes)
            .meta(response.meta));
    }
    cx.send_request(NewSessionRequest::new(root).mcp_servers(mcp_servers))
        .block_task()
        .await
}

/// Build the board MCP server descriptor for `session/new` or `session/load`.
fn mcp_servers(root: &Path, config: &AppConfig) -> Result<Vec<McpServer>> {
    Ok(vec![McpServer::Stdio(
        McpServerStdio::new(
            "boards",
            std::env::current_exe().unwrap_or_else(|_| PathBuf::from("kanboard-agent-worker")),
        )
        .args(vec!["mcp".to_string()])
        .env(vec![
            EnvVariable::new("KANBOARD_URL", config.server.url.clone()),
            EnvVariable::new("KANBOARD_USER", config.server.user.clone()),
            EnvVariable::new("KANBOARD_TOKEN", config.server.token.clone()),
            EnvVariable::new("KANBOARD_WORKER_BOARDS", boards_env(&config.boards)),
            EnvVariable::new("SMARTSHEET_CONFIG", smartsheet_env(&config.smartsheet)),
            EnvVariable::new("KANBOARD_AGENT_PWD", root.to_string_lossy().to_string()),
        ]),
    )])
}

/// Read streamed assistant text until the turn stop reason arrives.
async fn read_agent_turn(
    session: &mut agent_client_protocol::ActiveSession<'_, AcpAgentRole>,
) -> AcpResult<(StopReason, String)> {
    let mut text = String::new();
    loop {
        match session.read_update().await? {
            agent_client_protocol::SessionMessage::SessionMessage(dispatch) => {
                MatchDispatch::new(dispatch)
                    .if_notification(async |notification: SessionNotification| {
                        if let SessionUpdate::AgentMessageChunk(ContentChunk {
                            content: ContentBlock::Text(chunk),
                            ..
                        }) = notification.update
                        {
                            text.push_str(&chunk.text);
                        }
                        Ok(())
                    })
                    .await
                    .otherwise_ignore()?;
            }
            agent_client_protocol::SessionMessage::StopReason(stop_reason) => {
                return Ok((stop_reason, text));
            }
            _ => {}
        }
    }
}

/// Convert ACP stop reasons to the existing worker string contract.
fn stop_reason_to_str(stop_reason: &StopReason) -> &'static str {
    match stop_reason {
        StopReason::EndTurn => "end_turn",
        StopReason::MaxTokens => "max_tokens",
        StopReason::MaxTurnRequests => "max_turn_requests",
        StopReason::Refusal => "refusal",
        StopReason::Cancelled => "cancelled",
        _ => "unknown",
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for ACP command resolution helpers.

    use crate::config::AgentConfig;

    use super::*;

    /// Known agent names resolve to default ACP launchers, while unknown names require a command.
    #[test]
    fn resolves_default_agent_commands() {
        let base = AgentConfig {
            name: "codex".to_string(),
            command: Vec::new(),
            pwd: ".".to_string(),
            system_prompt: String::new(),
            timeout_seconds: 1,
        };
        assert_eq!(command_for_config(&base).unwrap(), ["codex-acp"]);
        assert_eq!(
            command_for_config(&AgentConfig {
                name: "claude".to_string(),
                ..base.clone()
            })
            .unwrap(),
            ["claude-agent-acp"]
        );
        assert!(
            command_for_config(&AgentConfig {
                name: "other".to_string(),
                ..base
            })
            .is_err()
        );
    }
}
