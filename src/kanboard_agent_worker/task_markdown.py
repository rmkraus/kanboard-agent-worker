"""Markdown prompt and output helpers for Kanboard cards."""

from __future__ import annotations

import json
import re
from typing import Any

from jinja2 import Environment, StrictUndefined

from .config import RosterEntry

SECTION_PATTERN = re.compile(r"(?m)^##\s+([A-Za-z0-9 _-]+)\s*$")

DEFAULT_AGENT_SYSTEM_PROMPT = """You are a local CLI agent working from a Kanboard card.
Do the requested work in the configured working directory.
Your final response from this turn will be posted as a Kanboard card comment and copied into the card's Output section.
Make the final response concise, factual, and useful to a human reviewer.
Include blockers or follow-up steps when relevant.
Use the available Kanboard tools instead of inventing text commands.
Use list_attachments to inspect task files, get_attachment to download a task file, upload_attachment to attach local work, and delete_attachment only when the task explicitly calls for removing an attachment.
Use add_subtask for follow-up work that should be handled separately. Assign subtasks to exact roster usernames when a clear owner exists, including yourself when appropriate.
Use move_column only for intentional workflow routing between configured columns.
Column policy:
- todo: return the card to the queue only when explicitly asked to requeue it or when no active work should continue right now. Do not move to todo just because you created subtasks.
- working: the worker normally puts claimed cards here. Move to working only when correcting a card that is in the wrong column while active work continues.
- blocked: move here when progress requires a human decision, missing credentials, unavailable dependency, reproducible failure outside your control, or another blocker you cannot resolve in this turn.
- done: move here only when the card's requested work is complete and there are no pending subtasks. For ordinary successful top-level task completion, you may leave the card in place; the worker will move it to done automatically.
For subtasks, complete the subtask work and report the result in your final response. Do not move the parent task unless the parent itself needs a workflow change.
Do not include private reasoning or raw tool transcripts unless they are necessary for the update."""

AGENT_PROMPT_TEMPLATE = Environment(
    autoescape=False,
    lstrip_blocks=True,
    trim_blocks=True,
    undefined=StrictUndefined,
).from_string(
    """# System Prompt
{{ system_prompt }}

# Kanboard Worker Identity
Username: {{ worker_username }}
Only work on behalf of this Kanboard user.

# Agent Roster
{{ roster }}

# Kanboard Card Metadata
{{ card_metadata }}

# Kanboard Conversation
{{ conversation }}

# Kanboard Task
{{ task_heading }}

## Spec
{{ spec }}
{% if config %}

## Config
{{ config }}
{% endif %}

## Full Card Description
{{ description }}
"""
)


def build_agent_prompt(
    task: dict[str, Any],
    *,
    comments: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
    subtask: dict[str, Any] | None = None,
    roster: tuple[RosterEntry, ...] | list[RosterEntry] | None = None,
    worker_username: str,
    system_prompt: str = "",
) -> str:
    """Build the prompt sent to the local agent for one Kanboard task."""

    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    spec = extract_section(description, "Spec") or description
    config = extract_section(description, "Config")
    task_heading = f"Task #{task.get('id')}: {title}".strip()
    if subtask:
        subtask_title = str(subtask.get("title") or "")
        task_heading = f"{task_heading} / Subtask #{subtask.get('id')}: {subtask_title}".strip()
        spec = f"Subtask #{subtask.get('id')}: {subtask_title}\n\nParent task context:\n{spec}".strip()

    return (
        AGENT_PROMPT_TEMPLATE.render(
            system_prompt=_merged_system_prompt(system_prompt),
            worker_username=worker_username,
            roster=_format_roster(roster or ()),
            card_metadata=_card_metadata_json(task, metadata or {}),
            conversation=_format_comments(comments or []),
            task_heading=task_heading,
            spec=spec.strip(),
            config=config.strip() if config else "",
            description=description.strip(),
        ).strip()
        + "\n"
    )


def extract_section(markdown: str, section_name: str) -> str | None:
    """Return the body of a second-level markdown section by heading name."""

    matches = list(SECTION_PATTERN.finditer(markdown))
    desired = section_name.casefold()
    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != desired:
            continue
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        return markdown[start:end].strip()
    return None


def replace_output_section(markdown: str, output: str) -> str:
    """Replace or append the card's ``## Output`` section."""

    replacement = f"## Output\n{output.strip()}\n"
    matches = list(SECTION_PATTERN.finditer(markdown))

    for index, match in enumerate(matches):
        if match.group(1).strip().casefold() != "output":
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        prefix = markdown[:start].rstrip()
        suffix = markdown[end:].lstrip()
        pieces = [prefix, replacement.rstrip(), suffix]
        return "\n\n".join(piece for piece in pieces if piece).rstrip() + "\n"

    return markdown.rstrip() + "\n\n" + replacement


def _merged_system_prompt(system_prompt: str) -> str:
    configured = system_prompt.strip()
    if not configured:
        return DEFAULT_AGENT_SYSTEM_PROMPT
    return DEFAULT_AGENT_SYSTEM_PROMPT + "\n\nAdditional worker instructions:\n" + configured


def _card_metadata_json(task: dict[str, Any], metadata: dict[str, str]) -> str:
    card_metadata = {
        "task": {
            key: value
            for key, value in sorted(task.items())
            if key not in {"description", "comment"}
        },
        "task_metadata": metadata,
    }
    return "```json\n" + json.dumps(card_metadata, indent=2, sort_keys=True, default=str) + "\n```"


def _format_comments(comments: list[dict[str, Any]]) -> str:
    if not comments:
        return "No comments yet."

    lines = []
    for comment in comments:
        username = comment.get("username") or comment.get("name") or f"user:{comment.get('user_id', 'unknown')}"
        created = comment.get("date_creation") or "unknown-time"
        body = str(comment.get("comment") or "").strip()
        lines.append(f"- {created} {username}: {body}")
    return "\n".join(lines)


def _format_roster(roster: tuple[RosterEntry, ...] | list[RosterEntry]) -> str:
    if not roster:
        return "No roster configured. Agents can assign new subtasks to this worker."

    lines = []
    for entry in roster:
        name = entry.name
        description = entry.description
        if not name:
            continue
        if description:
            lines.append(f"- {name}: {description}")
        else:
            lines.append(f"- {name}")
    return "\n".join(lines) or "No roster configured. Agents can assign new subtasks to this worker."
