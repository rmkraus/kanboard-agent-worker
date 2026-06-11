from __future__ import annotations

from ..config import AgentConfig
from .base import AgentExecResult, AgentExecutionError, BaseAgentWrapper


class ClaudeAgentWrapper(BaseAgentWrapper):
    def __init__(self, config: AgentConfig, session_name: str | None = None, session_exists: bool = False) -> None:
        super().__init__(config)
        self.session_name = session_name
        self.session_exists = session_exists

    def create_thread_id(self, project_id: int | str, task_id: int | str) -> str:
        session_name = f"kanboard-{project_id}-{task_id}"
        command = [self._executable(), "-n", session_name, "-p", "hello"]
        completed = self._run(command)
        if completed.returncode != 0:
            raise AgentExecutionError(f"Claude thread creation failed with exit code {completed.returncode}")
        self.session_name = session_name
        self.session_exists = True
        return session_name

    def exec(self, prompt: str) -> AgentExecResult:
        if not self.session_name:
            raise AgentExecutionError("Claude requires a session name")

        if self.session_exists:
            command = [self._executable(), "--resume", self.session_name, "-p", prompt]
        else:
            command = [self._executable(), "-n", self.session_name, "-p", prompt]
            self.session_exists = True

        completed = self._run(command)
        return AgentExecResult(
            exit_code=completed.returncode,
            output=completed.stdout.strip(),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=tuple(command),
            thread_id=self.session_name,
        )

    def _executable(self) -> str:
        return self.config.command[0] if self.config.command else "claude"
