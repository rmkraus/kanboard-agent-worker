//! Prompt construction for ACP agent turns.
//!
//! The prompt combines the default worker operating instructions, configured
//! additions, Kanboard card metadata, conversation comments, roster entries, and
//! optional subtask context into one text payload for the agent.

use regex::Regex;
use serde_json::{Value, json};

use crate::config::RosterEntry;

/// Baseline system prompt that defines the worker's Kanboard behavior.
pub const DEFAULT_AGENT_SYSTEM_PROMPT: &str = r#"You are a local CLI agent working from a Kanboard card.
Do the requested work in the configured working directory.
Your final response from this turn will be posted as a Kanboard card comment.
Make the final response concise, factual, and useful to a human reviewer.
Include blockers or follow-up steps when relevant.
Do not include private reasoning or raw tool transcripts unless they are necessary for the update.

Kanboard tool use:
- Use the available Kanboard tools instead of inventing text commands.
- Handoff work to another agent with add_subtask when a separable follow-up should be handled by a roster
  member. Use a clear title and set assignee to the exact Kanboard username from the roster when a clear
  owner exists, including yourself when appropriate.
- Manage shared files with list_attachments, get_attachment, upload_attachment, and delete_attachment.
  Download attachments before relying on their contents. Upload generated files, logs, patches, reports,
  or other artifacts that the user or another agent should inspect. Delete attachments only when the task
  explicitly asks for removal.
- Share links, attachment references, and coordination notes with add_comment. Use comments for URLs,
  file paths, artifact summaries, or handoff context that should be visible to the user and other agents
  before your final response.
- Use move_column only for intentional workflow routing between configured columns.

Column policy:
- todo: return the card to the queue only when explicitly asked to requeue it or when no active work should
  continue right now. Do not move to todo just because you created subtasks; the worker returns parent cards
  with pending subtasks to todo after your turn.
- working: the worker normally puts claimed cards here. Move to working only when correcting a card that is
  in the wrong column while active work continues.
- blocked: move here when progress requires a human decision, missing credentials, unavailable dependency,
  reproducible failure outside your control, or another blocker you cannot resolve in this turn.
- done: move here only when the card's requested work is complete and there are no pending subtasks. For
  ordinary successful top-level task completion, you may leave the card in place; the worker will move it
  to done automatically.
For subtasks, complete the subtask work and report the result in your final response. Do not move the parent
task unless the parent itself needs a workflow change."#;

/// Build the complete task prompt sent to an ACP agent.
///
/// When `subtask` is present, the prompt title and spec are rewritten to make
/// the assigned subtask the active work while keeping parent-card context.
pub fn build_agent_prompt(
    task: &Value,
    comments: &[Value],
    metadata: &serde_json::Map<String, Value>,
    subtask: Option<&Value>,
    roster: &[RosterEntry],
    worker_username: &str,
    system_prompt: &str,
) -> String {
    let title = task.get("title").and_then(Value::as_str).unwrap_or("");
    let description = task
        .get("description")
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut spec = extract_section(description, "Spec").unwrap_or_else(|| description.to_string());
    let config = extract_section(description, "Config").unwrap_or_default();
    let mut task_heading = format!(
        "Task #{}: {title}",
        task.get("id").map(Value::to_string).unwrap_or_default()
    )
    .replace('"', "");
    if let Some(subtask) = subtask {
        let subtask_title = subtask.get("title").and_then(Value::as_str).unwrap_or("");
        let subtask_id = subtask
            .get("id")
            .map(Value::to_string)
            .unwrap_or_default()
            .replace('"', "");
        task_heading = format!("{task_heading} / Subtask #{subtask_id}: {subtask_title}");
        spec = format!(
            "Subtask #{subtask_id}: {subtask_title}\n\nParent task context:\n{}",
            spec.trim()
        );
    }

    format!(
        r#"# System Prompt
{}

# Kanboard Worker Identity
Username: {worker_username}
Only work on behalf of this Kanboard user.

# Agent Roster
{}

# Kanboard Card Metadata
{}

# Kanboard Conversation
{}

# Kanboard Task
{task_heading}

## Spec
{}
{}

## Full Card Description
{}
"#,
        merged_system_prompt(system_prompt),
        format_roster(roster),
        card_metadata_json(task, metadata),
        format_comments(comments),
        spec.trim(),
        if config.trim().is_empty() {
            String::new()
        } else {
            format!("\n\n## Config\n{}", config.trim())
        },
        description.trim(),
    )
}

/// Extract a named second-level Markdown section from a card description.
///
/// Section names are matched case-insensitively against `##` headings and the
/// returned text excludes surrounding whitespace and the next section.
pub fn extract_section(markdown: &str, section_name: &str) -> Option<String> {
    let regex = Regex::new(r"(?m)^##\s+([A-Za-z0-9 _-]+)\s*$").unwrap();
    let matches = regex.captures_iter(markdown).collect::<Vec<_>>();
    for (index, captures) in matches.iter().enumerate() {
        if captures
            .get(1)?
            .as_str()
            .trim()
            .eq_ignore_ascii_case(section_name)
        {
            let start = captures.get(0)?.end();
            let end = matches
                .get(index + 1)
                .and_then(|next| next.get(0))
                .map(|next| next.start())
                .unwrap_or(markdown.len());
            return Some(markdown[start..end].trim().to_string());
        }
    }
    None
}

/// Merge the default worker prompt with optional config-provided instructions.
fn merged_system_prompt(system_prompt: &str) -> String {
    let configured = system_prompt.trim();
    if configured.is_empty() {
        DEFAULT_AGENT_SYSTEM_PROMPT.to_string()
    } else {
        format!("{DEFAULT_AGENT_SYSTEM_PROMPT}\n\nAdditional worker instructions:\n{configured}")
    }
}

/// Render Kanboard task data and metadata as a fenced JSON block.
fn card_metadata_json(task: &Value, metadata: &serde_json::Map<String, Value>) -> String {
    let task_metadata = task
        .as_object()
        .map(|map| {
            map.iter()
                .filter(|(key, _)| *key != "description" && *key != "comment")
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect::<serde_json::Map<_, _>>()
        })
        .unwrap_or_default();
    let value = json!({
        "task": task_metadata,
        "task_metadata": metadata,
    });
    format!(
        "```json\n{}\n```",
        serde_json::to_string_pretty(&value).unwrap()
    )
}

/// Render task comments as concise chronological bullet lines.
fn format_comments(comments: &[Value]) -> String {
    if comments.is_empty() {
        return "No comments yet.".to_string();
    }
    comments
        .iter()
        .map(|comment| {
            let username = comment
                .get("username")
                .or_else(|| comment.get("name"))
                .and_then(Value::as_str)
                .map(str::to_string)
                .unwrap_or_else(|| {
                    format!(
                        "user:{}",
                        comment
                            .get("user_id")
                            .unwrap_or(&Value::String("unknown".into()))
                    )
                });
            let created = comment
                .get("date_creation")
                .and_then(Value::as_str)
                .unwrap_or("unknown-time");
            let body = comment
                .get("comment")
                .and_then(Value::as_str)
                .unwrap_or("")
                .trim();
            format!("- {created} {username}: {body}")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

/// Render roster entries for the agent handoff instructions.
fn format_roster(roster: &[RosterEntry]) -> String {
    let lines = roster
        .iter()
        .filter(|entry| !entry.name.is_empty())
        .map(|entry| {
            if entry.description.is_empty() {
                format!("- {}", entry.name)
            } else {
                format!("- {}: {}", entry.name, entry.description)
            }
        })
        .collect::<Vec<_>>();
    if lines.is_empty() {
        "No roster configured. Agents can assign new subtasks to this worker.".to_string()
    } else {
        lines.join("\n")
    }
}

#[cfg(test)]
mod tests {
    //! Unit tests for Markdown section parsing and prompt assembly.

    use serde_json::json;

    use super::*;

    /// Markdown section extraction returns body text for matching headings.
    #[test]
    fn extracts_markdown_sections() {
        let markdown = "Intro\n## Spec\nDo it\n## Config\nagent: codex\n";
        assert_eq!(extract_section(markdown, "spec").unwrap(), "Do it");
        assert_eq!(extract_section(markdown, "Config").unwrap(), "agent: codex");
        assert!(extract_section(markdown, "Missing").is_none());
    }

    /// Subtask prompts include parent context, worker identity, and comments.
    #[test]
    fn builds_prompt_with_subtask_context() {
        let prompt = build_agent_prompt(
            &json!({"id": 42, "title": "Parent", "description": "## Spec\nParent work"}),
            &[json!({"username": "alice", "date_creation": "now", "comment": "hello"})],
            &serde_json::Map::new(),
            Some(&json!({"id": 99, "title": "Subtask work"})),
            &[],
            "codex-node1",
            "",
        );

        assert!(prompt.contains("Username: codex-node1"));
        assert!(prompt.contains("Subtask #99: Subtask work"));
        assert!(prompt.contains("Parent task context:"));
        assert!(prompt.contains("now alice: hello"));
    }
}
