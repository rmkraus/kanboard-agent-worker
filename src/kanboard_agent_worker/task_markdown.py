from __future__ import annotations

import re
import shlex
from typing import Any

from .agents import AgentRunResult


SECTION_PATTERN = re.compile(r"(?m)^##\s+([A-Za-z0-9 _-]+)\s*$")


def build_agent_prompt(task: dict[str, Any]) -> str:
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    spec = extract_section(description, "Spec") or description
    config = extract_section(description, "Config")

    parts = [
        f"Kanboard task #{task.get('id')}: {title}".strip(),
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


def build_summary_prompt(task: dict[str, Any], result: AgentRunResult, max_chars: int = 60000) -> str:
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    stdout = _truncate(result.stdout, max_chars)
    stderr = _truncate(result.stderr, max_chars)
    command = shlex.join(result.command)
    status = "succeeded" if result.ok else "failed"

    return f"""You just completed a Kanboard worker run.

Write a concise Kanboard card comment for a human reviewer.
Include the status, what happened, blockers if any, and next steps.
Return only the comment text. Do not include a preamble or markdown code fence.

Task id: {task.get("id")}
Task title: {title}
Run status: {status}
Exit code: {result.exit_code}
Command: {command}

Task description:
{description}

STDOUT:
{stdout}

STDERR:
{stderr}
"""


def _truncate(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    return value[:max_chars].rstrip() + f"\n\n[Truncated {omitted} characters.]"
