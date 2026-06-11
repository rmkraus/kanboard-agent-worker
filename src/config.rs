//! Configuration loading for the worker CLI.
//!
//! This module parses YAML into permissive `Raw*` structs first, then converts
//! them into validated public config structs used by the rest of the app.

use std::{env, fs, path::Path};

use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::{AppError, Result};

/// Kanboard server credentials for this worker identity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ServerConfig {
    /// Kanboard username used for API auth and task assignment matching.
    pub user: String,
    /// Kanboard password or personal access token.
    pub token: String,
    /// Kanboard base URL or direct `/jsonrpc.php` endpoint.
    pub url: String,
}

/// Column mapping for one Kanboard project.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BoardConfig {
    /// Kanboard project id.
    pub id: Value,
    /// Queue column name where work is considered ready.
    pub todo: String,
    /// Column name used while an agent is actively working.
    pub working: String,
    /// Column name used when the worker or agent needs human help.
    pub blocked: String,
    /// Column name used for completed work.
    pub done: String,
}

/// Settings for launching the local ACP-compatible agent process.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AgentConfig {
    /// Logical agent name, such as `codex` or `claude`.
    pub name: String,
    /// Explicit command and arguments. Empty means use the default for `name`.
    pub command: Vec<String>,
    /// Working directory where the agent process should run.
    pub pwd: String,
    /// Extra instructions appended to the default worker prompt.
    pub system_prompt: String,
    /// Maximum duration for one ACP prompt turn.
    pub timeout_seconds: u64,
}

/// Polling and concurrency controls for the worker loop.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkerSettings {
    /// Maximum number of claimed tasks to run concurrently.
    pub max_concurrency: usize,
    /// Delay between polls when no work is available.
    pub poll_interval: u64,
}

/// One known agent/user that may receive generated subtasks.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RosterEntry {
    /// Kanboard username.
    pub name: String,
    /// Human-readable description shown in the agent prompt.
    pub description: String,
}

/// Fully validated configuration used by the worker runtime.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct AppConfig {
    /// Kanboard connection and identity settings.
    pub server: ServerConfig,
    /// Worker loop behavior.
    pub worker: WorkerSettings,
    /// ACP agent launch behavior.
    pub agent: AgentConfig,
    /// Boards this worker should poll.
    pub boards: Vec<BoardConfig>,
    /// Optional roster included in prompts for subtask handoffs.
    pub roster: Vec<RosterEntry>,
}

/// Direct representation of the YAML root, where sections may be absent.
#[derive(Debug, Deserialize)]
struct RawConfig {
    /// Optional raw server section.
    server: Option<RawServer>,
    /// Optional raw worker settings section.
    worker: Option<RawWorker>,
    /// Optional raw agent launch section.
    agent: Option<RawAgent>,
    /// Optional raw board mappings.
    boards: Option<Vec<RawBoard>>,
    /// Optional raw roster entries.
    roster: Option<Vec<RawRosterEntry>>,
}

/// Raw server section before required fields and environment overrides are applied.
#[derive(Debug, Deserialize)]
struct RawServer {
    /// Optional Kanboard username.
    user: Option<String>,
    /// Optional Kanboard API token or password.
    token: Option<String>,
    /// Optional Kanboard base URL.
    url: Option<String>,
}

/// Raw worker settings section before defaults and numeric validation.
#[derive(Debug, Deserialize)]
struct RawWorker {
    /// Optional maximum concurrency as YAML string or number.
    max_concurrency: Option<Value>,
    /// Optional poll interval as YAML string or number.
    poll_interval: Option<Value>,
}

/// Raw agent section before command parsing and path resolution.
#[derive(Debug, Deserialize)]
struct RawAgent {
    /// Optional logical agent name.
    name: Option<String>,
    /// Optional command as a shell string or YAML list.
    command: Option<Value>,
    /// Optional working directory.
    pwd: Option<String>,
    /// Deprecated alias for `pwd`.
    cwd: Option<String>,
    /// Optional additional system prompt text.
    system_prompt: Option<String>,
    /// Optional turn timeout as YAML string or number.
    timeout_seconds: Option<Value>,
}

/// Raw board mapping before required-field validation.
#[derive(Debug, Deserialize)]
struct RawBoard {
    /// Optional Kanboard project id.
    id: Option<Value>,
    /// Optional ready column title.
    todo: Option<String>,
    /// Optional in-progress column title.
    working: Option<String>,
    /// Optional blocked column title.
    blocked: Option<String>,
    /// Optional done column title.
    done: Option<String>,
}

/// Raw roster entry before required-name validation.
#[derive(Debug, Deserialize)]
struct RawRosterEntry {
    /// Optional Kanboard username.
    name: Option<String>,
    /// Optional prompt-facing description.
    description: Option<String>,
}

/// Load, expand, parse, and validate a YAML config file.
///
/// Environment variables such as `KANBOARD_URL` and `AGENT_PWD` override the
/// values in YAML. Relative agent paths are resolved relative to the config
/// file's directory.
pub fn load_config(path: impl AsRef<Path>) -> Result<AppConfig> {
    let path = path.as_ref();
    if !path.exists() {
        return Err(AppError::Config(format!(
            "Config file not found: {}",
            path.display()
        )));
    }
    let config_dir = path
        .canonicalize()?
        .parent()
        .unwrap_or(Path::new("."))
        .to_path_buf();
    let raw_text = expand_env(&fs::read_to_string(path)?);
    let raw: RawConfig = serde_yaml::from_str(&raw_text)?;
    let server_raw = raw
        .server
        .ok_or_else(|| AppError::Config("server must be a mapping".to_string()))?;
    let boards_raw = raw
        .boards
        .filter(|boards| !boards.is_empty())
        .ok_or_else(|| AppError::Config("boards must be a non-empty list".to_string()))?;
    let worker_raw = raw.worker.unwrap_or(RawWorker {
        max_concurrency: None,
        poll_interval: None,
    });
    let agent_raw = raw.agent.unwrap_or(RawAgent {
        name: None,
        command: None,
        pwd: None,
        cwd: None,
        system_prompt: None,
        timeout_seconds: None,
    });

    let server = ServerConfig {
        user: env_or_value("KANBOARD_USER", server_raw.user, "server.user")?,
        token: env_or_value("KANBOARD_TOKEN", server_raw.token, "server.token")?,
        url: env_or_value("KANBOARD_URL", server_raw.url, "server.url")?,
    };
    let worker = WorkerSettings {
        max_concurrency: positive_int_env(
            "WORKER_MAX_CONCURRENCY",
            worker_raw.max_concurrency,
            1,
            "worker.max_concurrency",
        )? as usize,
        poll_interval: positive_int_env(
            "WORKER_POLL_INTERVAL",
            worker_raw.poll_interval,
            10,
            "worker.poll_interval",
        )?,
    };
    let command = match env::var("AGENT_COMMAND")
        .ok()
        .filter(|value| !value.trim().is_empty())
    {
        Some(value) => shlex::split(&value)
            .ok_or_else(|| AppError::Config("AGENT_COMMAND could not be parsed".to_string()))?,
        None => command_vec(agent_raw.command)?,
    };
    let agent = AgentConfig {
        name: agent_raw.name.unwrap_or_else(|| "local".to_string()),
        command,
        pwd: agent_pwd(agent_raw.pwd.or(agent_raw.cwd), &config_dir)?,
        system_prompt: agent_raw
            .system_prompt
            .unwrap_or_default()
            .trim()
            .to_string(),
        timeout_seconds: positive_int(
            agent_raw.timeout_seconds.unwrap_or(Value::from(3600)),
            "agent.timeout_seconds",
        )?,
    };
    let boards = boards_raw
        .into_iter()
        .enumerate()
        .map(|(index, raw)| board_from_raw(raw, index))
        .collect::<Result<Vec<_>>>()?;
    let roster = raw
        .roster
        .unwrap_or_default()
        .into_iter()
        .enumerate()
        .map(|(index, raw)| roster_from_raw(raw, index))
        .collect::<Result<Vec<_>>>()?;

    Ok(AppConfig {
        server,
        worker,
        agent,
        boards,
        roster,
    })
}

/// Return an environment override or YAML value for a required string field.
fn env_or_value(env_name: &str, value: Option<String>, path: &str) -> Result<String> {
    let value = env::var(env_name).ok().or(value).unwrap_or_default();
    if value.is_empty() {
        return Err(AppError::Config(format!("{path} is required")));
    }
    Ok(value)
}

/// Parse a positive integer from an environment override, YAML value, or default.
fn positive_int_env(env_name: &str, value: Option<Value>, default: u64, path: &str) -> Result<u64> {
    if let Ok(value) = env::var(env_name) {
        return positive_int(Value::from(value), path);
    }
    positive_int(value.unwrap_or(Value::from(default)), path)
}

/// Parse and validate a positive integer from a JSON/YAML value.
fn positive_int(value: Value, path: &str) -> Result<u64> {
    let parsed = match value {
        Value::Number(number) => number.as_u64(),
        Value::String(value) => value.parse::<u64>().ok(),
        _ => None,
    }
    .ok_or_else(|| AppError::Config(format!("{path} must be an integer")))?;
    if parsed < 1 {
        return Err(AppError::Config(format!("{path} must be >= 1")));
    }
    Ok(parsed)
}

/// Convert an optional command string or array into argv form.
fn command_vec(value: Option<Value>) -> Result<Vec<String>> {
    match value {
        Some(Value::String(value)) => shlex::split(&value)
            .ok_or_else(|| AppError::Config("agent.command could not be parsed".to_string())),
        Some(Value::Array(values)) => Ok(values
            .into_iter()
            .map(|item| match item {
                Value::String(value) => value,
                other => other.to_string(),
            })
            .collect()),
        _ => Ok(Vec::new()),
    }
}

/// Resolve the agent working directory from config, environment, or default.
fn agent_pwd(value: Option<String>, config_dir: &Path) -> Result<String> {
    let raw = env::var("AGENT_PWD")
        .ok()
        .or(value)
        .unwrap_or_else(|| ".".to_string());
    if raw.is_empty() {
        return Err(AppError::Config("agent.pwd must not be empty".to_string()));
    }
    let path = shellexpand_tilde(&raw);
    let path = if path.is_absolute() {
        path
    } else {
        config_dir.join(path)
    };
    let path = path.canonicalize()?;
    if !path.is_dir() {
        return Err(AppError::Config(format!(
            "agent.pwd must be an existing directory: {}",
            path.display()
        )));
    }
    Ok(path.to_string_lossy().to_string())
}

/// Validate and convert one raw board mapping.
fn board_from_raw(raw: RawBoard, index: usize) -> Result<BoardConfig> {
    let missing = [
        ("id", raw.id.is_none()),
        ("todo", raw.todo.as_deref().unwrap_or("").is_empty()),
        ("working", raw.working.as_deref().unwrap_or("").is_empty()),
        ("blocked", raw.blocked.as_deref().unwrap_or("").is_empty()),
        ("done", raw.done.as_deref().unwrap_or("").is_empty()),
    ]
    .into_iter()
    .filter_map(|(name, missing)| missing.then_some(name))
    .collect::<Vec<_>>();
    if !missing.is_empty() {
        return Err(AppError::Config(format!(
            "boards[{index}] missing required fields: {}",
            missing.join(", ")
        )));
    }
    Ok(BoardConfig {
        id: raw.id.unwrap(),
        todo: raw.todo.unwrap(),
        working: raw.working.unwrap(),
        blocked: raw.blocked.unwrap(),
        done: raw.done.unwrap(),
    })
}

/// Validate and convert one raw roster entry.
fn roster_from_raw(raw: RawRosterEntry, index: usize) -> Result<RosterEntry> {
    let name = raw.name.unwrap_or_default();
    if name.is_empty() {
        return Err(AppError::Config(format!(
            "roster[{index}] missing required field: name"
        )));
    }
    Ok(RosterEntry {
        name,
        description: raw.description.unwrap_or_default().trim().to_string(),
    })
}

/// Expand `$NAME` and `${NAME}` environment variables inside raw YAML text.
fn expand_env(input: &str) -> String {
    let mut output = String::with_capacity(input.len());
    let mut chars = input.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch != '$' {
            output.push(ch);
            continue;
        }
        let mut name = String::new();
        if chars.peek() == Some(&'{') {
            chars.next();
            while let Some(&next) = chars.peek() {
                chars.next();
                if next == '}' {
                    break;
                }
                name.push(next);
            }
        } else {
            while let Some(&next) = chars.peek() {
                if next != '_' && !next.is_ascii_alphanumeric() {
                    break;
                }
                chars.next();
                name.push(next);
            }
        }
        if name.is_empty() {
            output.push('$');
        } else {
            output.push_str(&env::var(name).unwrap_or_default());
        }
    }
    output
}

/// Expand a leading shell-style tilde using the current user's home directory.
fn shellexpand_tilde(path: &str) -> std::path::PathBuf {
    if (path == "~" || path.starts_with("~/"))
        && let Some(home) = env::var_os("HOME")
    {
        return std::path::PathBuf::from(home).join(path.trim_start_matches("~/"));
    }
    std::path::PathBuf::from(path)
}

#[cfg(test)]
mod tests {
    //! Unit tests for configuration loading and path resolution.

    use std::fs;

    use super::*;

    /// Config loading validates YAML while preserving relative agent paths.
    #[test]
    fn loads_config_with_env_overrides_and_relative_pwd() {
        let temp = tempfile::tempdir().unwrap();
        let config = temp.path().join("config.yml");
        fs::write(
            &config,
            r#"
server:
  user: admin
  token: admin
  url: http://localhost:8080
worker:
  max_concurrency: 2
  poll_interval: 5
agent:
  name: codex
  command: "codex-acp --debug"
  pwd: .
  system_prompt: extra
boards:
  - id: 1
    todo: Ready
    working: In Progress
    blocked: Blocked
    done: Done
roster:
  - name: claude
    description: General agent
"#,
        )
        .unwrap();

        let loaded = load_config(&config).unwrap();

        assert_eq!(loaded.server.user, "admin");
        assert_eq!(loaded.worker.max_concurrency, 2);
        assert_eq!(loaded.agent.command, ["codex-acp", "--debug"]);
        assert_eq!(
            loaded.agent.pwd,
            temp.path().canonicalize().unwrap().to_string_lossy()
        );
        assert_eq!(loaded.boards[0].todo, "Ready");
        assert_eq!(loaded.roster[0].name, "claude");
    }
}
