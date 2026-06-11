from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import AgentConfig


class AgentExecutionError(RuntimeError):
    """Raised when an agent command cannot be started or parsed."""


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
    def exec(self, prompt: str) -> AgentExecResult:
        ...


def create_agent_wrapper(
    config: AgentConfig,
    thread_id: str | None = None,
    default_thread_id: str | None = None,
) -> AgentWrapper:
    name = config.name.lower()
    if name == "codex":
        return CodexAgentWrapper(config, thread_id=thread_id)
    if name == "claude":
        return ClaudeAgentWrapper(config, session_name=thread_id or default_thread_id, session_exists=bool(thread_id))
    return SubprocessAgentWrapper(config)


class BaseAgentWrapper:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config

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
            stdout = _decode_timeout_output(exc.stdout)
            stderr = _decode_timeout_output(exc.stderr)
            if stderr:
                stderr += "\n"
            stderr += f"Agent timed out after {self.config.timeout_seconds} seconds."
            return subprocess.CompletedProcess(command, 124, stdout=stdout, stderr=stderr)
        except OSError as exc:
            raise AgentExecutionError(f"Failed to start agent command {command!r}: {exc}") from exc


class CodexAgentWrapper(BaseAgentWrapper):
    def __init__(self, config: AgentConfig, thread_id: str | None = None) -> None:
        super().__init__(config)
        self.thread_id = thread_id

    def exec(self, prompt: str) -> AgentExecResult:
        if self.thread_id:
            command = [self._executable(), "exec", "resume", self.thread_id, "--skip-git-repo-check", "--json", "-"]
        else:
            command = [self._executable(), "exec", "--skip-git-repo-check", "--json", "-"]

        completed = self._run(command, input_text=prompt)
        events = tuple(parse_jsonl_events(completed.stdout))
        thread_id = thread_id_from_events(events) or self.thread_id
        if thread_id:
            self.thread_id = thread_id

        return AgentExecResult(
            exit_code=completed.returncode,
            output=_output_from_jsonl(completed.stdout),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=tuple(command),
            events=events,
            thread_id=thread_id,
        )

    def _executable(self) -> str:
        return self.config.command[0] if self.config.command else "codex"


class ClaudeAgentWrapper(BaseAgentWrapper):
    def __init__(self, config: AgentConfig, session_name: str | None = None, session_exists: bool = False) -> None:
        super().__init__(config)
        self.session_name = session_name
        self.session_exists = session_exists

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


def _output_from_jsonl(stdout_jsonlines: str) -> str:
    return final_agent_text_from_events(parse_jsonl_events(stdout_jsonlines)) or stdout_jsonlines.strip()


def _decode_timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
