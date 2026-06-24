//! Worker lifecycle and Kanboard task orchestration.
//!
//! A worker polls configured Kanboard boards, claims assigned work, runs an ACP
//! agent, and routes the card or subtask based on the agent result.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

use serde_json::{Value, json};
use tokio::{
    sync::Semaphore,
    time::{Duration, sleep},
};
use tracing::{error, info};

use crate::{
    Result,
    acp::AcpSession,
    config::{AppConfig, BoardConfig},
    kanboard::{
        ColumnLookup, KanboardApi, KanboardClient, coerce_id, column_lookup, value_i64, value_id,
        value_str,
    },
    prompt::build_agent_prompt,
    smartsheet::SmartsheetClient,
};

/// Comment posted when the worker claims a top-level task.
pub const WORK_STARTED_COMMENT: &str = "Started working on this task.";
/// Comment template posted when the worker claims an assigned subtask.
pub const SUBTASK_WORK_STARTED_COMMENT: &str = "Started working on subtask #{subtask_id}: {title}";
/// Comment posted when startup recovery returns abandoned work to the queue.
pub const RECOVERY_COMMENT: &str = "Sorry, I fell asleep on the job. I'll get back to this.";
/// Kanboard's built-in internal task-link label for dependency edges.
pub const BLOCKED_BY_LINK_LABEL: &str = "is blocked by";
/// Kanboard numeric status for todo subtasks.
pub const SUBTASK_STATUS_TODO: i64 = 0;
/// Kanboard numeric status for in-progress subtasks.
pub const SUBTASK_STATUS_IN_PROGRESS: i64 = 1;
/// Kanboard numeric status for completed subtasks.
pub const SUBTASK_STATUS_DONE: i64 = 2;

/// A unit of work that has been claimed and is ready for agent execution.
///
/// Top-level tasks have `subtask: None`. Subtask claims keep the parent task in
/// `task` and the specific subtask record in `subtask`.
#[derive(Debug, Clone, PartialEq)]
pub struct ClaimedTask {
    /// Board configuration the task came from.
    pub board: BoardConfig,
    /// Kanboard task record, usually refreshed after claiming.
    pub task: Value,
    /// Configured ready/todo column id for routing unfinished parent work.
    pub todo_column_id: Value,
    /// Configured done column id for successful top-level work.
    pub done_column_id: Value,
    /// Configured blocked column id for failed or blocked top-level work.
    pub blocked_column_id: Value,
    /// Assigned subtask being worked, if this claim is subtask-level.
    pub subtask: Option<Value>,
}

/// Polling worker parameterized by a Kanboard API implementation.
///
/// Production uses `Worker<KanboardClient>`. Tests can use `Worker<FakeClient>`
/// as long as the fake implements [`KanboardApi`].
#[derive(Debug, Clone)]
pub struct Worker<C> {
    /// Validated application configuration.
    pub config: AppConfig,
    /// Kanboard API implementation used by the worker.
    pub client: C,
    /// Authenticated Kanboard user id for comments and subtask assignment.
    pub user_id: Value,
}

#[derive(Debug, Clone)]
pub enum BoardClient<C> {
    /// Kanboard API implementation.
    Kanboard(C),
    /// Smartsheet adapter implementation.
    Smartsheet(SmartsheetClient),
    /// Mixed Kanboard projects and Smartsheet sheets in one worker.
    Hybrid {
        /// Kanboard API implementation.
        kanboard: C,
        /// Smartsheet adapter implementation.
        smartsheet: SmartsheetClient,
        /// Configured boards used for per-board routing.
        boards: Vec<BoardConfig>,
    },
}

impl<C> BoardClient<C> {
    /// Return true when a project id belongs to a configured Smartsheet board.
    fn is_smartsheet_project(&self, project_id: &Value) -> bool {
        let wanted = project_id.to_string().trim_matches('"').to_string();
        match self {
            Self::Smartsheet(_) => true,
            Self::Hybrid { boards, .. } => boards.iter().any(|board| {
                board.is_smartsheet() && board.id.to_string().trim_matches('"') == wanted
            }),
            Self::Kanboard(_) => false,
        }
    }

    /// Return true when a task id is an encoded Smartsheet `sheet_id:row_id` task id.
    fn is_smartsheet_task(&self, task_id: &Value) -> bool {
        let text = task_id.to_string().trim_matches('"').to_string();
        text.split_once(':')
            .is_some_and(|(sheet_id, _)| self.is_smartsheet_project(&json!(sheet_id)))
    }
}

#[async_trait::async_trait]
impl<C: KanboardApi> KanboardApi for BoardClient<C> {
    async fn get_me(&self) -> Result<Value> {
        match self {
            Self::Kanboard(client) => client.get_me().await,
            Self::Smartsheet(client) => client.get_me().await,
            Self::Hybrid { smartsheet, .. } => smartsheet.get_me().await,
        }
    }

    async fn get_board(&self, project_id: &Value) -> Result<Vec<Value>> {
        match self {
            Self::Kanboard(client) => client.get_board(project_id).await,
            Self::Smartsheet(client) => client.get_board(project_id).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_project(project_id) {
                    smartsheet.get_board(project_id).await
                } else {
                    kanboard.get_board(project_id).await
                }
            }
        }
    }

    async fn get_columns(&self, project_id: &Value) -> Result<Vec<Value>> {
        match self {
            Self::Kanboard(client) => client.get_columns(project_id).await,
            Self::Smartsheet(client) => client.get_columns(project_id).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_project(project_id) {
                    smartsheet.get_columns(project_id).await
                } else {
                    kanboard.get_columns(project_id).await
                }
            }
        }
    }

    async fn get_task(&self, task_id: &Value) -> Result<Value> {
        match self {
            Self::Kanboard(client) => client.get_task(task_id).await,
            Self::Smartsheet(client) => client.get_task(task_id).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_task(task_id) {
                    smartsheet.get_task(task_id).await
                } else {
                    kanboard.get_task(task_id).await
                }
            }
        }
    }

    async fn get_user_by_name(&self, username: &str) -> Result<Value> {
        match self {
            Self::Kanboard(client) => client.get_user_by_name(username).await,
            Self::Smartsheet(_) => {
                Ok(json!({"id": username, "username": username, "email": username}))
            }
            Self::Hybrid { kanboard, .. } => kanboard.get_user_by_name(username).await,
        }
    }

    async fn get_all_comments(&self, task_id: &Value) -> Result<Vec<Value>> {
        match self {
            Self::Kanboard(client) => client.get_all_comments(task_id).await,
            Self::Smartsheet(client) => client.get_all_comments(task_id).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_task(task_id) {
                    smartsheet.get_all_comments(task_id).await
                } else {
                    kanboard.get_all_comments(task_id).await
                }
            }
        }
    }

    async fn create_comment(&self, task_id: &Value, user_id: &Value, content: &str) -> Result<i64> {
        match self {
            Self::Kanboard(client) => client.create_comment(task_id, user_id, content).await,
            Self::Smartsheet(client) => client.create_comment(task_id, content).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_task(task_id) {
                    smartsheet.create_comment(task_id, content).await
                } else {
                    kanboard.create_comment(task_id, user_id, content).await
                }
            }
        }
    }

    async fn get_task_metadata(&self, task_id: &Value) -> Result<serde_json::Map<String, Value>> {
        match self {
            Self::Kanboard(client) => client.get_task_metadata(task_id).await,
            Self::Smartsheet(client) => client.get_task_metadata(task_id).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_task(task_id) {
                    smartsheet.get_task_metadata(task_id).await
                } else {
                    kanboard.get_task_metadata(task_id).await
                }
            }
        }
    }

    async fn save_task_metadata(
        &self,
        task_id: &Value,
        values: serde_json::Map<String, Value>,
    ) -> Result<()> {
        match self {
            Self::Kanboard(client) => client.save_task_metadata(task_id, values).await,
            Self::Smartsheet(client) => client.save_task_metadata(task_id, values).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_task(task_id) {
                    smartsheet.save_task_metadata(task_id, values).await
                } else {
                    kanboard.save_task_metadata(task_id, values).await
                }
            }
        }
    }

    async fn get_all_subtasks(&self, task_id: &Value) -> Result<Vec<Value>> {
        match self {
            Self::Kanboard(client) => client.get_all_subtasks(task_id).await,
            Self::Smartsheet(_) => Ok(Vec::new()),
            Self::Hybrid { kanboard, .. } => {
                if self.is_smartsheet_task(task_id) {
                    Ok(Vec::new())
                } else {
                    kanboard.get_all_subtasks(task_id).await
                }
            }
        }
    }

    async fn get_all_task_links(&self, task_id: &Value) -> Result<Vec<Value>> {
        match self {
            Self::Kanboard(client) => client.get_all_task_links(task_id).await,
            Self::Smartsheet(_) => Ok(Vec::new()),
            Self::Hybrid { kanboard, .. } => {
                if self.is_smartsheet_task(task_id) {
                    Ok(Vec::new())
                } else {
                    kanboard.get_all_task_links(task_id).await
                }
            }
        }
    }

    async fn get_all_task_files(&self, task_id: &Value) -> Result<Vec<Value>> {
        match self {
            Self::Kanboard(client) => client.get_all_task_files(task_id).await,
            Self::Smartsheet(client) => client.get_all_task_files(task_id).await,
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_task(task_id) {
                    smartsheet.get_all_task_files(task_id).await
                } else {
                    kanboard.get_all_task_files(task_id).await
                }
            }
        }
    }

    async fn download_task_file(&self, file_id: &Value) -> Result<Vec<u8>> {
        match self {
            Self::Kanboard(client) => client.download_task_file(file_id).await,
            Self::Smartsheet(client) => client.download_task_file(file_id, None).await,
            Self::Hybrid { smartsheet, .. } => smartsheet.download_task_file(file_id, None).await,
        }
    }

    async fn create_task_file(
        &self,
        project_id: &Value,
        task_id: &Value,
        filename: &str,
        content: &[u8],
    ) -> Result<i64> {
        match self {
            Self::Kanboard(client) => {
                client
                    .create_task_file(project_id, task_id, filename, content)
                    .await
            }
            Self::Smartsheet(client) => {
                client
                    .create_task_file(project_id, task_id, filename, content)
                    .await
            }
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_project(project_id) {
                    smartsheet
                        .create_task_file(project_id, task_id, filename, content)
                        .await
                } else {
                    kanboard
                        .create_task_file(project_id, task_id, filename, content)
                        .await
                }
            }
        }
    }

    async fn remove_task_file(&self, file_id: &Value) -> Result<()> {
        match self {
            Self::Kanboard(client) => client.remove_task_file(file_id).await,
            Self::Smartsheet(client) => client.remove_task_file(file_id).await,
            Self::Hybrid { smartsheet, .. } => smartsheet.remove_task_file(file_id).await,
        }
    }

    async fn create_subtask(
        &self,
        task_id: &Value,
        title: &str,
        user_id: &Value,
        status: i64,
    ) -> Result<i64> {
        match self {
            Self::Kanboard(client) => client.create_subtask(task_id, title, user_id, status).await,
            Self::Smartsheet(_) => Err(crate::AppError::Kanboard(
                "Smartsheet boards do not support Kanboard subtasks".to_string(),
            )),
            Self::Hybrid { kanboard, .. } => {
                if self.is_smartsheet_task(task_id) {
                    Err(crate::AppError::Kanboard(
                        "Smartsheet boards do not support Kanboard subtasks".to_string(),
                    ))
                } else {
                    kanboard
                        .create_subtask(task_id, title, user_id, status)
                        .await
                }
            }
        }
    }

    async fn update_subtask(
        &self,
        subtask_id: &Value,
        task_id: &Value,
        title: Option<&str>,
        user_id: Option<&Value>,
        status: Option<i64>,
    ) -> Result<()> {
        match self {
            Self::Kanboard(client) => {
                client
                    .update_subtask(subtask_id, task_id, title, user_id, status)
                    .await
            }
            Self::Smartsheet(_) => Ok(()),
            Self::Hybrid { kanboard, .. } => {
                if self.is_smartsheet_task(task_id) {
                    Ok(())
                } else {
                    kanboard
                        .update_subtask(subtask_id, task_id, title, user_id, status)
                        .await
                }
            }
        }
    }

    async fn has_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<bool> {
        match self {
            Self::Kanboard(client) => client.has_subtask_timer(subtask_id, user_id).await,
            Self::Smartsheet(_) => Ok(false),
            Self::Hybrid { kanboard, .. } => kanboard.has_subtask_timer(subtask_id, user_id).await,
        }
    }

    async fn start_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<()> {
        match self {
            Self::Kanboard(client) => client.start_subtask_timer(subtask_id, user_id).await,
            Self::Smartsheet(_) => Ok(()),
            Self::Hybrid { kanboard, .. } => {
                kanboard.start_subtask_timer(subtask_id, user_id).await
            }
        }
    }

    async fn stop_subtask_timer(&self, subtask_id: &Value, user_id: &Value) -> Result<()> {
        match self {
            Self::Kanboard(client) => client.stop_subtask_timer(subtask_id, user_id).await,
            Self::Smartsheet(_) => Ok(()),
            Self::Hybrid { kanboard, .. } => kanboard.stop_subtask_timer(subtask_id, user_id).await,
        }
    }

    async fn move_task_to_column(
        &self,
        project_id: &Value,
        task_id: &Value,
        column_id: &Value,
        swimlane_id: &Value,
        position: i64,
    ) -> Result<()> {
        match self {
            Self::Kanboard(client) => {
                client
                    .move_task_to_column(project_id, task_id, column_id, swimlane_id, position)
                    .await
            }
            Self::Smartsheet(client) => {
                client
                    .move_task_to_column(project_id, task_id, column_id)
                    .await
            }
            Self::Hybrid {
                kanboard,
                smartsheet,
                ..
            } => {
                if self.is_smartsheet_project(project_id) {
                    smartsheet
                        .move_task_to_column(project_id, task_id, column_id)
                        .await
                } else {
                    kanboard
                        .move_task_to_column(project_id, task_id, column_id, swimlane_id, position)
                        .await
                }
            }
        }
    }
}

impl Worker<BoardClient<KanboardClient>> {
    /// Build a production worker that can route configured boards to Kanboard or Smartsheet.
    pub async fn from_config(config: AppConfig) -> Result<Self> {
        let has_smartsheet = config.boards.iter().any(BoardConfig::is_smartsheet);
        let has_kanboard = config.boards.iter().any(|board| !board.is_smartsheet());
        let kanboard = || {
            KanboardClient::new(
                &config.server.url,
                &config.server.user,
                &config.server.token,
            )
        };
        let smartsheet = || {
            SmartsheetClient::new(
                config.smartsheet.as_ref(),
                &config.server.user,
                config.boards.clone(),
            )
        };
        let client = match (has_kanboard, has_smartsheet) {
            (true, true) => BoardClient::Hybrid {
                kanboard: kanboard(),
                smartsheet: smartsheet(),
                boards: config.boards.clone(),
            },
            (false, true) => BoardClient::Smartsheet(smartsheet()),
            _ => BoardClient::Kanboard(kanboard()),
        };
        let user = client.get_me().await?;
        Ok(Self {
            config,
            client,
            user_id: value_id(&user),
        })
    }
}

impl<C: KanboardApi> Worker<C> {
    /// Build a worker from an already-created Kanboard API implementation.
    ///
    /// This is the dependency-injection constructor used by tests and any
    /// caller that wants to provide a custom client.
    pub fn new(config: AppConfig, client: C, user_id: Value) -> Self {
        Self {
            config,
            client,
            user_id,
        }
    }

    /// Validate credentials, configured board columns, and roster users.
    pub async fn check(&self) -> Result<Vec<String>> {
        let user = self.client.get_me().await?;
        let mut lines = vec![format!(
            "Authenticated as {} (id={})",
            value_str(&user, "username"),
            user.get("id").unwrap_or(&Value::Null)
        )];
        for board in &self.config.boards {
            if board.is_smartsheet() {
                self.lookup_columns(board).await?;
                lines.push(format!(
                    "Smartsheet sheet {}: found configured columns",
                    board.id
                ));
                continue;
            }
            self.lookup_columns(board).await?;
            lines.push(format!("Board {}: found configured columns", board.id));
        }
        for entry in &self.config.roster {
            let user = self.client.get_user_by_name(&entry.name).await?;
            lines.push(format!(
                "Roster {}: Kanboard user id={}",
                entry.name,
                user.get("id").unwrap_or(&Value::Null)
            ));
        }
        Ok(lines)
    }

    /// Poll Kanboard forever, claiming and executing work up to the concurrency limit.
    ///
    /// Startup recovery runs once before polling begins. Each claimed item is
    /// executed in its own Tokio task, and a semaphore keeps the number of
    /// active executions under `worker.max_concurrency`.
    pub async fn run_forever(&self) -> Result<()> {
        self.recover_in_process_tasks().await?;
        let semaphore = Arc::new(Semaphore::new(self.config.worker.max_concurrency));
        loop {
            let permit = semaphore
                .clone()
                .acquire_owned()
                .await
                .expect("semaphore is not closed");
            if let Some(claimed) = self.claim_next_available().await? {
                let worker = self.clone();
                tokio::spawn(async move {
                    let _permit = permit;
                    if let Err(error) = worker.execute_claimed(claimed).await {
                        error!("Background task execution failed: {error}");
                    }
                });
            } else {
                drop(permit);
                sleep(Duration::from_secs(self.config.worker.poll_interval)).await;
            }
        }
    }

    /// Claim the next available unit of work.
    ///
    /// Assigned todo subtasks are preferred over top-level cards so split-out
    /// work can complete before the parent card is processed.
    pub async fn claim_next_available(&self) -> Result<Option<ClaimedTask>> {
        if let Some(subtask) = self.claim_next_subtask().await? {
            return Ok(Some(subtask));
        }
        self.claim_next_task().await
    }

    /// Claim the next assigned top-level task from a configured todo column.
    ///
    /// A task is skipped when it has pending subtasks or an active internal
    /// `is blocked by` link whose blocking card is still in ready, working, or
    /// blocked.
    pub async fn claim_next_task(&self) -> Result<Option<ClaimedTask>> {
        for board in &self.config.boards {
            let lookup = self.lookup_columns(board).await?;
            let board_tasks = self.tasks_by_column(board).await?;
            let blocking_column_titles = BTreeSet::from([
                value_str(&lookup.todo, "title").to_string(),
                value_str(&lookup.working, "title").to_string(),
                value_str(&lookup.blocked, "title").to_string(),
            ]);
            for task in assigned_tasks(
                board_tasks
                    .get(&value_id(&lookup.todo).to_string())
                    .map(Vec::as_slice)
                    .unwrap_or_default(),
                &self.config.server.user,
            ) {
                if self.task_has_pending_subtasks(&task).await? {
                    continue;
                }
                if self
                    .task_has_active_blocking_link(&task, &blocking_column_titles)
                    .await?
                {
                    continue;
                }
                self.client
                    .move_task_to_column(
                        &board.id,
                        &value_id(&task),
                        &value_id(&lookup.working),
                        task.get("swimlane_id").unwrap_or(&Value::from(0)),
                        1,
                    )
                    .await?;
                self.client
                    .create_comment(&value_id(&task), &self.user_id, WORK_STARTED_COMMENT)
                    .await?;
                return Ok(Some(ClaimedTask {
                    board: board.clone(),
                    task: self.client.get_task(&value_id(&task)).await?,
                    todo_column_id: value_id(&lookup.todo),
                    done_column_id: value_id(&lookup.done),
                    blocked_column_id: value_id(&lookup.blocked),
                    subtask: None,
                }));
            }
        }
        Ok(None)
    }

    /// Claim the next assigned todo subtask from any configured board column.
    ///
    /// Claiming a subtask marks it in progress, starts its Kanboard timer, and
    /// posts a progress comment on the parent card.
    pub async fn claim_next_subtask(&self) -> Result<Option<ClaimedTask>> {
        for board in &self.config.boards {
            if board.is_smartsheet() {
                continue;
            }
            let lookup = self.lookup_columns(board).await?;
            for task in self.all_board_tasks(board).await? {
                if let Some(subtask) = self
                    .assigned_subtasks(&task, Some(SUBTASK_STATUS_TODO))
                    .await?
                    .into_iter()
                    .next()
                {
                    let title = value_str(&subtask, "title").to_string();
                    self.client
                        .update_subtask(
                            &value_id(&subtask),
                            &value_id(&task),
                            Some(&title),
                            Some(&self.user_id),
                            Some(SUBTASK_STATUS_IN_PROGRESS),
                        )
                        .await?;
                    self.client
                        .start_subtask_timer(&value_id(&subtask), &self.user_id)
                        .await?;
                    self.client
                        .create_comment(
                            &value_id(&task),
                            &self.user_id,
                            &SUBTASK_WORK_STARTED_COMMENT
                                .replace(
                                    "{subtask_id}",
                                    &value_id(&subtask).to_string().replace('"', ""),
                                )
                                .replace("{title}", &title),
                        )
                        .await?;
                    let mut claimed_subtask = subtask.clone();
                    claimed_subtask["status"] = Value::from(SUBTASK_STATUS_IN_PROGRESS);
                    return Ok(Some(ClaimedTask {
                        board: board.clone(),
                        task: self.client.get_task(&value_id(&task)).await?,
                        subtask: Some(claimed_subtask),
                        todo_column_id: value_id(&lookup.todo),
                        done_column_id: value_id(&lookup.done),
                        blocked_column_id: value_id(&lookup.blocked),
                    }));
                }
            }
        }
        Ok(None)
    }

    /// Return this worker's abandoned in-process work to the queue.
    ///
    /// Top-level working-column tasks assigned to this worker are moved back to
    /// todo. In-progress assigned subtasks have their timers stopped and status
    /// reset to todo.
    pub async fn recover_in_process_tasks(&self) -> Result<usize> {
        let mut recovered = 0;
        for board in &self.config.boards {
            let lookup = self.lookup_columns(board).await?;
            let board_tasks = self.tasks_by_column(board).await?;
            if board.is_smartsheet() {
                continue;
            }
            for task in assigned_tasks(
                board_tasks
                    .get(&value_id(&lookup.working).to_string())
                    .map(Vec::as_slice)
                    .unwrap_or_default(),
                &self.config.server.user,
            ) {
                self.client
                    .create_comment(&value_id(&task), &self.user_id, RECOVERY_COMMENT)
                    .await?;
                self.client
                    .move_task_to_column(
                        &board.id,
                        &value_id(&task),
                        &value_id(&lookup.todo),
                        task.get("swimlane_id").unwrap_or(&Value::from(0)),
                        1,
                    )
                    .await?;
                recovered += 1;
            }
            for task in all_tasks_from_columns(&board_tasks) {
                for subtask in self
                    .assigned_subtasks(&task, Some(SUBTASK_STATUS_IN_PROGRESS))
                    .await?
                {
                    if self
                        .client
                        .has_subtask_timer(&value_id(&subtask), &self.user_id)
                        .await?
                    {
                        self.client
                            .stop_subtask_timer(&value_id(&subtask), &self.user_id)
                            .await?;
                    }
                    let title = value_str(&subtask, "title").to_string();
                    self.client
                        .update_subtask(
                            &value_id(&subtask),
                            &value_id(&task),
                            Some(&title),
                            Some(&self.user_id),
                            Some(SUBTASK_STATUS_TODO),
                        )
                        .await?;
                    self.client
                        .create_comment(&value_id(&task), &self.user_id, RECOVERY_COMMENT)
                        .await?;
                    recovered += 1;
                }
            }
        }
        if recovered > 0 {
            info!("Recovered {recovered} in-process task(s) back to the queue");
        }
        Ok(recovered)
    }

    /// Run the configured ACP agent for one claimed task or subtask and route the result.
    ///
    /// Successful top-level work moves to done unless the agent already moved
    /// the card or created pending subtasks. Failed top-level work moves to the
    /// blocked column. Subtask work updates the subtask status instead of
    /// moving the parent card.
    pub async fn execute_claimed(&self, claimed: ClaimedTask) -> Result<()> {
        let task_id = value_id(&claimed.task);
        let comments = self.client.get_all_comments(&task_id).await?;
        let mut metadata = self.client.get_task_metadata(&task_id).await?;
        let session_id = self.agent_session_id(&claimed, &metadata);
        let prompt = build_agent_prompt(
            &claimed.task,
            &comments,
            &metadata,
            claimed.subtask.as_ref(),
            &self.config.roster,
            &self.config.server.user,
            &self.config.agent.system_prompt,
        );
        let mut session = AcpSession::create(&self.config.agent, &self.config, session_id).await?;
        let turn = session.run_turn(&prompt).await?;
        self.save_agent_session_id(&claimed, &mut metadata, &turn.session_id)
            .await?;
        self.client
            .create_comment(&task_id, &self.user_id, &turn.text)
            .await?;

        if claimed.subtask.is_some() {
            return self.route_subtask_result(&claimed, &turn.stop_reason).await;
        }
        if turn.stop_reason != "end_turn" {
            self.block_task(
                &claimed,
                &format!("Agent stopped with reason {}.", turn.stop_reason),
            )
            .await
        } else if self.agent_moved_task(&claimed).await? {
            Ok(())
        } else if self.task_has_pending_subtasks(&claimed.task).await? {
            self.move_task_to_column(&claimed, &claimed.todo_column_id)
                .await
        } else {
            self.move_task_to_column(&claimed, &claimed.done_column_id)
                .await
        }
    }

    /// Comment on a task and route it to the configured blocked column.
    async fn block_task(&self, claimed: &ClaimedTask, message: &str) -> Result<()> {
        error!("{message}");
        self.client
            .create_comment(&value_id(&claimed.task), &self.user_id, message)
            .await?;
        self.move_task_to_column(claimed, &claimed.blocked_column_id)
            .await
    }

    /// Stop any active subtask timer and update subtask status after an agent turn.
    async fn route_subtask_result(&self, claimed: &ClaimedTask, stop_reason: &str) -> Result<()> {
        let Some(subtask) = &claimed.subtask else {
            return Ok(());
        };
        if self
            .client
            .has_subtask_timer(&value_id(subtask), &self.user_id)
            .await?
        {
            self.client
                .stop_subtask_timer(&value_id(subtask), &self.user_id)
                .await?;
        }
        let title = value_str(subtask, "title").to_string();
        if stop_reason != "end_turn" {
            self.client
                .create_comment(
                    &value_id(&claimed.task),
                    &self.user_id,
                    &format!("Agent stopped with reason {stop_reason}."),
                )
                .await?;
            self.client
                .update_subtask(
                    &value_id(subtask),
                    &value_id(&claimed.task),
                    Some(&title),
                    Some(&Value::from(0)),
                    Some(0),
                )
                .await
        } else {
            self.client
                .update_subtask(
                    &value_id(subtask),
                    &value_id(&claimed.task),
                    Some(&title),
                    Some(&self.user_id),
                    Some(SUBTASK_STATUS_DONE),
                )
                .await
        }
    }

    /// Move a claimed top-level task to a board column id.
    async fn move_task_to_column(&self, claimed: &ClaimedTask, column_id: &Value) -> Result<()> {
        let swimlane_id = if claimed.board.is_smartsheet() {
            Value::from(0)
        } else {
            claimed
                .task
                .get("swimlane_id")
                .cloned()
                .unwrap_or_else(|| Value::from(0))
        };
        self.client
            .move_task_to_column(
                &claimed.board.id,
                &value_id(&claimed.task),
                column_id,
                &swimlane_id,
                1,
            )
            .await
    }

    /// Resolve configured column names for a board against Kanboard's column list.
    async fn lookup_columns(&self, board: &BoardConfig) -> Result<ColumnLookup> {
        let columns = self.client.get_columns(&board.id).await?;
        column_lookup(
            &columns,
            &[
                ("todo", &board.todo),
                ("working", &board.working),
                ("blocked", &board.blocked),
                ("done", &board.done),
            ],
        )
    }

    /// Return board tasks grouped by Kanboard column id.
    async fn tasks_by_column(&self, board: &BoardConfig) -> Result<BTreeMap<String, Vec<Value>>> {
        let mut tasks: BTreeMap<String, Vec<Value>> = BTreeMap::new();
        for swimlane in self.client.get_board(&board.id).await? {
            for column in swimlane
                .get("columns")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                let column_id = value_id(&column).to_string();
                tasks.entry(column_id).or_default().extend(
                    column
                        .get("tasks")
                        .and_then(Value::as_array)
                        .cloned()
                        .unwrap_or_default(),
                );
            }
        }
        Ok(tasks)
    }

    /// Return every task visible on a board, regardless of column.
    async fn all_board_tasks(&self, board: &BoardConfig) -> Result<Vec<Value>> {
        Ok(all_tasks_from_columns(&self.tasks_by_column(board).await?))
    }

    /// Return subtasks assigned to this worker, optionally filtered by status.
    async fn assigned_subtasks(&self, task: &Value, status: Option<i64>) -> Result<Vec<Value>> {
        let mut subtasks = Vec::new();
        for subtask in self.client.get_all_subtasks(&value_id(task)).await? {
            if coerce_id(subtask.get("user_id").unwrap_or(&Value::from(0)))
                != coerce_id(&self.user_id)
            {
                continue;
            }
            if let Some(status) = status
                && value_i64(&subtask, "status") != status
            {
                continue;
            }
            subtasks.push(subtask);
        }
        Ok(subtasks)
    }

    /// Return whether a task has any incomplete subtasks.
    async fn task_has_pending_subtasks(&self, task: &Value) -> Result<bool> {
        Ok(self
            .client
            .get_all_subtasks(&value_id(task))
            .await?
            .iter()
            .any(|subtask| value_i64(subtask, "status") != SUBTASK_STATUS_DONE))
    }

    /// Return whether a task is blocked by an internal link in an active column.
    async fn task_has_active_blocking_link(
        &self,
        task: &Value,
        column_titles: &BTreeSet<String>,
    ) -> Result<bool> {
        for link in self.client.get_all_task_links(&value_id(task)).await? {
            if !value_str(&link, "label").eq_ignore_ascii_case(BLOCKED_BY_LINK_LABEL) {
                continue;
            }
            if !column_titles.contains(value_str(&link, "column_title")) {
                continue;
            }
            let linked_task = link
                .get("task_id")
                .map(Value::to_string)
                .unwrap_or_else(|| "null".to_string());
            info!(
                "Skipping task {} because it is blocked by linked task {} in column {}",
                value_id(task),
                linked_task,
                value_str(&link, "column_title")
            );
            return Ok(true);
        }
        Ok(false)
    }

    /// Read the saved ACP session id for a claimed task or subtask from metadata.
    fn agent_session_id(
        &self,
        claimed: &ClaimedTask,
        metadata: &serde_json::Map<String, Value>,
    ) -> Option<String> {
        metadata
            .get(&session_metadata_key(
                &self.config.server.user,
                claimed.subtask.as_ref().map(value_id).as_ref(),
            ))
            .and_then(Value::as_str)
            .map(str::to_string)
    }

    /// Persist the ACP session id for future turns on the same task or subtask.
    async fn save_agent_session_id(
        &self,
        claimed: &ClaimedTask,
        metadata: &mut serde_json::Map<String, Value>,
        session_id: &str,
    ) -> Result<()> {
        if session_id.is_empty() {
            return Ok(());
        }
        let key = session_metadata_key(
            &self.config.server.user,
            claimed.subtask.as_ref().map(value_id).as_ref(),
        );
        if metadata.get(&key).and_then(Value::as_str) == Some(session_id) {
            return Ok(());
        }
        self.client
            .save_task_metadata(
                &value_id(&claimed.task),
                serde_json::Map::from_iter([(key.clone(), json!(session_id))]),
            )
            .await?;
        metadata.insert(key, json!(session_id));
        Ok(())
    }

    /// Return whether the agent moved the task away from its claimed column.
    async fn agent_moved_task(&self, claimed: &ClaimedTask) -> Result<bool> {
        if claimed.task.get("column_id").is_none() {
            return Ok(false);
        }
        let current = self.client.get_task(&value_id(&claimed.task)).await?;
        Ok(current.get("column_id").map(Value::to_string)
            != claimed.task.get("column_id").map(Value::to_string))
    }
}

/// Return the Kanboard metadata key used to persist an ACP session id.
///
/// Subtasks get separate session keys so parent-task and subtask agent
/// conversations do not overwrite each other.
pub fn session_metadata_key(server_user: &str, subtask_id: Option<&Value>) -> String {
    if let Some(subtask_id) = subtask_id {
        format!(
            "kanboard_worker.{server_user}.subtask.{}.session_id",
            subtask_id.to_string().replace('"', "")
        )
    } else {
        format!("kanboard_worker.{server_user}.session_id")
    }
}

/// Filter task payloads to those assigned to a board username.
fn assigned_tasks(tasks: &[Value], username: &str) -> Vec<Value> {
    tasks
        .iter()
        .filter(|task| {
            let assignee = value_str(task, "assignee_username");
            assignee == username || assignee.eq_ignore_ascii_case(username)
        })
        .cloned()
        .collect()
}

/// Flatten grouped column task payloads into a single vector.
fn all_tasks_from_columns(tasks_by_column: &BTreeMap<String, Vec<Value>>) -> Vec<Value> {
    tasks_by_column.values().flatten().cloned().collect()
}
