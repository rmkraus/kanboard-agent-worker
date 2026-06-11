"""Shared agent execution result types."""

from __future__ import annotations

from dataclasses import dataclass


class AgentExecutionError(RuntimeError):
    """Raised when an agent turn cannot be completed."""


@dataclass(frozen=True)
class AgentExecResult:
    """Completed agent process output normalized for Kanboard consumption."""

    exit_code: int
    output: str
    stdout: str
    stderr: str
    command: tuple[str, ...]
    thread_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def card_text(self) -> str:
        """Return the visible text to post back to the Kanboard card."""

        if self.output.strip():
            return self.output.strip()
        if self.stdout.strip():
            return self.stdout.strip()
        if self.stderr.strip():
            return self.stderr.strip()
        return "Agent completed without output."
