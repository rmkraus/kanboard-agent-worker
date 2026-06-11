from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..config import AgentConfig


class AgentExecutionError(RuntimeError):
    """Raised when an agent command cannot be started."""


@dataclass(frozen=True)
class AgentExecResult:
    exit_code: int
    output: str
    stdout: str
    stderr: str
    command: tuple[str, ...]
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    thread_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def card_text(self) -> str:
        if self.output.strip():
            return self.output.strip()
        if self.stdout.strip():
            return self.stdout.strip()
        return self.stderr.strip() or "Agent completed without output."


class AgentWrapper(Protocol):
    @classmethod
    def create_session_id(cls, worker_username: str, project_id: int | str, task_id: int | str) -> str:
        ...

    def exec(self, prompt: str) -> AgentExecResult:
        ...


class BaseAgentWrapper:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    @classmethod
    def create_session_id(cls, worker_username: str, project_id: int | str, task_id: int | str) -> str:
        return f"kanboard-{worker_username}-{project_id}-{task_id}"

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
