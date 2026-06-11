"""Shared agent wrapper interfaces and execution result types."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import AgentConfig
from ..status import parse_kanboard_status


class AgentExecutionError(RuntimeError):
    """Raised when an agent command cannot be started."""


@dataclass(frozen=True)
class AgentExecResult:
    """Completed agent process output normalized for Kanboard consumption."""

    exit_code: int
    output: str
    stdout: str
    stderr: str
    command: tuple[str, ...]
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    thread_id: str | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        text = self.output if self.output.strip() else self.stdout
        parsed = parse_kanboard_status(text)
        object.__setattr__(self, "output", parsed.text)
        if self.status is None and parsed.status:
            object.__setattr__(self, "status", parsed.status)

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def card_text(self) -> str:
        """Return the visible text to post back to the Kanboard card."""

        if self.output.strip():
            return self.output.strip()
        if self.stdout.strip():
            clean_stdout = parse_kanboard_status(self.stdout).text
            if clean_stdout.strip():
                return clean_stdout.strip()
        if self.stderr.strip():
            clean_stderr = parse_kanboard_status(self.stderr).text
            if clean_stderr.strip():
                return clean_stderr.strip()
        return "Agent completed without output."


class AgentWrapper(Protocol):
    """Minimal interface implemented by concrete CLI agent adapters."""

    def create_thread_id(self, project_id: int | str, task_id: int | str) -> str:
        """Create or name an agent conversation for a Kanboard task."""

        ...

    def exec(self, thread_id: str, prompt: str) -> AgentExecResult:
        """Run one prompt in the given agent conversation."""

        ...


class BaseAgentWrapper:
    """Base class for wrappers that execute local subprocess commands."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def create_thread_id(self, project_id: int | str, task_id: int | str) -> str:
        """Return an empty thread id for stateless subprocess-style agents."""

        return ""

    def _run(self, command: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                cwd=self.config.pwd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = self._decode_timeout_output(exc.stdout)
            stderr = self._decode_timeout_output(exc.stderr)
            if stderr:
                stderr += "\n"
            stderr += f"Agent timed out after {self.config.timeout_seconds} seconds."
            return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)
        except OSError as exc:
            raise AgentExecutionError(f"Failed to start agent command {command!r}: {exc}") from exc

    @staticmethod
    def _decode_timeout_output(value: bytes | str | None) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode(errors="replace")
        return value
