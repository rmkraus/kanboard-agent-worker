//! Agent Client Protocol process management.
//!
//! This module starts an ACP-compatible local agent through the official Rust
//! SDK, sends one prompt turn, collects streamed assistant text, and handles
//! the client callbacks an agent may request while working in the configured
//! repository.

use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::Arc,
};

use agent_client_protocol::{
    AcpAgent, Agent as AcpAgentRole, Client, ConnectionTo, Dispatch, on_receive_dispatch,
    on_receive_request,
    schema::{
        ClientCapabilities, CloseSessionRequest, ContentBlock, ContentChunk, CreateTerminalRequest,
        CreateTerminalResponse, EnvVariable, FileSystemCapabilities, Implementation,
        InitializeRequest, KillTerminalRequest, KillTerminalResponse, LoadSessionRequest,
        McpServer, McpServerStdio, NewSessionRequest, NewSessionResponse, PermissionOption,
        PermissionOptionKind, ProtocolVersion, ReadTextFileRequest, ReadTextFileResponse,
        ReleaseTerminalRequest, ReleaseTerminalResponse, RequestPermissionOutcome,
        RequestPermissionRequest, RequestPermissionResponse, SelectedPermissionOutcome,
        SessionNotification, SessionUpdate, StopReason, TerminalExitStatus, TerminalId,
        TerminalOutputRequest, TerminalOutputResponse, WaitForTerminalExitRequest,
        WaitForTerminalExitResponse, WriteTextFileRequest, WriteTextFileResponse,
    },
    util::MatchDispatch,
};
use tokio::{
    process::Command,
    sync::{Mutex, Notify},
    time::{Duration, timeout},
};
use uuid::Uuid;

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
    let client_state = ClientState::new(root.clone());
    let agent = agent_for_config(&command, &root, &app_config)?;
    let mcp_servers = mcp_servers(&root, &app_config)?;
    Client
        .builder()
        .name("kanboard-agent-worker")
        .on_receive_request(
            {
                let state = client_state.clone();
                async move |request: ReadTextFileRequest, responder, _| {
                    responder.respond(read_text_file(request, &state.root).await?)
                }
            },
            on_receive_request!(),
        )
        .on_receive_request(
            {
                let state = client_state.clone();
                async move |request: WriteTextFileRequest, responder, _| {
                    responder.respond(write_text_file(request, &state.root).await?)
                }
            },
            on_receive_request!(),
        )
        .on_receive_request(
            {
                let state = client_state.clone();
                async move |request: CreateTerminalRequest, responder, _| {
                    responder.respond(create_terminal(request, &state).await?)
                }
            },
            on_receive_request!(),
        )
        .on_receive_request(
            {
                let state = client_state.clone();
                async move |request: TerminalOutputRequest, responder, _| {
                    responder.respond(terminal_output(request, &state).await?)
                }
            },
            on_receive_request!(),
        )
        .on_receive_request(
            {
                let state = client_state.clone();
                async move |request: WaitForTerminalExitRequest, responder, _| {
                    responder.respond(terminal_wait(request, &state).await?)
                }
            },
            on_receive_request!(),
        )
        .on_receive_request(
            {
                let state = client_state.clone();
                async move |request: ReleaseTerminalRequest, responder, _| {
                    responder.respond(terminal_release(request.terminal_id, &state).await?)
                }
            },
            on_receive_request!(),
        )
        .on_receive_request(
            {
                let state = client_state.clone();
                async move |request: KillTerminalRequest, responder, _| {
                    responder.respond(terminal_kill(request.terminal_id, &state).await?)
                }
            },
            on_receive_request!(),
        )
        .on_receive_request(
            async move |request: RequestPermissionRequest, responder, _| {
                responder.respond(request_permission(&request.options)?)
            },
            on_receive_request!(),
        )
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
            .client_capabilities(
                ClientCapabilities::new()
                    .fs(FileSystemCapabilities::new()
                        .read_text_file(true)
                        .write_text_file(true))
                    .terminal(true),
            )
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

/// Shared state for ACP client callbacks.
#[derive(Debug, Clone)]
struct ClientState {
    /// Canonical root that bounds file and terminal cwd callbacks.
    root: PathBuf,
    /// Terminal command state by ACP terminal id.
    terminals: Arc<Mutex<HashMap<String, TerminalState>>>,
}

impl ClientState {
    /// Build callback state for a session root.
    fn new(root: PathBuf) -> Self {
        Self {
            root,
            terminals: Arc::new(Mutex::new(HashMap::new())),
        }
    }
}

/// Approve a permission request from the local ACP agent.
fn request_permission(options: &[PermissionOption]) -> AcpResult<RequestPermissionResponse> {
    let selected = [
        "allow",
        "allow_once",
        "default",
        "acceptEdits",
        "auto",
        "allow_always",
    ]
    .iter()
    .find_map(|wanted| {
        options
            .iter()
            .find(|option| option.option_id.to_string() == *wanted)
            .map(|option| option.option_id.clone())
    })
    .or_else(|| {
        options
            .iter()
            .find(|option| {
                matches!(
                    option.kind,
                    PermissionOptionKind::AllowOnce | PermissionOptionKind::AllowAlways
                )
            })
            .map(|option| option.option_id.clone())
    })
    .ok_or_else(|| {
        agent_client_protocol::util::internal_error("permission request had no allow option")
    })?;

    Ok(RequestPermissionResponse::new(
        RequestPermissionOutcome::Selected(SelectedPermissionOutcome::new(selected)),
    ))
}

/// Read a UTF-8 text file from inside the configured agent root.
async fn read_text_file(
    request: ReadTextFileRequest,
    root: &Path,
) -> AcpResult<ReadTextFileResponse> {
    let path = confined_path(root, &request.path)?;
    let mut lines = tokio::fs::read_to_string(path)
        .await
        .map_err(agent_client_protocol::Error::into_internal_error)?
        .lines()
        .map(str::to_string)
        .collect::<Vec<_>>();
    if let Some(line) = request.line {
        lines = lines
            .into_iter()
            .skip(line.saturating_sub(1) as usize)
            .collect();
    }
    if let Some(limit) = request.limit {
        lines.truncate(limit as usize);
    }
    Ok(ReadTextFileResponse::new(lines.join("\n")))
}

/// Write a UTF-8 text file inside the configured agent root.
async fn write_text_file(
    request: WriteTextFileRequest,
    root: &Path,
) -> AcpResult<WriteTextFileResponse> {
    let path = confined_path(root, &request.path)?;
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent)
            .await
            .map_err(agent_client_protocol::Error::into_internal_error)?;
    }
    tokio::fs::write(path, request.content)
        .await
        .map_err(agent_client_protocol::Error::into_internal_error)?;
    Ok(WriteTextFileResponse::new())
}

/// Start a terminal command requested by the ACP process.
async fn create_terminal(
    request: CreateTerminalRequest,
    state: &ClientState,
) -> AcpResult<CreateTerminalResponse> {
    let cwd = match request.cwd {
        Some(path) => confined_path(&state.root, &path)?,
        None => state.root.clone(),
    };
    let terminal_id = Uuid::new_v4().to_string();
    let terminal = TerminalState::new(request.output_byte_limit.map(|limit| limit as usize));
    let terminal_for_task = terminal.clone();
    let mut process = Command::new(&request.command);
    process.args(request.args).current_dir(cwd);
    for env in request.env {
        process.env(env.name, env.value);
    }
    tokio::spawn(async move {
        let result = process.output().await.map(TerminalResult::from_output);
        *terminal_for_task.result.lock().await = Some(match result {
            Ok(result) => result,
            Err(error) => TerminalResult {
                output: error.to_string(),
                exit_code: 1,
                signal: None,
            },
        });
        terminal_for_task.notify.notify_waiters();
    });
    state
        .terminals
        .lock()
        .await
        .insert(terminal_id.clone(), terminal);
    Ok(CreateTerminalResponse::new(terminal_id))
}

/// Return terminal output and exit status if the command has completed.
async fn terminal_output(
    request: TerminalOutputRequest,
    state: &ClientState,
) -> AcpResult<TerminalOutputResponse> {
    let terminal = terminal_state(&request.terminal_id, state).await?;
    let result = terminal.result.lock().await.clone();
    Ok(match result {
        Some(result) => {
            let (output, truncated) = terminal.limit_output(&result.output);
            TerminalOutputResponse::new(output, truncated).exit_status(result.exit_status())
        }
        None => TerminalOutputResponse::new("", false),
    })
}

/// Wait for a terminal command to finish and return its exit status.
async fn terminal_wait(
    request: WaitForTerminalExitRequest,
    state: &ClientState,
) -> AcpResult<WaitForTerminalExitResponse> {
    let terminal = terminal_state(&request.terminal_id, state).await?;
    loop {
        if let Some(result) = terminal.result.lock().await.clone() {
            return Ok(WaitForTerminalExitResponse::new(result.exit_status()));
        }
        terminal.notify.notified().await;
    }
}

/// Release terminal state.
async fn terminal_release(
    terminal_id: TerminalId,
    state: &ClientState,
) -> AcpResult<ReleaseTerminalResponse> {
    state
        .terminals
        .lock()
        .await
        .remove(&terminal_id.to_string());
    Ok(ReleaseTerminalResponse::new())
}

/// Kill terminal state.
async fn terminal_kill(
    terminal_id: TerminalId,
    state: &ClientState,
) -> AcpResult<KillTerminalResponse> {
    state
        .terminals
        .lock()
        .await
        .remove(&terminal_id.to_string());
    Ok(KillTerminalResponse::new())
}

/// Return tracked terminal state by id.
async fn terminal_state(terminal_id: &TerminalId, state: &ClientState) -> AcpResult<TerminalState> {
    state
        .terminals
        .lock()
        .await
        .get(&terminal_id.to_string())
        .cloned()
        .ok_or_else(|| agent_client_protocol::util::internal_error("unknown terminal id"))
}

/// Resolve a requested path and reject paths outside the configured root.
fn confined_path(root: &Path, path: &Path) -> AcpResult<PathBuf> {
    let target = if path.is_absolute() {
        path.to_path_buf()
    } else {
        root.join(path)
    };
    let parent = target.parent().unwrap_or(root);
    let canonical_parent = parent
        .canonicalize()
        .unwrap_or_else(|_| parent.to_path_buf());
    if !canonical_parent.starts_with(root) {
        return Err(agent_client_protocol::util::internal_error(format!(
            "Path outside configured pwd: {}",
            path.display()
        )));
    }
    Ok(target)
}

/// Shared state for an ACP terminal command.
#[derive(Debug, Clone)]
struct TerminalState {
    /// Completed command result, populated by the background task.
    result: Arc<Mutex<Option<TerminalResult>>>,
    /// Notification used to wake waiters once a command finishes.
    notify: Arc<Notify>,
    /// Optional maximum number of output bytes exposed to the agent.
    output_byte_limit: Option<usize>,
}

impl TerminalState {
    /// Create empty terminal state with an optional output limit.
    fn new(output_byte_limit: Option<usize>) -> Self {
        Self {
            result: Arc::new(Mutex::new(None)),
            notify: Arc::new(Notify::new()),
            output_byte_limit,
        }
    }

    /// Apply the configured output limit and report whether truncation occurred.
    fn limit_output(&self, output: &str) -> (String, bool) {
        let Some(limit) = self.output_byte_limit else {
            return (output.to_string(), false);
        };
        if output.len() <= limit {
            return (output.to_string(), false);
        }
        (output.chars().take(limit).collect(), true)
    }
}

/// Completed terminal command output and status.
#[derive(Debug, Clone)]
struct TerminalResult {
    /// Combined stdout and stderr text.
    output: String,
    /// Process exit code used when no signal is recorded.
    exit_code: i32,
    /// Process signal name, when available.
    signal: Option<String>,
}

impl TerminalResult {
    /// Convert Tokio process output into the ACP terminal result shape.
    fn from_output(output: std::process::Output) -> Self {
        let mut combined = String::from_utf8_lossy(&output.stdout).to_string();
        if !output.stderr.is_empty() {
            if !combined.is_empty() {
                combined.push('\n');
            }
            combined.push_str(&String::from_utf8_lossy(&output.stderr));
        }
        Self {
            output: combined,
            exit_code: output.status.code().unwrap_or(1),
            signal: None,
        }
    }

    /// Return ACP-compatible exit status.
    fn exit_status(&self) -> TerminalExitStatus {
        TerminalExitStatus::new()
            .exit_code((self.signal.is_none()).then_some(self.exit_code.max(0) as u32))
            .signal(self.signal.clone())
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for ACP command resolution helpers.

    use agent_client_protocol::schema::{PermissionOption, PermissionOptionKind};
    use serde_json::json;

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

    /// Permission prompts select the narrow one-shot allow option when available.
    #[test]
    fn request_permission_prefers_one_shot_allow() {
        let response = request_permission(&[
            PermissionOption::new(
                "allow_always",
                "Always Allow",
                PermissionOptionKind::AllowAlways,
            ),
            PermissionOption::new("allow", "Allow", PermissionOptionKind::AllowOnce),
            PermissionOption::new("reject", "Reject", PermissionOptionKind::RejectOnce),
        ])
        .unwrap();

        assert_eq!(
            serde_json::to_value(response).unwrap(),
            json!({"outcome": {"outcome": "selected", "optionId": "allow"}})
        );
    }
}
