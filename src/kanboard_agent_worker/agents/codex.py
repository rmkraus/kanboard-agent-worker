"""Codex CLI agent wrapper."""

from __future__ import annotations

import json
from typing import Any

from .base import AgentExecResult, AgentExecutionError, BaseAgentWrapper


class CodexAgentWrapper(BaseAgentWrapper):
    """Run work in a Codex CLI JSONL conversation."""

    def create_thread_id(self, project_id: int | str, task_id: int | str) -> str:
        command = [self._executable(), "exec", "--skip-git-repo-check", "--json", "-"]
        completed = self._run(command, input_text="hello")
        events = tuple(self._parse_jsonl_events(completed.stdout))
        thread_id = self._thread_id_from_events(events)
        if completed.returncode != 0:
            raise AgentExecutionError(f"Codex thread creation failed with exit code {completed.returncode}")
        if not thread_id:
            raise AgentExecutionError("Codex did not emit thread.started with a thread_id")
        return thread_id

    def exec(self, thread_id: str, prompt: str) -> AgentExecResult:
        if not thread_id:
            raise AgentExecutionError("Codex requires a thread id")

        command = [self._executable(), "exec", "resume", thread_id, "--skip-git-repo-check", "--json", "-"]
        completed = self._run(command, input_text=prompt)
        events = tuple(self._parse_jsonl_events(completed.stdout))
        emitted_thread_id = self._thread_id_from_events(events) or thread_id

        return AgentExecResult(
            exit_code=completed.returncode,
            output=self._output_from_jsonl(completed.stdout),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=tuple(command),
            events=events,
            thread_id=emitted_thread_id,
        )

    def _executable(self) -> str:
        return self.config.command[0] if self.config.command else "codex"

    @classmethod
    def _parse_jsonl_events(cls, output: str) -> list[dict[str, Any]]:
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

    @staticmethod
    def _thread_id_from_events(events: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str | None:
        for event in events:
            if event.get("type") == "thread.started" and event.get("thread_id"):
                return str(event["thread_id"])
        return None

    @staticmethod
    def _final_agent_text_from_events(events: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str | None:
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

    @classmethod
    def _output_from_jsonl(cls, stdout_jsonlines: str) -> str:
        events = cls._parse_jsonl_events(stdout_jsonlines)
        return cls._final_agent_text_from_events(events) or stdout_jsonlines.strip()
