from __future__ import annotations

from .base import AgentExecResult, AgentExecutionError, BaseAgentWrapper


class SubprocessAgentWrapper(BaseAgentWrapper):
    def exec(self, prompt: str) -> AgentExecResult:
        if not self.config.command:
            raise AgentExecutionError("agent.command is required for generic subprocess agents")
        command = list(self.config.command)
        input_text = prompt if self.config.pass_task_on_stdin else None
        completed = self._run(command, input_text=input_text)
        return AgentExecResult(
            exit_code=completed.returncode,
            output=completed.stdout.strip(),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=tuple(command),
        )
