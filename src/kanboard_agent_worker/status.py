"""Parsing for agent-emitted Kanboard routing markers."""

from __future__ import annotations

import re
from dataclasses import dataclass

DONE_STATUS = "done"
BLOCKED_STATUS = "blocked"
VALID_KANBOARD_STATUSES = frozenset({DONE_STATUS, BLOCKED_STATUS})
STATUS_LINE_PATTERN = re.compile(r"(?im)^[ \t]*KANBOARD_STATUS[ \t]*:[ \t]*([A-Za-z_-]+)[ \t]*$")
SUBTASK_LINE_PATTERN = re.compile(r"(?i)^[ \t]*KANBAN_SUBTASK[ \t]+(.+?)[ \t]*$")
SUBTASK_AGENT_LINE_PATTERN = re.compile(r"(?i)^[ \t]*KANBAN_SUBTASK_(?:AGENT|AGET)[ \t]+([A-Za-z0-9_.-]+)[ \t]*$")


@dataclass(frozen=True)
class ParsedKanboardStatus:
    """Agent output split into a routing status and visible text."""

    status: str | None
    text: str


@dataclass(frozen=True)
class SubtaskDirective:
    """A subtask creation request emitted by an agent."""

    title: str
    assignee: str | None = None


@dataclass(frozen=True)
class ParsedAgentDirectives:
    """Agent output split into visible text and worker directives."""

    text: str
    subtasks: tuple[SubtaskDirective, ...]


def parse_kanboard_status(text: str) -> ParsedKanboardStatus:
    """Extract the last valid KANBOARD_STATUS marker and strip all markers."""

    matches = list(STATUS_LINE_PATTERN.finditer(text))
    status = None
    if matches:
        candidate = matches[-1].group(1).strip().casefold()
        if candidate in VALID_KANBOARD_STATUSES:
            status = candidate

    clean = STATUS_LINE_PATTERN.sub("", text).strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return ParsedKanboardStatus(status=status, text=clean)


def parse_agent_directives(text: str) -> ParsedAgentDirectives:
    """Extract Kanboard worker directives and strip them from visible text."""

    subtasks: list[SubtaskDirective] = []
    visible_lines: list[str] = []

    for line in text.splitlines():
        subtask_match = SUBTASK_LINE_PATTERN.match(line)
        if subtask_match:
            title = subtask_match.group(1).strip()
            if title:
                subtasks.append(SubtaskDirective(title=title))
            continue

        assignee_match = SUBTASK_AGENT_LINE_PATTERN.match(line)
        if assignee_match:
            if subtasks:
                subtasks[-1] = SubtaskDirective(title=subtasks[-1].title, assignee=assignee_match.group(1).strip())
            continue

        visible_lines.append(line)

    clean = "\n".join(visible_lines).strip()
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return ParsedAgentDirectives(text=clean, subtasks=tuple(subtasks))
