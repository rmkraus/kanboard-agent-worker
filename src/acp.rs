//! Agent Client Protocol process management.
//!
//! This module starts an ACP-compatible local agent, sends JSON-RPC requests to
//! it, collects streamed agent text, and implements the small set of client
//! callbacks the agent needs while working in the configured repository.

use std::{
    collections::HashMap,
    path::PathBuf,
    process::Stdio,
    sync::{
        Arc,
        atomic::{AtomicI64, Ordering},
    },
};

use serde_json::{Value, json};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{Mutex, Notify, oneshot},
    time::{Duration, timeout},
};
use uuid::Uuid;

use crate::{
    AppError, Result,
    config::{AgentConfig, AppConfig, BoardConfig},
};

/// ACP protocol version spoken by this client.
const PROTOCOL_VERSION: i64 = 1;

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

/// Live ACP subprocess session.
///
/// The session owns the child process, tracks pending JSON-RPC responses, and
/// exposes a turn-based API for worker task execution.
#[derive(Debug)]
pub struct AcpSession {
    /// Canonical working directory exposed to the agent.
    root: PathBuf,
    /// Command and arguments used to launch the ACP process.
    command: Vec<String>,
    /// Shared stdin writer for outbound JSON-RPC messages and callback replies.
    stdin: Arc<Mutex<ChildStdin>>,
    /// Child process handle killed when the session is dropped.
    child: Child,
    /// In-flight JSON-RPC requests waiting for a response by id.
    pending: Arc<Mutex<HashMap<i64, oneshot::Sender<Result<Value>>>>>,
    /// Monotonic JSON-RPC request id source.
    next_id: AtomicI64,
    /// Text accumulated from `session/update` agent message chunks.
    agent_text: Arc<Mutex<String>>,
    /// Current ACP session id, if one has already been created or loaded.
    session_id: Option<String>,
    /// Maximum duration allowed for one agent prompt turn.
    timeout_seconds: u64,
    /// Worker configuration forwarded to the embedded Kanboard MCP server.
    app_config: AppConfig,
}

impl AcpSession {
    /// Start an ACP subprocess and initialize the protocol session.
    pub async fn create(
        config: &AgentConfig,
        app_config: &AppConfig,
        session_id: impl Into<Option<String>>,
    ) -> Result<Self> {
        let command = command_for_config(config)?;
        let root = PathBuf::from(&config.pwd).canonicalize()?;
        let mut child = Command::new(&command[0])
            .args(&command[1..])
            .current_dir(&root)
            .env("KANBOARD_URL", &app_config.server.url)
            .env("KANBOARD_USER", &app_config.server.user)
            .env("KANBOARD_TOKEN", &app_config.server.token)
            .env("KANBOARD_WORKER_BOARDS", boards_env(&app_config.boards))
            .env("KANBOARD_AGENT_PWD", &root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| {
                AppError::Acp(format!(
                    "failed to start ACP command {:?}: {error}",
                    command
                ))
            })?;
        let stdin = child
            .stdin
            .take()
            .ok_or_else(|| AppError::Acp("ACP agent process did not expose stdin".to_string()))?;
        let stdin = Arc::new(Mutex::new(stdin));
        let stdout = child
            .stdout
            .take()
            .ok_or_else(|| AppError::Acp("ACP agent process did not expose stdout".to_string()))?;
        let pending = Arc::new(Mutex::new(HashMap::new()));
        let agent_text = Arc::new(Mutex::new(String::new()));
        let terminals = Arc::new(Mutex::new(HashMap::new()));
        tokio::spawn(read_loop(
            BufReader::new(stdout),
            stdin.clone(),
            pending.clone(),
            agent_text.clone(),
            root.clone(),
            terminals.clone(),
        ));
        let mut session = Self {
            root,
            command,
            stdin,
            child,
            pending,
            next_id: AtomicI64::new(0),
            agent_text,
            session_id: session_id.into(),
            timeout_seconds: config.timeout_seconds,
            app_config: app_config.clone(),
        };
        session.initialize().await?;
        Ok(session)
    }

    /// Return the launched ACP command and arguments.
    pub fn command(&self) -> &[String] {
        &self.command
    }

    /// Send one prompt to the agent and collect the resulting turn text.
    ///
    /// The session id is loaded or created before prompting, then closed after
    /// the turn so a future worker run can reload it from task metadata.
    pub async fn run_turn(&mut self, prompt: &str) -> Result<AgentTurn> {
        self.agent_text.lock().await.clear();
        let session_id = self.session_id_for_turn().await?;
        self.session_id = Some(session_id.clone());
        let response = timeout(
            Duration::from_secs(self.timeout_seconds),
            self.request(
                "session/prompt",
                json!({
                    "sessionId": session_id,
                    "messageId": Uuid::new_v4().to_string(),
                    "prompt": [{"type": "text", "text": prompt}],
                }),
            ),
        )
        .await
        .map_err(|_| {
            AppError::Acp(format!(
                "ACP agent timed out after {} seconds",
                self.timeout_seconds
            ))
        })??;
        let _ = self
            .request("session/close", json!({"sessionId": session_id}))
            .await;
        Ok(AgentTurn {
            stop_reason: response
                .get("stopReason")
                .and_then(Value::as_str)
                .unwrap_or("unknown")
                .to_string(),
            session_id: self.session_id.clone().unwrap_or_default(),
            text: self
                .agent_text
                .lock()
                .await
                .trim()
                .chars()
                .take(6000)
                .collect(),
        })
    }

    /// Negotiate ACP capabilities with the child process.
    async fn initialize(&mut self) -> Result<()> {
        self.request(
            "initialize",
            json!({
                "protocolVersion": PROTOCOL_VERSION,
                "clientCapabilities": {
                    "fs": {"readTextFile": true, "writeTextFile": true},
                    "terminal": true
                },
                "clientInfo": {"name": "kanboard-agent-worker", "version": env!("CARGO_PKG_VERSION")}
            }),
        )
        .await
        .map(|_| ())
    }

    /// Load an existing ACP session or create a new one for the next turn.
    async fn session_id_for_turn(&mut self) -> Result<String> {
        let mcp_servers = json!([self.kanboard_mcp_server()]);
        if let Some(session_id) = &self.session_id {
            self.request(
                "session/load",
                json!({"cwd": self.root, "sessionId": session_id, "mcpServers": mcp_servers}),
            )
            .await?;
            return Ok(session_id.clone());
        }
        let response = self
            .request(
                "session/new",
                json!({"cwd": self.root, "mcpServers": mcp_servers}),
            )
            .await?;
        response
            .get("sessionId")
            .and_then(Value::as_str)
            .map(str::to_string)
            .ok_or_else(|| AppError::Acp(format!("session/new returned no sessionId: {response}")))
    }

    /// Build the MCP server descriptor passed to `session/new` or `session/load`.
    fn kanboard_mcp_server(&self) -> Value {
        json!({
            "name": "kanboard",
            "command": std::env::current_exe()
                .unwrap_or_else(|_| PathBuf::from("kanboard-agent-worker")),
            "args": ["mcp"],
            "env": [
                {"name": "KANBOARD_WORKER_BOARDS", "value": boards_env(&self.app_config.boards)},
                {"name": "KANBOARD_AGENT_PWD", "value": self.root},
            ]
        })
    }

    /// Send a JSON-RPC request to the child process and await the matching response.
    async fn request(&self, method: &str, params: Value) -> Result<Value> {
        let id = self.next_id.fetch_add(1, Ordering::SeqCst);
        let (sender, receiver) = oneshot::channel();
        self.pending.lock().await.insert(id, sender);
        let payload = json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params});
        let mut stdin = self.stdin.lock().await;
        stdin
            .write_all(serde_json::to_string(&payload)?.as_bytes())
            .await?;
        stdin.write_all(b"\n").await?;
        stdin.flush().await?;
        receiver
            .await
            .map_err(|_| AppError::Acp(format!("ACP response channel closed for {method}")))?
    }
}

impl Drop for AcpSession {
    /// Terminate the ACP subprocess when the session owner is dropped.
    fn drop(&mut self) {
        let _ = self.child.start_kill();
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

/// Read JSON-RPC messages from the ACP process until stdout closes.
///
/// Responses complete pending requests. Server-to-client requests are handled
/// locally, and streamed agent text is appended for the eventual worker comment.
async fn read_loop(
    mut stdout: BufReader<tokio::process::ChildStdout>,
    stdin: Arc<Mutex<ChildStdin>>,
    pending: Arc<Mutex<HashMap<i64, oneshot::Sender<Result<Value>>>>>,
    agent_text: Arc<Mutex<String>>,
    root: PathBuf,
    terminals: Arc<Mutex<HashMap<String, TerminalState>>>,
) {
    let mut line = String::new();
    loop {
        line.clear();
        match stdout.read_line(&mut line).await {
            Ok(0) | Err(_) => break,
            Ok(_) => {
                let Ok(message) = serde_json::from_str::<Value>(line.trim()) else {
                    continue;
                };
                if let Some(id) = message.get("id").and_then(Value::as_i64) {
                    if message.get("method").is_some() {
                        respond_to_client_request(&message, id, &root, &stdin, &terminals).await;
                        continue;
                    }
                    if let Some(sender) = pending.lock().await.remove(&id) {
                        let result = if let Some(error) = message.get("error") {
                            Err(AppError::Acp(format!("ACP JSON-RPC error: {error}")))
                        } else {
                            Ok(message.get("result").cloned().unwrap_or(Value::Null))
                        };
                        let _ = sender.send(result);
                    }
                    continue;
                }
                if message.get("method").and_then(Value::as_str) == Some("session/update") {
                    append_agent_text(&message, &agent_text).await;
                }
            }
        }
    }
}

/// Append an agent message chunk from a `session/update` notification.
async fn append_agent_text(message: &Value, agent_text: &Arc<Mutex<String>>) {
    let update = message
        .get("params")
        .and_then(|params| params.get("update"))
        .unwrap_or(message);
    if update.get("sessionUpdate").and_then(Value::as_str) != Some("agent_message_chunk") {
        return;
    }
    let text = update
        .get("content")
        .and_then(|content| content.get("text"))
        .and_then(Value::as_str)
        .unwrap_or("");
    agent_text.lock().await.push_str(text);
}

/// Execute a JSON-RPC request initiated by the ACP process and write the reply.
async fn respond_to_client_request(
    message: &Value,
    id: i64,
    root: &PathBuf,
    stdin: &Arc<Mutex<ChildStdin>>,
    terminals: &Arc<Mutex<HashMap<String, TerminalState>>>,
) {
    let method = message.get("method").and_then(Value::as_str).unwrap_or("");
    let params = message.get("params").cloned().unwrap_or(Value::Null);
    let response = match handle_client_request(method, &params, root, terminals).await {
        Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
        Err(error) => json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {"code": -32603, "message": error.to_string()}
        }),
    };
    if let Ok(encoded) = serde_json::to_string(&response) {
        let mut stdin = stdin.lock().await;
        let _ = stdin.write_all(encoded.as_bytes()).await;
        let _ = stdin.write_all(b"\n").await;
        let _ = stdin.flush().await;
    }
}

/// Dispatch ACP client callback methods to their local handlers.
async fn handle_client_request(
    method: &str,
    params: &Value,
    root: &PathBuf,
    terminals: &Arc<Mutex<HashMap<String, TerminalState>>>,
) -> Result<Value> {
    match method {
        "fs/read_text_file" => read_text_file(params, root).await,
        "fs/write_text_file" => write_text_file(params, root).await,
        "terminal/create" => create_terminal(params, root, terminals).await,
        "terminal/output" => terminal_output(params, terminals).await,
        "terminal/wait_for_exit" => terminal_wait(params, terminals).await,
        "terminal/release" | "terminal/kill" => terminal_forget(params, terminals).await,
        "session/request_permission" => request_permission(params).await,
        other => Err(AppError::Acp(format!(
            "unsupported ACP client method {other}"
        ))),
    }
}

/// Approve a permission request from the local ACP agent.
///
/// The worker runs trusted, configured agents non-interactively. Selecting the
/// regular one-shot allow option keeps the behavior narrow while unblocking
/// agents that ask the ACP client before using tools or terminals.
async fn request_permission(params: &Value) -> Result<Value> {
    let options = params
        .get("options")
        .and_then(Value::as_array)
        .ok_or_else(|| AppError::Acp("session/request_permission requires options".to_string()))?;
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
        options.iter().find_map(|option| {
            let option_id = option.get("optionId").and_then(Value::as_str)?;
            (option_id == *wanted).then_some(option_id)
        })
    })
    .or_else(|| {
        options.iter().find_map(|option| {
            let kind = option.get("kind").and_then(Value::as_str).unwrap_or("");
            let option_id = option.get("optionId").and_then(Value::as_str)?;
            kind.starts_with("allow").then_some(option_id)
        })
    })
    .ok_or_else(|| AppError::Acp("permission request had no allow option".to_string()))?;

    Ok(json!({"outcome": {"outcome": "selected", "optionId": selected}}))
}

/// Read a UTF-8 text file from inside the configured agent root.
async fn read_text_file(params: &Value, root: &PathBuf) -> Result<Value> {
    let path = confined_path(
        root,
        params.get("path").and_then(Value::as_str).unwrap_or("."),
    )?;
    let mut text = tokio::fs::read_to_string(path).await?;
    if let Some(line) = params.get("line").and_then(Value::as_u64) {
        text = text
            .lines()
            .skip(line.saturating_sub(1) as usize)
            .collect::<Vec<_>>()
            .join("\n");
    }
    if let Some(limit) = params.get("limit").and_then(Value::as_u64) {
        text = text.chars().take(limit as usize).collect();
    }
    Ok(json!({"content": text}))
}

/// Write a UTF-8 text file inside the configured agent root.
async fn write_text_file(params: &Value, root: &PathBuf) -> Result<Value> {
    let path = confined_path(
        root,
        params.get("path").and_then(Value::as_str).unwrap_or("."),
    )?;
    if let Some(parent) = path.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    tokio::fs::write(
        path,
        params.get("content").and_then(Value::as_str).unwrap_or(""),
    )
    .await?;
    Ok(json!({}))
}

/// Start a terminal command requested by the ACP process.
///
/// Commands are executed asynchronously and their combined output is stored
/// until the agent polls or waits for the terminal id.
async fn create_terminal(
    params: &Value,
    root: &PathBuf,
    terminals: &Arc<Mutex<HashMap<String, TerminalState>>>,
) -> Result<Value> {
    let command = params
        .get("command")
        .and_then(Value::as_str)
        .ok_or_else(|| AppError::Acp("terminal/create requires command".to_string()))?;
    let args = params
        .get("args")
        .and_then(Value::as_array)
        .map(|items| items.iter().filter_map(Value::as_str).collect::<Vec<_>>())
        .unwrap_or_default();
    let cwd = confined_path(
        root,
        params.get("cwd").and_then(Value::as_str).unwrap_or("."),
    )?;
    let output_byte_limit = params
        .get("outputByteLimit")
        .and_then(Value::as_u64)
        .map(|value| value as usize);
    let terminal_id = Uuid::new_v4().to_string();
    let state = TerminalState::new(output_byte_limit);
    let state_for_task = state.clone();
    let mut process = Command::new(command);
    process.args(args).current_dir(cwd);
    if let Some(env) = params.get("env").and_then(Value::as_array) {
        for item in env {
            if let (Some(name), Some(value)) = (
                item.get("name").and_then(Value::as_str),
                item.get("value").and_then(Value::as_str),
            ) {
                process.env(name, value);
            }
        }
    }
    let child = process.output();
    tokio::spawn(async move {
        let result = child.await.map(TerminalResult::from_output);
        let mut slot = state_for_task.result.lock().await;
        *slot = Some(match result {
            Ok(result) => result,
            Err(error) => TerminalResult {
                output: error.to_string(),
                exit_code: 1,
                signal: None,
            },
        });
        state_for_task.notify.notify_waiters();
    });
    terminals.lock().await.insert(terminal_id.clone(), state);
    Ok(json!({"terminalId": terminal_id}))
}

/// Return terminal output and exit status if the command has completed.
async fn terminal_output(
    params: &Value,
    terminals: &Arc<Mutex<HashMap<String, TerminalState>>>,
) -> Result<Value> {
    let terminal_id = terminal_id(params)?;
    let Some(state) = terminals.lock().await.get(&terminal_id).cloned() else {
        return Err(AppError::Acp(format!("unknown terminal id {terminal_id}")));
    };
    let result = state.result.lock().await.clone();
    Ok(match result {
        Some(result) => {
            let (output, truncated) = state.limit_output(&result.output);
            json!({"output": output, "truncated": truncated, "exitStatus": result.exit_status()})
        }
        None => json!({"output": "", "truncated": false, "exitStatus": Value::Null}),
    })
}

/// Wait for a terminal command to finish and return its exit status.
async fn terminal_wait(
    params: &Value,
    terminals: &Arc<Mutex<HashMap<String, TerminalState>>>,
) -> Result<Value> {
    let terminal_id = terminal_id(params)?;
    let Some(state) = terminals.lock().await.get(&terminal_id).cloned() else {
        return Err(AppError::Acp(format!("unknown terminal id {terminal_id}")));
    };
    loop {
        if let Some(result) = state.result.lock().await.clone() {
            return Ok(result.wait_response());
        }
        state.notify.notified().await;
    }
}

/// Drop stored terminal state for a released or killed terminal id.
async fn terminal_forget(
    params: &Value,
    terminals: &Arc<Mutex<HashMap<String, TerminalState>>>,
) -> Result<Value> {
    let terminal_id = terminal_id(params)?;
    terminals.lock().await.remove(&terminal_id);
    Ok(json!({}))
}

/// Extract a terminal id from ACP terminal callback parameters.
fn terminal_id(params: &Value) -> Result<String> {
    params
        .get("terminalId")
        .and_then(Value::as_str)
        .map(str::to_string)
        .ok_or_else(|| AppError::Acp("terminal id is required".to_string()))
}

/// Resolve a requested path and reject paths outside the configured root.
fn confined_path(root: &PathBuf, path: &str) -> Result<PathBuf> {
    let target = PathBuf::from(path);
    let target = if target.is_absolute() {
        target
    } else {
        root.join(target)
    };
    let parent = target.parent().unwrap_or(root);
    let canonical_parent = parent
        .canonicalize()
        .unwrap_or_else(|_| parent.to_path_buf());
    if !canonical_parent.starts_with(root) {
        return Err(AppError::Acp(format!(
            "Path outside configured pwd: {path}"
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

    /// Return ACP-compatible exit status JSON.
    fn exit_status(&self) -> Value {
        if let Some(signal) = &self.signal {
            json!({"signal": signal})
        } else {
            json!({"exitCode": self.exit_code})
        }
    }

    /// Return the terminal wait response payload.
    fn wait_response(&self) -> Value {
        self.exit_status()
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

    /// Permission prompts select the narrow one-shot allow option when available.
    #[tokio::test]
    async fn request_permission_prefers_one_shot_allow() {
        let response = request_permission(&json!({
            "options": [
                {"kind": "allow_always", "name": "Always Allow", "optionId": "allow_always"},
                {"kind": "allow_once", "name": "Allow", "optionId": "allow"},
                {"kind": "reject_once", "name": "Reject", "optionId": "reject"}
            ]
        }))
        .await
        .unwrap();

        assert_eq!(
            response,
            json!({"outcome": {"outcome": "selected", "optionId": "allow"}})
        );
    }
}
