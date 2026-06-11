//! Kanboard JSON-RPC client and value helpers.
//!
//! The client wraps Kanboard's JSON-RPC API, normalizes common result shapes,
//! retries transient SQLite lock responses, and exposes a trait so worker tests
//! can run against fakes.

use std::time::Duration;

use async_trait::async_trait;
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use tokio::time::sleep;

use crate::{AppError, Result};

/// Resolved Kanboard columns for one configured board.
#[derive(Debug, Clone)]
pub struct ColumnLookup {
    /// Ready queue column.
    pub todo: Value,
    /// In-progress column.
    pub working: Value,
    /// Human-blocked column.
    pub blocked: Value,
    /// Completed-work column.
    pub done: Value,
}

/// HTTP JSON-RPC client for Kanboard.
#[derive(Debug, Clone)]
pub struct KanboardClient {
    /// Normalized `/jsonrpc.php` endpoint.
    endpoint: String,
    /// Kanboard username for basic authentication.
    user: String,
    /// Kanboard API token or password for basic authentication.
    token: String,
    /// Reusable async HTTP client.
    client: reqwest::Client,
    /// Maximum attempts for calls that hit a locked SQLite database.
    retry_attempts: usize,
    /// Base retry delay multiplied by the attempt number.
    retry_delay: Duration,
}

impl KanboardClient {
    /// Create a new client for a Kanboard URL and credentials.
    pub fn new(url: impl Into<String>, user: impl Into<String>, token: impl Into<String>) -> Self {
        Self {
            endpoint: normalize_endpoint(&url.into()),
            user: user.into(),
            token: token.into(),
            client: reqwest::Client::new(),
            retry_attempts: 8,
            retry_delay: Duration::from_millis(250),
        }
    }

    /// Execute one Kanboard JSON-RPC method call.
    ///
    /// Database-lock responses are retried with a short linear backoff. Other
    /// HTTP or JSON-RPC errors are returned immediately.
    pub async fn call(&self, method: &str, params: Option<Value>) -> Result<Value> {
        for attempt in 1..=self.retry_attempts {
            let payload = json!({
                "jsonrpc": "2.0",
                "method": method,
                "id": attempt,
                "params": params.clone().unwrap_or(Value::Null),
            });
            let response = self
                .client
                .post(&self.endpoint)
                .basic_auth(&self.user, Some(&self.token))
                .json(&payload)
                .send()
                .await?;
            let status = response.status();
            let body = response.text().await?;
            if !status.is_success() {
                if is_database_locked_error(&body) && attempt < self.retry_attempts {
                    self.sleep_before_retry(attempt).await;
                    continue;
                }
                return Err(AppError::Kanboard(format!(
                    "Kanboard HTTP {status}: {body}"
                )));
            }
            let data: Value = serde_json::from_str(&body)?;
            if let Some(error) = data.get("error") {
                if is_database_locked_error(error) && attempt < self.retry_attempts {
                    self.sleep_before_retry(attempt).await;
                    continue;
                }
                return Err(AppError::Kanboard(format!(
                    "Kanboard JSON-RPC error in {method}: {error}"
                )));
            }
            return Ok(data.get("result").cloned().unwrap_or(Value::Null));
        }
        Err(AppError::Kanboard(format!(
            "Kanboard JSON-RPC retry attempts exhausted in {method}"
        )))
    }

    /// Sleep before retrying a transient database-lock response.
    async fn sleep_before_retry(&self, attempt: usize) {
        sleep(self.retry_delay * attempt as u32).await;
    }
}

/// Trait covering the Kanboard operations used by the worker and MCP tools.
///
/// Implementing this trait allows tests to provide an in-memory fake while
/// production uses [`KanboardClient`].
#[async_trait]
pub trait KanboardApi: Clone + Send + Sync + 'static {
    /// Return the authenticated Kanboard user.
    async fn get_me(&self) -> Result<Value>;
    /// Return the swimlane board structure for a project.
    async fn get_board(&self, project_id: &Value) -> Result<Vec<Value>>;
    /// Return all columns for a project.
    async fn get_columns(&self, project_id: &Value) -> Result<Vec<Value>>;
    /// Return one task by id.
    async fn get_task(&self, task_id: &Value) -> Result<Value>;
    /// Look up a Kanboard user by username.
    async fn get_user_by_name(&self, username: &str) -> Result<Value>;
    /// Return all comments on a task.
    async fn get_all_comments(&self, task_id: &Value) -> Result<Vec<Value>>;
    /// Create a comment on a task and return the comment id.
    async fn create_comment(&self, task_id: &Value, user_id: &Value, content: &str) -> Result<i64>;
    /// Return saved metadata for a task.
    async fn get_task_metadata(&self, task_id: &Value) -> Result<serde_json::Map<String, Value>>;
    /// Persist metadata key-value pairs on a task.
    async fn save_task_metadata(
        &self,
        task_id: &Value,
        values: serde_json::Map<String, Value>,
    ) -> Result<()>;
    /// Return all subtasks for a task.
    async fn get_all_subtasks(&self, task_id: &Value) -> Result<Vec<Value>>;
    /// Return all internal task links for a task.
    async fn get_all_task_links(&self, task_id: &Value) -> Result<Vec<Value>>;
    /// Return all files attached to a task.
    async fn get_all_task_files(&self, task_id: &Value) -> Result<Vec<Value>>;
    /// Download a task file and return raw bytes.
    async fn download_task_file(&self, file_id: &Value) -> Result<Vec<u8>>;
    /// Upload a task file and return the new file id.
    async fn create_task_file(
        &self,
        project_id: &Value,
        task_id: &Value,
        filename: &str,
        content: &[u8],
    ) -> Result<i64>;
    /// Remove a task file by id.
    async fn remove_task_file(&self, file_id: &Value) -> Result<()>;
    /// Create a subtask and return the new subtask id.
    async fn create_subtask(
        &self,
        task_id: &Value,
        title: &str,
        user_id: &Value,
        status: i64,
    ) -> Result<i64>;
    /// Update selected subtask fields.
    async fn update_subtask(
        &self,
        subtask_id: &Value,
        task_id: &Value,
        title: Option<&str>,
        user_id: Option<&Value>,
        status: Option<i64>,
    ) -> Result<()>;
    /// Return whether a subtask timer is currently running for a user.
    async fn has_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<bool>;
    /// Start a subtask timer for a user.
    async fn start_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<()>;
    /// Stop a subtask timer for a user.
    async fn stop_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<()>;
    /// Move a task to a board column and position.
    async fn move_task_to_column(
        &self,
        project_id: &Value,
        task_id: &Value,
        column_id: &Value,
        swimlane_id: &Value,
        position: i64,
    ) -> Result<()>;
}

#[async_trait]
impl KanboardApi for KanboardClient {
    /// Return the authenticated Kanboard user.
    async fn get_me(&self) -> Result<Value> {
        truthy(
            self.call("getMe", None).await?,
            "getMe returned no user data",
        )
    }

    /// Return the swimlane board structure for a project.
    async fn get_board(&self, project_id: &Value) -> Result<Vec<Value>> {
        array_result(
            self.call("getBoard", Some(json!([coerce_id(project_id)])))
                .await?,
            "getBoard",
        )
    }

    /// Return all columns for a project.
    async fn get_columns(&self, project_id: &Value) -> Result<Vec<Value>> {
        array_result(
            self.call("getColumns", Some(json!([coerce_id(project_id)])))
                .await?,
            "getColumns",
        )
    }

    /// Return one task by id.
    async fn get_task(&self, task_id: &Value) -> Result<Value> {
        truthy(
            self.call("getTask", Some(json!({"task_id": coerce_id(task_id)})))
                .await?,
            "getTask failed",
        )
    }

    /// Look up a Kanboard user by username.
    async fn get_user_by_name(&self, username: &str) -> Result<Value> {
        truthy(
            self.call("getUserByName", Some(json!({"username": username})))
                .await?,
            "getUserByName failed",
        )
    }

    /// Return all comments on a task.
    async fn get_all_comments(&self, task_id: &Value) -> Result<Vec<Value>> {
        array_result(
            self.call(
                "getAllComments",
                Some(json!({"task_id": coerce_id(task_id)})),
            )
            .await?,
            "getAllComments",
        )
    }

    /// Create a comment on a task and return the comment id.
    async fn create_comment(&self, task_id: &Value, user_id: &Value, content: &str) -> Result<i64> {
        int_result(
            self.call(
                "createComment",
                Some(json!({
                    "task_id": coerce_id(task_id),
                    "user_id": coerce_id(user_id),
                    "content": content,
                })),
            )
            .await?,
            "createComment",
        )
    }

    /// Return saved metadata for a task.
    async fn get_task_metadata(&self, task_id: &Value) -> Result<serde_json::Map<String, Value>> {
        let result = self
            .call("getTaskMetadata", Some(json!([coerce_id(task_id)])))
            .await?;
        Ok(match result {
            Value::Object(map) => map,
            Value::Array(items) => {
                let mut merged = serde_json::Map::new();
                for item in items {
                    if let Value::Object(map) = item {
                        merged.extend(map);
                    }
                }
                merged
            }
            _ => serde_json::Map::new(),
        })
    }

    /// Persist metadata key-value pairs on a task.
    async fn save_task_metadata(
        &self,
        task_id: &Value,
        values: serde_json::Map<String, Value>,
    ) -> Result<()> {
        true_result(
            self.call(
                "saveTaskMetadata",
                Some(json!([coerce_id(task_id), values])),
            )
            .await?,
            "saveTaskMetadata",
        )
    }

    /// Return all subtasks for a task.
    async fn get_all_subtasks(&self, task_id: &Value) -> Result<Vec<Value>> {
        array_result(
            self.call(
                "getAllSubtasks",
                Some(json!({"task_id": coerce_id(task_id)})),
            )
            .await?,
            "getAllSubtasks",
        )
    }

    /// Return all internal task links for a task.
    async fn get_all_task_links(&self, task_id: &Value) -> Result<Vec<Value>> {
        array_result(
            self.call("getAllTaskLinks", Some(json!([coerce_id(task_id)])))
                .await?,
            "getAllTaskLinks",
        )
    }

    /// Return all files attached to a task.
    async fn get_all_task_files(&self, task_id: &Value) -> Result<Vec<Value>> {
        array_result(
            self.call(
                "getAllTaskFiles",
                Some(json!({"task_id": coerce_id(task_id)})),
            )
            .await?,
            "getAllTaskFiles",
        )
    }

    /// Download a task file and decode the base64 payload returned by Kanboard.
    async fn download_task_file(&self, file_id: &Value) -> Result<Vec<u8>> {
        let result = self
            .call(
                "downloadTaskFile",
                Some(json!({"file_id": coerce_id(file_id)})),
            )
            .await?;
        let encoded = result.as_str().ok_or_else(|| {
            AppError::Kanboard("downloadTaskFile returned non-string content".to_string())
        })?;
        STANDARD.decode(encoded).map_err(|error| {
            AppError::Kanboard(format!("downloadTaskFile returned invalid base64: {error}"))
        })
    }

    /// Upload a task file after base64-encoding the bytes for Kanboard.
    async fn create_task_file(
        &self,
        project_id: &Value,
        task_id: &Value,
        filename: &str,
        content: &[u8],
    ) -> Result<i64> {
        int_result(
            self.call(
                "createTaskFile",
                Some(json!({
                    "project_id": coerce_id(project_id),
                    "task_id": coerce_id(task_id),
                    "filename": filename,
                    "blob": STANDARD.encode(content),
                })),
            )
            .await?,
            "createTaskFile",
        )
    }

    /// Remove a task file by id.
    async fn remove_task_file(&self, file_id: &Value) -> Result<()> {
        true_result(
            self.call(
                "removeTaskFile",
                Some(json!({"file_id": coerce_id(file_id)})),
            )
            .await?,
            "removeTaskFile",
        )
    }

    /// Create a subtask and return the new subtask id.
    async fn create_subtask(
        &self,
        task_id: &Value,
        title: &str,
        user_id: &Value,
        status: i64,
    ) -> Result<i64> {
        int_result(
            self.call(
                "createSubtask",
                Some(json!({
                    "task_id": coerce_id(task_id),
                    "title": title,
                    "user_id": coerce_id(user_id),
                    "status": status,
                })),
            )
            .await?,
            "createSubtask",
        )
    }

    /// Update selected subtask fields.
    async fn update_subtask(
        &self,
        subtask_id: &Value,
        task_id: &Value,
        title: Option<&str>,
        user_id: Option<&Value>,
        status: Option<i64>,
    ) -> Result<()> {
        let mut params = serde_json::Map::from_iter([
            ("id".to_string(), coerce_id(subtask_id)),
            ("task_id".to_string(), coerce_id(task_id)),
        ]);
        if let Some(title) = title {
            params.insert("title".to_string(), Value::String(title.to_string()));
        }
        if let Some(user_id) = user_id {
            params.insert("user_id".to_string(), coerce_id(user_id));
        }
        if let Some(status) = status {
            params.insert("status".to_string(), Value::from(status));
        }
        true_result(
            self.call("updateSubtask", Some(Value::Object(params)))
                .await?,
            "updateSubtask",
        )
    }

    /// Return whether a subtask timer is currently running for a user.
    async fn has_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<bool> {
        Ok(self
            .call(
                "hasSubtaskTimer",
                Some(json!([coerce_id(subtask_id), coerce_id(user_id)])),
            )
            .await?
            .as_bool()
            .unwrap_or(false))
    }

    /// Start a subtask timer for a user.
    async fn start_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<()> {
        true_result(
            self.call(
                "setSubtaskStartTime",
                Some(json!([coerce_id(subtask_id), coerce_id(user_id)])),
            )
            .await?,
            "setSubtaskStartTime",
        )
    }

    /// Stop a subtask timer for a user.
    async fn stop_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<()> {
        true_result(
            self.call(
                "setSubtaskEndTime",
                Some(json!([coerce_id(subtask_id), coerce_id(user_id)])),
            )
            .await?,
            "setSubtaskEndTime",
        )
    }

    /// Move a task to a board column and position.
    async fn move_task_to_column(
        &self,
        project_id: &Value,
        task_id: &Value,
        column_id: &Value,
        swimlane_id: &Value,
        position: i64,
    ) -> Result<()> {
        true_result(
            self.call(
                "moveTaskPosition",
                Some(json!({
                    "project_id": coerce_id(project_id),
                    "task_id": coerce_id(task_id),
                    "column_id": coerce_id(column_id),
                    "position": position,
                    "swimlane_id": coerce_id(swimlane_id),
                })),
            )
            .await?,
            "moveTaskPosition",
        )
    }
}

/// Normalize a Kanboard base URL into a JSON-RPC endpoint URL.
pub fn normalize_endpoint(url: &str) -> String {
    let clean = url.trim_end_matches('/');
    if clean.ends_with("/jsonrpc.php") {
        clean.to_string()
    } else {
        format!("{clean}/jsonrpc.php")
    }
}

/// Resolve configured logical column names against Kanboard column payloads.
pub fn column_lookup(columns: &[Value], names: &[(&str, &str)]) -> Result<ColumnLookup> {
    let by_title = columns
        .iter()
        .filter_map(|column| Some((column.get("title")?.as_str()?.to_string(), column.clone())))
        .collect::<std::collections::BTreeMap<_, _>>();
    let mut resolved = serde_json::Map::new();
    let mut missing = Vec::new();
    for (label, title) in names {
        if let Some(column) = by_title.get(*title) {
            resolved.insert((*label).to_string(), column.clone());
        } else {
            missing.push(format!("{label}={title:?}"));
        }
    }
    if !missing.is_empty() {
        let available = by_title.keys().cloned().collect::<Vec<_>>().join(", ");
        return Err(AppError::Kanboard(format!(
            "Missing configured columns: {}. Available columns: {available}",
            missing.join(", ")
        )));
    }
    Ok(ColumnLookup {
        todo: resolved.remove("todo").unwrap(),
        working: resolved.remove("working").unwrap(),
        blocked: resolved.remove("blocked").unwrap(),
        done: resolved.remove("done").unwrap(),
    })
}

/// Convert stringified numeric ids into JSON numbers when possible.
pub fn coerce_id(value: &Value) -> Value {
    match value {
        Value::String(text) => text
            .parse::<i64>()
            .map(Value::from)
            .unwrap_or_else(|_| value.clone()),
        _ => value.clone(),
    }
}

/// Return the `id` field from a JSON object, or `null` when absent.
pub fn value_id(value: &Value) -> Value {
    value.get("id").cloned().unwrap_or(Value::Null)
}

/// Return a string field from a JSON object, or an empty string when absent.
pub fn value_str<'a>(value: &'a Value, key: &str) -> &'a str {
    value.get(key).and_then(Value::as_str).unwrap_or("")
}

/// Return an integer field from a JSON object, accepting strings and numbers.
pub fn value_i64(value: &Value, key: &str) -> i64 {
    value.get(key).and_then(Value::as_i64).unwrap_or_else(|| {
        value
            .get(key)
            .and_then(Value::as_str)
            .and_then(|value| value.parse().ok())
            .unwrap_or(0)
    })
}

/// Detect Kanboard SQLite lock errors in HTTP bodies or JSON-RPC errors.
pub fn is_database_locked_error(value: impl serde::Serialize) -> bool {
    serde_json::to_string(&value)
        .unwrap_or_default()
        .to_ascii_lowercase()
        .contains("database is locked")
}

/// Treat `null` and `false` Kanboard results as method failures.
fn truthy(value: Value, message: &str) -> Result<Value> {
    if value.is_null() || value == Value::Bool(false) {
        Err(AppError::Kanboard(message.to_string()))
    } else {
        Ok(value)
    }
}

/// Convert a Kanboard result into a JSON array or a method-specific error.
fn array_result(value: Value, method: &str) -> Result<Vec<Value>> {
    match value {
        Value::Array(items) => Ok(items),
        Value::Bool(false) | Value::Null => Err(AppError::Kanboard(format!("{method} failed"))),
        other => Err(AppError::Kanboard(format!(
            "{method} returned non-list result: {other}"
        ))),
    }
}

/// Convert a Kanboard result into an integer id.
fn int_result(value: Value, method: &str) -> Result<i64> {
    value
        .as_i64()
        .or_else(|| value.as_str().and_then(|value| value.parse().ok()))
        .ok_or_else(|| AppError::Kanboard(format!("{method} failed")))
}

/// Require an exact `true` Kanboard result for mutating operations.
fn true_result(value: Value, method: &str) -> Result<()> {
    if value == Value::Bool(true) {
        Ok(())
    } else {
        Err(AppError::Kanboard(format!("{method} failed")))
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for Kanboard value helpers.

    use serde_json::json;

    use super::*;

    /// Endpoint normalization appends `/jsonrpc.php` exactly once.
    #[test]
    fn endpoint_normalization_appends_jsonrpc() {
        assert_eq!(
            normalize_endpoint("http://localhost:8080"),
            "http://localhost:8080/jsonrpc.php"
        );
        assert_eq!(
            normalize_endpoint("http://localhost:8080/jsonrpc.php"),
            "http://localhost:8080/jsonrpc.php"
        );
    }

    /// Column lookup maps logical board roles to configured Kanboard columns.
    #[test]
    fn column_lookup_returns_configured_columns() {
        let lookup = column_lookup(
            &[
                json!({"id": "1", "title": "Intake"}),
                json!({"id": "2", "title": "In Process"}),
                json!({"id": "3", "title": "Escalate"}),
                json!({"id": "4", "title": "Complete"}),
            ],
            &[
                ("todo", "Intake"),
                ("working", "In Process"),
                ("blocked", "Escalate"),
                ("done", "Complete"),
            ],
        )
        .unwrap();

        assert_eq!(lookup.todo["id"], "1");
        assert_eq!(lookup.working["id"], "2");
        assert_eq!(lookup.blocked["id"], "3");
        assert_eq!(lookup.done["id"], "4");
    }
}
