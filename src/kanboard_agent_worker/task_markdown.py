from __future__ import annotations

import json
import re
from typing import Any

SECTION_PATTERN = re.compile(r"(?m)^##\s+([A-Za-z0-9 _-]+)\s*$")

DEFAULT_AGENT_SYSTEM_PROMPT = """You are a local CLI agent working from a Kanboard card.
Do the requested work in the configured working directory.
Your final response from this turn will be posted as a Kanboard card comment and copied into the card's Output section.
Make the final response concise, factual, and useful to a human reviewer.
Include blockers or follow-up steps when relevant.
End the final response with exactly one status line: KANBOARD_STATUS: done or KANBOARD_STATUS: blocked.
Use KANBOARD_STATUS: blocked when you need human input or cannot continue safely.
Do not include private reasoning or raw tool transcripts unless they are necessary for the update."""


def build_agent_prompt(
    task: dict[str, Any],
    *,
    comments: list[dict[str, Any]] | None = None,
    metadata: dict[str, str] | None = None,
    worker_username: str,
    system_prompt: str = "",
) -> str:
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    spec = extract_section(description, "Spec") or description
    config = extract_section(description, "Config")
    merged_system_prompt = _merged_system_prompt(system_prompt)

    parts = [
        "# System Prompt",
        merged_system_prompt,
        "",
        "# Kanboard Worker Identity",
        f"Username: {worker_username}",
        "Only work on behalf of this Kanboard user.",
        "",
        "# Kanboard Card Metadata",
        _card_metadata_json(task, metadata or {}),
        "",
        "# Kanboard Conversation",
        _format_comments(comments or []),
        "",
        "# Kanboard Task",
        f"Task #{task.get('id')}: {title}".strip(),
        "",
        "## Spec",
        spec.strip(),
    ]

    if config:
        parts.extend(["", "## Config", config.strip()])

    parts.extend(["", "## Full Card Description", description.strip()])
    return "\n".join(parts).strip() + "\n"


def extract_section(markdown: str, section_name: str) -> str | None:
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


def summarize_output(output: str, max_chars: int = 6000) -> str:
    clean = output.strip()
    if len(clean) <= max_chars:
        return clean or "Agent completed without output."
    return clean[:max_chars].rstrip() + "\n\n[Output truncated by worker.]"


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
