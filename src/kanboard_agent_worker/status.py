"""Parsing for agent-emitted Kanboard routing markers."""

from __future__ import annotations

import re
from dataclasses import dataclass

DONE_STATUS = "done"
BLOCKED_STATUS = "blocked"
VALID_KANBOARD_STATUSES = frozenset({DONE_STATUS, BLOCKED_STATUS})
STATUS_LINE_PATTERN = re.compile(r"(?im)^[ \t]*KANBOARD_STATUS[ \t]*:[ \t]*([A-Za-z_-]+)[ \t]*$")


@dataclass(frozen=True)
class ParsedKanboardStatus:
    """Agent output split into a routing status and visible text."""

    status: str | None
    text: str


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
