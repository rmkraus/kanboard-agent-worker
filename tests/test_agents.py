from __future__ import annotations

import sys
from pathlib import Path

from kanboard_agent_worker.agents import (
    CodexAgentRunner,
    SubprocessAgentRunner,
    final_agent_text_from_events,
    parse_jsonl_events,
    thread_id_from_events,
)
from kanboard_agent_worker.config import AgentConfig


def test_agent_runner_starts_process_in_configured_pwd(tmp_path: Path) -> None:
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    runner = SubprocessAgentRunner(
        AgentConfig(
            name="python",
            command=(sys.executable, "-c", "import os; print(os.getcwd())"),
            pwd=str(workdir),
        )
    )

    result = runner.run_task({"id": "123", "title": "Test"}, "prompt", thread_id=None)

    assert result.ok
    assert result.stdout.strip() == str(workdir)


def test_codex_create_thread_parses_thread_id(tmp_path: Path) -> None:
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
print(json.dumps({{"type": "thread.started", "thread_id": "thread-123"}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "hello"}}}}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    runner = CodexAgentRunner(AgentConfig(name="codex", command=(str(fake_codex),), pwd=str(tmp_path)))

    result = runner.create_thread({"id": "123", "title": "Test"})

    assert result.ok
    assert result.thread_id == "thread-123"
    assert result.final_text == "hello"


def test_parse_codex_jsonl_helpers() -> None:
    events = parse_jsonl_events(
        """
{"type":"thread.started","thread_id":"abc"}
not-json
{"type":"item.completed","item":{"type":"agent_message","text":"done"}}
"""
    )

    assert thread_id_from_events(events) == "abc"
    assert final_agent_text_from_events(events) == "done"
