from __future__ import annotations

import json
from typing import Any

from ..config import AgentConfig
from .base import AgentExecResult, BaseAgentWrapper


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
        events = tuple(self._parse_jsonl_events(completed.stdout))
        thread_id = self._thread_id_from_events(events) or self.thread_id
        if thread_id:
            self.thread_id = thread_id

        return AgentExecResult(
            exit_code=completed.returncode,
            output=self._output_from_jsonl(completed.stdout),
            stdout=completed.stdout,
            stderr=completed.stderr,
            command=tuple(command),
            events=events,
            thread_id=thread_id,
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
