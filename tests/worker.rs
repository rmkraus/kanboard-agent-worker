//! Integration tests for worker task claiming behavior.

use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

use async_trait::async_trait;
use kanboard_agent_worker::{
    Result,
    config::{AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings},
    kanboard::KanboardApi,
    worker::{WORK_STARTED_COMMENT, Worker},
};
use serde_json::{Value, json};

/// Captured `move_task_to_column` call arguments used by assertions.
type MoveCall = (Value, Value, Value, Value);

/// In-memory Kanboard API fake for worker integration tests.
#[derive(Debug, Clone, Default)]
struct FakeClient {
    /// Board payload returned by `get_board`.
    board: Vec<Value>,
    /// Task links keyed by task id.
    links: Arc<Mutex<BTreeMap<String, Vec<Value>>>>,
    /// Subtasks keyed by parent task id.
    subtasks: Arc<Mutex<BTreeMap<String, Vec<Value>>>>,
    /// Captured card moves.
    moves: Arc<Mutex<Vec<MoveCall>>>,
    /// Captured comments.
    comments: Arc<Mutex<Vec<(Value, Value, String)>>>,
}

#[async_trait]
impl KanboardApi for FakeClient {
    /// Return the fake authenticated worker user.
    async fn get_me(&self) -> Result<Value> {
        Ok(json!({"id": 9, "username": "codex-node1"}))
    }
    /// Return the configured fake board payload.
    async fn get_board(&self, _project_id: &Value) -> Result<Vec<Value>> {
        Ok(self.board.clone())
    }
    /// Return the standard test column mapping.
    async fn get_columns(&self, _project_id: &Value) -> Result<Vec<Value>> {
        Ok(vec![
            json!({"id": 1, "title": "Ready"}),
            json!({"id": 2, "title": "In Progress"}),
            json!({"id": 3, "title": "Done"}),
            json!({"id": 4, "title": "Blocked"}),
        ])
    }
    /// Return a minimal task payload for a requested id.
    async fn get_task(&self, task_id: &Value) -> Result<Value> {
        Ok(json!({"id": task_id, "assignee_username": "codex-node1", "swimlane_id": 8}))
    }
    /// Return a fake user record for any username.
    async fn get_user_by_name(&self, username: &str) -> Result<Value> {
        Ok(json!({"id": 11, "username": username}))
    }
    /// Return no comments for fake tasks.
    async fn get_all_comments(&self, _task_id: &Value) -> Result<Vec<Value>> {
        Ok(Vec::new())
    }
    /// Capture a fake comment creation call.
    async fn create_comment(&self, task_id: &Value, user_id: &Value, content: &str) -> Result<i64> {
        self.comments
            .lock()
            .unwrap()
            .push((task_id.clone(), user_id.clone(), content.to_string()));
        Ok(1)
    }
    /// Return empty fake task metadata.
    async fn get_task_metadata(&self, _task_id: &Value) -> Result<serde_json::Map<String, Value>> {
        Ok(serde_json::Map::new())
    }
    /// Accept fake metadata writes.
    async fn save_task_metadata(
        &self,
        _task_id: &Value,
        _values: serde_json::Map<String, Value>,
    ) -> Result<()> {
        Ok(())
    }
    /// Return fake subtasks keyed by parent task id.
    async fn get_all_subtasks(&self, task_id: &Value) -> Result<Vec<Value>> {
        Ok(self
            .subtasks
            .lock()
            .unwrap()
            .get(&task_id.to_string().replace('"', ""))
            .cloned()
            .unwrap_or_default())
    }
    /// Return fake task links keyed by task id.
    async fn get_all_task_links(&self, task_id: &Value) -> Result<Vec<Value>> {
        Ok(self
            .links
            .lock()
            .unwrap()
            .get(&task_id.to_string().replace('"', ""))
            .cloned()
            .unwrap_or_default())
    }
    /// Return no fake task files.
    async fn get_all_task_files(&self, _task_id: &Value) -> Result<Vec<Value>> {
        Ok(Vec::new())
    }
    /// Return empty fake attachment bytes.
    async fn download_task_file(&self, _file_id: &Value) -> Result<Vec<u8>> {
        Ok(Vec::new())
    }
    /// Accept fake file uploads and return a stable id.
    async fn create_task_file(
        &self,
        _project_id: &Value,
        _task_id: &Value,
        _filename: &str,
        _content: &[u8],
    ) -> Result<i64> {
        Ok(1)
    }
    /// Accept fake file deletion.
    async fn remove_task_file(&self, _file_id: &Value) -> Result<()> {
        Ok(())
    }
    /// Accept fake subtask creation and return a stable id.
    async fn create_subtask(
        &self,
        _task_id: &Value,
        _title: &str,
        _user_id: &Value,
        _status: i64,
    ) -> Result<i64> {
        Ok(1)
    }
    /// Accept fake subtask updates.
    async fn update_subtask(
        &self,
        _subtask_id: &Value,
        _task_id: &Value,
        _title: Option<&str>,
        _user_id: Option<&Value>,
        _status: Option<i64>,
    ) -> Result<()> {
        Ok(())
    }
    /// Report no running fake subtask timers.
    async fn has_subtask_timer(&self, _subtask_id: &Value, _user_id: &Value) -> Result<bool> {
        Ok(false)
    }
    /// Accept fake timer starts.
    async fn start_subtask_timer(&self, _subtask_id: &Value, _user_id: &Value) -> Result<()> {
        Ok(())
    }
    /// Accept fake timer stops.
    async fn stop_subtask_timer(&self, _subtask_id: &Value, _user_id: &Value) -> Result<()> {
        Ok(())
    }
    /// Capture a fake task move call.
    async fn move_task_to_column(
        &self,
        project_id: &Value,
        task_id: &Value,
        column_id: &Value,
        swimlane_id: &Value,
        _position: i64,
    ) -> Result<()> {
        self.moves.lock().unwrap().push((
            project_id.clone(),
            task_id.clone(),
            column_id.clone(),
            swimlane_id.clone(),
        ));
        Ok(())
    }
}

/// Ready tasks blocked by active internal links are skipped during claiming.
#[tokio::test]
async fn skips_ready_tasks_blocked_by_active_internal_links() {
    let mut links = BTreeMap::new();
    links.insert(
        "42".to_string(),
        vec![json!({"task_id": "41", "label": "is blocked by", "column_title": "In Progress"})],
    );
    let client = FakeClient {
        board: vec![json!({
            "columns": [
                {"id": 1, "tasks": [
                    {"id": "42", "assignee_username": "codex-node1", "swimlane_id": 8},
                    {"id": "43", "assignee_username": "codex-node1", "swimlane_id": 8}
                ]},
                {"id": 2, "tasks": []}
            ]
        })],
        links: Arc::new(Mutex::new(links)),
        ..Default::default()
    };
    let worker = Worker::new(config(), client.clone(), json!(9));

    let claimed = worker.claim_next_available().await.unwrap().unwrap();

    assert_eq!(claimed.task["id"], "43");
    assert_eq!(
        client.moves.lock().unwrap().as_slice(),
        &[(json!(1), json!("43"), json!(2), json!(8))]
    );
    assert_eq!(
        client.comments.lock().unwrap().as_slice(),
        &[(json!("43"), json!(9), WORK_STARTED_COMMENT.to_string())]
    );
}

/// Ready tasks whose blockers are done can still be claimed.
#[tokio::test]
async fn claims_ready_tasks_when_blocker_is_done() {
    let mut links = BTreeMap::new();
    links.insert(
        "42".to_string(),
        vec![json!({"task_id": "41", "label": "is blocked by", "column_title": "Done"})],
    );
    let client = FakeClient {
        board: vec![
            json!({"columns": [{"id": 1, "tasks": [{"id": "42", "assignee_username": "codex-node1", "swimlane_id": 8}]}]}),
        ],
        links: Arc::new(Mutex::new(links)),
        ..Default::default()
    };
    let worker = Worker::new(config(), client.clone(), json!(9));

    let claimed = worker.claim_next_available().await.unwrap().unwrap();

    assert_eq!(claimed.task["id"], "42");
    assert_eq!(
        client.moves.lock().unwrap().as_slice(),
        &[(json!(1), json!("42"), json!(2), json!(8))]
    );
}

/// Build the standard test worker configuration.
fn config() -> AppConfig {
    AppConfig {
        server: ServerConfig {
            user: "codex-node1".to_string(),
            token: "secret".to_string(),
            url: "http://localhost:8080".to_string(),
        },
        worker: WorkerSettings {
            max_concurrency: 1,
            poll_interval: 10,
        },
        agent: AgentConfig {
            name: "codex".to_string(),
            command: vec!["codex-acp".to_string()],
            pwd: ".".to_string(),
            system_prompt: String::new(),
            timeout_seconds: 3600,
        },
        boards: vec![BoardConfig {
            id: json!(1),
            todo: "Ready".to_string(),
            working: "In Progress".to_string(),
            blocked: "Blocked".to_string(),
            done: "Done".to_string(),
        }],
        roster: Vec::new(),
    }
}
