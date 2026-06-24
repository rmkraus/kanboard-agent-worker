//! Smartsheet REST client and adapter helpers.
//!
//! Smartsheet sheets are exposed to the worker through the same board-shaped
//! operations used for Kanboard. A configured sheet uses one status column as
//! the workflow column and one assignee column for worker ownership.

use std::{collections::BTreeMap, time::Duration};

use reqwest::header;
use serde_json::{Value, json};
use tokio::time::sleep;

use crate::{
    AppError, Result,
    config::{BoardConfig, SmartsheetConfig},
    kanboard::value_id,
};

/// Default Smartsheet REST API root.
pub const DEFAULT_SMARTSHEET_URL: &str = "https://api.smartsheet.com/2.0";

/// REST client for Smartsheet API 2.0.
#[derive(Debug, Clone)]
pub struct SmartsheetClient {
    /// Normalized API base URL, without a trailing slash.
    base_url: String,
    /// API access token used as a bearer token.
    token: String,
    /// Worker username/email used when filtering assigned rows.
    user: String,
    /// Configured sheets that should be exposed as boards.
    boards: Vec<BoardConfig>,
    /// Reusable async HTTP client.
    client: reqwest::Client,
    /// Maximum attempts for retryable rate-limit/server errors.
    retry_attempts: usize,
    /// Base retry delay multiplied by the attempt number.
    retry_delay: Duration,
}

impl SmartsheetClient {
    /// Create a Smartsheet client from worker config.
    pub fn new(
        config: Option<&SmartsheetConfig>,
        user: impl Into<String>,
        boards: Vec<BoardConfig>,
    ) -> Self {
        let config = config.cloned().unwrap_or_default();
        Self {
            base_url: normalize_base_url(&config.url),
            token: config.token,
            user: user.into(),
            boards,
            client: reqwest::Client::new(),
            retry_attempts: 5,
            retry_delay: Duration::from_millis(500),
        }
    }

    /// Return the configured worker identity in a Kanboard-like shape.
    pub async fn get_me(&self) -> Result<Value> {
        if self.token.trim().is_empty() {
            return Ok(json!({"id": self.user, "username": self.user, "email": self.user}));
        }
        match self.get("/users/me").await {
            Ok(user) => Ok(json!({
                "id": user.get("id").cloned().unwrap_or_else(|| json!(self.user)),
                "username": user.get("email").and_then(Value::as_str).unwrap_or(&self.user),
                "email": user.get("email").and_then(Value::as_str).unwrap_or(&self.user),
                "name": user.get("name").and_then(Value::as_str).unwrap_or("")
            })),
            Err(_) => Ok(json!({"id": self.user, "username": self.user, "email": self.user})),
        }
    }

    /// Return virtual workflow columns for a configured Smartsheet sheet.
    pub async fn get_columns(&self, sheet_id: &Value) -> Result<Vec<Value>> {
        let board = self.board_for_sheet(sheet_id)?;
        Ok(vec![
            json!({"id": "todo", "title": board.todo}),
            json!({"id": "working", "title": board.working}),
            json!({"id": "blocked", "title": board.blocked}),
            json!({"id": "done", "title": board.done}),
        ])
    }

    /// Return a Kanboard-like board payload for the Smartsheet sheet.
    pub async fn get_board(&self, sheet_id: &Value) -> Result<Vec<Value>> {
        let board = self.board_for_sheet(sheet_id)?;
        let sheet = self.sheet(&sheet_id_string(sheet_id)?).await?;
        let columns = sheet_columns_by_title(&sheet);
        let status_column =
            required_column_id(&columns, board.status_column.as_deref(), "status_column")?;
        let mut buckets = BTreeMap::from_iter([
            ("todo".to_string(), Vec::new()),
            ("working".to_string(), Vec::new()),
            ("blocked".to_string(), Vec::new()),
            ("done".to_string(), Vec::new()),
        ]);
        for row in sheet
            .get("rows")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
        {
            let status = cell_text(&row, status_column).unwrap_or_default();
            let logical = status_to_logical(&board, &status).unwrap_or("todo");
            buckets
                .entry(logical.to_string())
                .or_default()
                .push(self.row_to_task(&board, &sheet, &row)?);
        }
        Ok(vec![json!({
            "id": 0,
            "name": "Default",
            "columns": [
                {"id": "todo", "title": board.todo, "tasks": buckets.remove("todo").unwrap_or_default()},
                {"id": "working", "title": board.working, "tasks": buckets.remove("working").unwrap_or_default()},
                {"id": "blocked", "title": board.blocked, "tasks": buckets.remove("blocked").unwrap_or_default()},
                {"id": "done", "title": board.done, "tasks": buckets.remove("done").unwrap_or_default()}
            ]
        })])
    }

    /// Return one Smartsheet row as a Kanboard-like task.
    pub async fn get_task(&self, task_id: &Value) -> Result<Value> {
        let (sheet_id, row_id) = parse_task_id(task_id)?;
        let board = self.board_for_sheet(&json!(sheet_id))?;
        let sheet = self.sheet(&sheet_id).await?;
        let row = self
            .get(&format!("/sheets/{sheet_id}/rows/{row_id}"))
            .await?;
        self.row_to_task(&board, &sheet, &row)
    }

    /// Return row discussions flattened into Kanboard-like comments.
    pub async fn get_all_comments(&self, task_id: &Value) -> Result<Vec<Value>> {
        let (sheet_id, row_id) = parse_task_id(task_id)?;
        let result = self
            .get(&format!(
                "/sheets/{sheet_id}/rows/{row_id}/discussions?include=comments&includeAll=true"
            ))
            .await?;
        let mut comments = Vec::new();
        for discussion in result
            .get("data")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default()
        {
            for comment in discussion
                .get("comments")
                .and_then(Value::as_array)
                .cloned()
                .unwrap_or_default()
            {
                let created_by = comment.get("createdBy").cloned().unwrap_or(Value::Null);
                comments.push(json!({
                    "id": comment.get("id").cloned().unwrap_or(Value::Null),
                    "user_id": created_by.get("email").cloned().unwrap_or(Value::Null),
                    "username": created_by.get("email").or_else(|| created_by.get("name")).and_then(Value::as_str).unwrap_or(""),
                    "date_creation": comment.get("createdAt").and_then(Value::as_str).unwrap_or(""),
                    "comment": comment.get("text").and_then(Value::as_str).unwrap_or(""),
                }));
            }
        }
        Ok(comments)
    }

    /// Create a new discussion on a Smartsheet row.
    pub async fn create_comment(&self, task_id: &Value, content: &str) -> Result<i64> {
        let (sheet_id, row_id) = parse_task_id(task_id)?;
        let result = self
            .post(
                &format!("/sheets/{sheet_id}/rows/{row_id}/discussions"),
                &json!({"comment": {"text": content}}),
            )
            .await?;
        Ok(result
            .pointer("/result/id")
            .or_else(|| result.get("id"))
            .and_then(Value::as_i64)
            .unwrap_or(0))
    }

    /// Return metadata stored in the configured metadata column, when present.
    pub async fn get_task_metadata(
        &self,
        task_id: &Value,
    ) -> Result<serde_json::Map<String, Value>> {
        let (sheet_id, row_id) = parse_task_id(task_id)?;
        let board = self.board_for_sheet(&json!(sheet_id))?;
        let Some(metadata_column) = board.metadata_column.as_deref() else {
            return Ok(serde_json::Map::new());
        };
        let sheet = self.sheet(&sheet_id).await?;
        let columns = sheet_columns_by_title(&sheet);
        let Some(column_id) = columns.get(metadata_column).copied() else {
            return Ok(serde_json::Map::new());
        };
        let row = self
            .get(&format!("/sheets/{sheet_id}/rows/{row_id}"))
            .await?;
        let Some(text) = cell_text(&row, column_id) else {
            return Ok(serde_json::Map::new());
        };
        serde_json::from_str(&text).or_else(|_| Ok(serde_json::Map::new()))
    }

    /// Merge metadata into the configured metadata column, when present.
    pub async fn save_task_metadata(
        &self,
        task_id: &Value,
        values: serde_json::Map<String, Value>,
    ) -> Result<()> {
        let (sheet_id, row_id) = parse_task_id(task_id)?;
        let board = self.board_for_sheet(&json!(sheet_id))?;
        let Some(metadata_column) = board.metadata_column.as_deref() else {
            return Ok(());
        };
        let sheet = self.sheet(&sheet_id).await?;
        let columns = sheet_columns_by_title(&sheet);
        let column_id = required_column_id(&columns, Some(metadata_column), "metadata_column")?;
        let mut merged = self.get_task_metadata(task_id).await?;
        merged.extend(values);
        self.update_cell(
            &sheet_id,
            &row_id,
            column_id,
            json!(serde_json::to_string(&merged)?),
        )
        .await
    }

    /// Move a row by changing its configured status cell.
    pub async fn move_task_to_column(
        &self,
        sheet_id: &Value,
        task_id: &Value,
        column_id: &Value,
    ) -> Result<()> {
        let sheet_id = sheet_id_string(sheet_id)?;
        let (_, row_id) = parse_task_id(task_id)?;
        let board = self.board_for_sheet(&json!(sheet_id))?;
        let sheet = self.sheet(&sheet_id).await?;
        let columns = sheet_columns_by_title(&sheet);
        let status_column =
            required_column_id(&columns, board.status_column.as_deref(), "status_column")?;
        let target_value = match column_id.as_str().unwrap_or_default() {
            "todo" => &board.todo,
            "working" => &board.working,
            "blocked" => &board.blocked,
            "done" => &board.done,
            other => {
                return Err(AppError::Kanboard(format!(
                    "Unknown Smartsheet virtual column: {other}"
                )));
            }
        };
        self.update_cell(&sheet_id, &row_id, status_column, json!(target_value))
            .await
    }

    /// List row attachments.
    pub async fn get_all_task_files(&self, task_id: &Value) -> Result<Vec<Value>> {
        let (sheet_id, row_id) = parse_task_id(task_id)?;
        let result = self
            .get(&format!(
                "/sheets/{sheet_id}/rows/{row_id}/attachments?includeAll=true"
            ))
            .await?;
        Ok(result
            .get("data")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default())
    }

    /// Download an attachment by resolving its temporary URL first.
    pub async fn download_task_file(
        &self,
        file_id: &Value,
        sheet_hint: Option<&str>,
    ) -> Result<Vec<u8>> {
        let sheet_id = sheet_hint
            .map(str::to_string)
            .or_else(|| {
                self.boards
                    .iter()
                    .find(|board| board.is_smartsheet())
                    .and_then(|board| {
                        board
                            .id
                            .as_i64()
                            .map(|id| id.to_string())
                            .or_else(|| board.id.as_str().map(str::to_string))
                    })
            })
            .ok_or_else(|| {
                AppError::Kanboard(
                    "No Smartsheet sheet configured for attachment download".to_string(),
                )
            })?;
        let attachment = self
            .get(&format!(
                "/sheets/{sheet_id}/attachments/{}",
                id_text(file_id)
            ))
            .await?;
        let url = attachment
            .get("url")
            .and_then(Value::as_str)
            .ok_or_else(|| {
                AppError::Kanboard(
                    "Smartsheet attachment response did not include a download URL".to_string(),
                )
            })?;
        let response = self.client.get(url).send().await?;
        if !response.status().is_success() {
            return Err(AppError::Kanboard(format!(
                "Smartsheet attachment HTTP {}",
                response.status()
            )));
        }
        Ok(response.bytes().await?.to_vec())
    }

    /// Upload bytes as a row attachment.
    pub async fn create_task_file(
        &self,
        sheet_id: &Value,
        task_id: &Value,
        filename: &str,
        content: &[u8],
    ) -> Result<i64> {
        let sheet_id = sheet_id_string(sheet_id)?;
        let (_, row_id) = parse_task_id(task_id)?;
        let url = format!(
            "{}/sheets/{sheet_id}/rows/{row_id}/attachments",
            self.base_url
        );
        let response = self
            .client
            .post(url)
            .bearer_auth(&self.token)
            .header(
                header::CONTENT_DISPOSITION,
                format!("attachment; filename=\"{filename}\""),
            )
            .header(header::CONTENT_LENGTH, content.len())
            .header(header::CONTENT_TYPE, "application/octet-stream")
            .body(content.to_vec())
            .send()
            .await?;
        let result = self.response_json(response).await?;
        Ok(result
            .pointer("/result/id")
            .and_then(Value::as_i64)
            .unwrap_or(0))
    }

    /// Delete an attachment. Smartsheet requires a sheet id; use the first configured sheet.
    pub async fn remove_task_file(&self, file_id: &Value) -> Result<()> {
        let sheet_id = self
            .boards
            .iter()
            .find(|board| board.is_smartsheet())
            .map(|board| id_text(&board.id))
            .ok_or_else(|| {
                AppError::Kanboard(
                    "No Smartsheet sheet configured for attachment deletion".to_string(),
                )
            })?;
        self.delete(&format!(
            "/sheets/{sheet_id}/attachments/{}",
            id_text(file_id)
        ))
        .await?;
        Ok(())
    }

    /// Return a configured board for a sheet id.
    fn board_for_sheet(&self, sheet_id: &Value) -> Result<BoardConfig> {
        let wanted = id_text(sheet_id);
        self.boards
            .iter()
            .find(|board| board.is_smartsheet() && id_text(&board.id) == wanted)
            .cloned()
            .ok_or_else(|| {
                AppError::Kanboard(format!("Smartsheet sheet {wanted} is not configured"))
            })
    }

    /// Fetch a sheet with rows and columns.
    async fn sheet(&self, sheet_id: &str) -> Result<Value> {
        self.get(&format!("/sheets/{sheet_id}")).await
    }

    /// Convert a Smartsheet row to a Kanboard-like task object.
    fn row_to_task(&self, board: &BoardConfig, sheet: &Value, row: &Value) -> Result<Value> {
        let columns = sheet_columns_by_title(sheet);
        let status_column =
            required_column_id(&columns, board.status_column.as_deref(), "status_column")?;
        let assignee_column = required_column_id(
            &columns,
            board.assignee_column.as_deref(),
            "assignee_column",
        )?;
        let title_column =
            required_column_id(&columns, board.title_column.as_deref(), "title_column")?;
        let description = match board.description_column.as_deref() {
            Some(title) => columns
                .get(title)
                .and_then(|id| cell_text(row, *id))
                .unwrap_or_default(),
            None => String::new(),
        };
        let status = cell_text(row, status_column).unwrap_or_default();
        let logical = status_to_logical(board, &status).unwrap_or("todo");
        let sheet_id = id_text(&board.id);
        let row_id = row.get("id").map(id_text).unwrap_or_default();
        Ok(json!({
            "id": format!("{sheet_id}:{row_id}"),
            "project_id": board.id,
            "title": cell_text(row, title_column).unwrap_or_else(|| format!("Row {row_id}")),
            "description": description,
            "assignee_username": cell_text(row, assignee_column).unwrap_or_default(),
            "column_id": logical,
            "column_title": status,
            "swimlane_id": 0,
            "row_id": row_id,
            "sheet_id": sheet_id,
            "permalink": row.get("permalink").cloned().unwrap_or(Value::Null),
        }))
    }

    /// Update one cell on a row.
    async fn update_cell(
        &self,
        sheet_id: &str,
        row_id: &str,
        column_id: i64,
        value: Value,
    ) -> Result<()> {
        self.put(
            &format!("/sheets/{sheet_id}/rows"),
            &json!([{
                "id": row_id.parse::<i64>().unwrap_or_default(),
                "cells": [{"columnId": column_id, "value": value, "strict": false}]
            }]),
        )
        .await?;
        Ok(())
    }

    async fn get(&self, path: &str) -> Result<Value> {
        let url = format!("{}{}", self.base_url, path);
        self.request_json(|| self.client.get(&url).bearer_auth(&self.token))
            .await
    }

    async fn post(&self, path: &str, body: &Value) -> Result<Value> {
        let url = format!("{}{}", self.base_url, path);
        self.request_json(|| self.client.post(&url).bearer_auth(&self.token).json(body))
            .await
    }

    async fn put(&self, path: &str, body: &Value) -> Result<Value> {
        let url = format!("{}{}", self.base_url, path);
        self.request_json(|| self.client.put(&url).bearer_auth(&self.token).json(body))
            .await
    }

    async fn delete(&self, path: &str) -> Result<Value> {
        let url = format!("{}{}", self.base_url, path);
        self.request_json(|| self.client.delete(&url).bearer_auth(&self.token))
            .await
    }

    async fn request_json<F>(&self, request: F) -> Result<Value>
    where
        F: Fn() -> reqwest::RequestBuilder,
    {
        for attempt in 1..=self.retry_attempts {
            let response = request().send().await?;
            if (response.status().as_u16() == 429 || response.status().is_server_error())
                && attempt < self.retry_attempts
            {
                self.sleep_before_retry(attempt).await;
                continue;
            }
            return self.response_json(response).await;
        }
        Err(AppError::Kanboard(
            "Smartsheet retry attempts exhausted".to_string(),
        ))
    }

    async fn response_json(&self, response: reqwest::Response) -> Result<Value> {
        let status = response.status();
        let body = response.text().await?;
        if !status.is_success() {
            return Err(AppError::Kanboard(format!(
                "Smartsheet HTTP {status}: {body}"
            )));
        }
        if body.trim().is_empty() {
            return Ok(Value::Null);
        }
        Ok(serde_json::from_str(&body)?)
    }

    async fn sleep_before_retry(&self, attempt: usize) {
        sleep(self.retry_delay * attempt as u32).await;
    }
}

/// Normalize a Smartsheet API URL.
pub fn normalize_base_url(url: &str) -> String {
    let clean = url.trim().trim_end_matches('/');
    if clean.is_empty() {
        DEFAULT_SMARTSHEET_URL.to_string()
    } else {
        clean.to_string()
    }
}

/// Return configured column ids by title from a Smartsheet sheet response.
fn sheet_columns_by_title(sheet: &Value) -> BTreeMap<String, i64> {
    sheet
        .get("columns")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|column| {
            Some((
                column.get("title")?.as_str()?.to_string(),
                column.get("id")?.as_i64()?,
            ))
        })
        .collect()
}

/// Resolve a required configured Smartsheet column title to its id.
fn required_column_id(
    columns: &BTreeMap<String, i64>,
    title: Option<&str>,
    path: &str,
) -> Result<i64> {
    let title =
        title.ok_or_else(|| AppError::Config(format!("Smartsheet board {path} is required")))?;
    columns.get(title).copied().ok_or_else(|| {
        AppError::Kanboard(format!(
            "Smartsheet column {path}={title:?} was not found. Available columns: {}",
            columns.keys().cloned().collect::<Vec<_>>().join(", ")
        ))
    })
}

/// Return a cell's display text/value for a column id.
fn cell_text(row: &Value, column_id: i64) -> Option<String> {
    row.get("cells")?
        .as_array()?
        .iter()
        .find(|cell| cell.get("columnId").and_then(Value::as_i64) == Some(column_id))
        .and_then(|cell| {
            cell.get("displayValue")
                .or_else(|| cell.get("value"))
                .or_else(|| {
                    cell.get("objectValue")
                        .and_then(|object| object.get("email"))
                })
                .or_else(|| {
                    cell.get("objectValue")
                        .and_then(|object| object.get("name"))
                })
        })
        .map(|value| match value {
            Value::String(text) => text.to_string(),
            other => other.to_string().trim_matches('"').to_string(),
        })
}

/// Map a status cell value to a worker logical column id.
fn status_to_logical<'a>(board: &'a BoardConfig, status: &str) -> Option<&'a str> {
    if status == board.todo {
        Some("todo")
    } else if status == board.working {
        Some("working")
    } else if status == board.blocked {
        Some("blocked")
    } else if status == board.done {
        Some("done")
    } else {
        None
    }
}

/// Convert a sheet id config value to string.
fn sheet_id_string(value: &Value) -> Result<String> {
    let text = id_text(value);
    if text.is_empty() || text == "null" {
        Err(AppError::Kanboard(
            "Smartsheet sheet id is empty".to_string(),
        ))
    } else {
        Ok(text)
    }
}

/// Parse the encoded worker task id `<sheet_id>:<row_id>`.
pub fn parse_task_id(value: &Value) -> Result<(String, String)> {
    let text = id_text(value);
    let Some((sheet_id, row_id)) = text.split_once(':') else {
        return Err(AppError::Kanboard(format!(
            "Smartsheet task id must be encoded as <sheet_id>:<row_id>, got {text}"
        )));
    };
    Ok((sheet_id.to_string(), row_id.to_string()))
}

/// Stringify an id-like JSON value without quotes.
pub fn id_text(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        Value::Number(number) => number.to_string(),
        other => value_id(other).to_string().trim_matches('"').to_string(),
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for Smartsheet helper functions.

    use super::*;

    /// Base URL normalization supplies the official default and trims trailing slashes.
    #[test]
    fn normalizes_smartsheet_base_url() {
        assert_eq!(normalize_base_url(""), DEFAULT_SMARTSHEET_URL);
        assert_eq!(
            normalize_base_url("https://api.smartsheet.com/2.0/"),
            DEFAULT_SMARTSHEET_URL
        );
    }

    /// Encoded task ids split into sheet and row ids.
    #[test]
    fn parses_encoded_task_ids() {
        assert_eq!(
            parse_task_id(&json!("123:456")).unwrap(),
            ("123".to_string(), "456".to_string())
        );
    }
}
