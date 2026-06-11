from __future__ import annotations

import re
from typing import Any


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
