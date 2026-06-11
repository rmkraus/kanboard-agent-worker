from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import AgentConfig


class AgentExecutionError(RuntimeError):
    """Raised when an agent command cannot be started or parsed."""


@dataclass(frozen=True)
class AgentRunResult:
    exit_code: int
    stdout: str
    stderr: str
    command: tuple[str, ...]
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    thread_id: str | None = None
    final_text: str | None = None

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def summary_text(self) -> str:
        if self.final_text:
            return self.final_text.strip()
        if self.stdout.strip():
            return self.stdout.strip()
        return self.stderr.strip()

    def transcript(self) -> str:
        parts = []
        if self.stdout.strip():
            parts.extend(["STDOUT:", self.stdout.strip()])
        if self.stderr.strip():
            parts.extend(["STDERR:", self.stderr.strip()])
        return "\n".join(parts).strip()


class AgentRunner(Protocol):
    uses_threads: bool

    def create_thread(self, task: dict[str, Any]) -> AgentRunResult:
        ...

    def run_task(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        ...

    def summarize_run(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        ...


def create_agent_runner(config: AgentConfig) -> AgentRunner:
    name = config.name.lower()
    if name == "codex":
        return CodexAgentRunner(config)
    if name == "claude":
        return ClaudeAgentRunner(config)
    return SubprocessAgentRunner(config)


class BaseAgentRunner:
    uses_threads = False

    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def create_thread(self, task: dict[str, Any]) -> AgentRunResult:
        raise AgentExecutionError(f"{self.config.name} does not support worker-managed threads yet")

    def _run(
        self,
        command: list[str],
        input_text: str | None = None,
        parse_jsonl: bool = False,
    ) -> AgentRunResult:
        try:
            completed = subprocess.run(
                command,
                cwd=self.config.pwd,
                input=input_text,
                capture_output=True,
                text=True,
                timeout=self.config.timeout_seconds,
                check=False,
            )
            stdout = completed.stdout
            stderr = completed.stderr
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr)
            if stderr:
                stderr += "\n"
            stderr += f"Agent timed out after {self.config.timeout_seconds} seconds."
            exit_code = 124
        except OSError as exc:
            raise AgentExecutionError(f"Failed to start agent command {command!r}: {exc}") from exc

        events = tuple(parse_jsonl_events(stdout)) if parse_jsonl else ()
        return AgentRunResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=tuple(command),
            events=events,
            thread_id=thread_id_from_events(events),
            final_text=final_agent_text_from_events(events),
        )


class CodexAgentRunner(BaseAgentRunner):
    uses_threads = True

    def create_thread(self, task: dict[str, Any]) -> AgentRunResult:
        result = self._run(
            [self._executable(), "exec", "--skip-git-repo-check", "--json", "-"],
            input_text="hello",
            parse_jsonl=True,
        )
        if not result.ok:
            raise AgentExecutionError(f"Codex thread bootstrap failed with exit code {result.exit_code}")
        if not result.thread_id:
            raise AgentExecutionError("Codex did not emit thread.started with a thread_id")
        return result

    def run_task(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        if not thread_id:
            raise AgentExecutionError("Codex requires a thread_id")
        return self._run(
            [self._executable(), "exec", "resume", thread_id, "--skip-git-repo-check", "--json", "-"],
            input_text=prompt,
            parse_jsonl=True,
        )

    def summarize_run(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        return self.run_task(task, prompt, thread_id)

    def _executable(self) -> str:
        return self.config.command[0] if self.config.command else "codex"


class ClaudeAgentRunner(BaseAgentRunner):
    uses_threads = True

    def create_thread(self, task: dict[str, Any]) -> AgentRunResult:
        thread_id = str(uuid.uuid4())
        result = self._run([self._executable(), "-p", "--session-id", thread_id, "hello"])
        if not result.ok:
            raise AgentExecutionError(f"Claude thread bootstrap failed with exit code {result.exit_code}")
        return AgentRunResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            command=result.command,
            thread_id=thread_id,
            final_text=result.summary_text(),
        )

    def run_task(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        if not thread_id:
            raise AgentExecutionError("Claude requires a thread_id")
        return self._run([self._executable(), "-p", "--resume", thread_id, prompt])

    def summarize_run(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        return self.run_task(task, prompt, thread_id)

    def _executable(self) -> str:
        return self.config.command[0] if self.config.command else "claude"


class SubprocessAgentRunner(BaseAgentRunner):
    def run_task(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        if not self.config.command:
            raise AgentExecutionError("agent.command is required for generic subprocess agents")
        command = [
            part.format(
                task_id=task.get("id", ""),
                task_title=task.get("title", ""),
                thread_id=thread_id or "",
            )
            for part in self.config.command
        ]
        input_text = prompt if self.config.pass_task_on_stdin else None
        return self._run(command, input_text=input_text)

    def summarize_run(self, task: dict[str, Any], prompt: str, thread_id: str | None) -> AgentRunResult:
        return self.run_task(task, prompt, thread_id)


def parse_jsonl_events(output: str) -> list[dict[str, Any]]:
    events = []
    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def thread_id_from_events(events: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str | None:
    for event in events:
        if event.get("type") == "thread.started" and event.get("thread_id"):
            return str(event["thread_id"])
    return None


def final_agent_text_from_events(events: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str | None:
    messages = []
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message" and item.get("text"):
            messages.append(str(item["text"]))
    if not messages:
        return None
    return messages[-1]


def _decode_timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
