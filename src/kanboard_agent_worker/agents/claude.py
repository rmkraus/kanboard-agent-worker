from __future__ import annotations

from .base import AgentExecResult, AgentExecutionError, BaseAgentWrapper


class ClaudeAgentWrapper(BaseAgentWrapper):
    def create_thread_id(self, project_id: int | str, task_id: int | str) -> str:
        session_name = f"kanboard-{project_id}-{task_id}"
        command = [self._executable(), "-n", session_name, "-p", "hello"]
        completed = self._run(command)
        if completed.returncode != 0:
            raise AgentExecutionError(f"Claude thread creation failed with exit code {completed.returncode}")
        return session_name

    def exec(self, thread_id: str, prompt: str) -> AgentExecResult:
        if not thread_id:
            raise AgentExecutionError("Claude requires a thread id")
        command = [self._executable(), "--resume", thread_id, "-p", prompt]
        completed = self._run(command)
        return AgentExecResult(
            exit_code=completed.returncode,
            output=completed.stdout.strip(),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=tuple(command),
            thread_id=thread_id,
        )

    def _executable(self) -> str:
        return self.config.command[0] if self.config.command else "claude"
