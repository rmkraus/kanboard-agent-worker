from __future__ import annotations

import sys
from pathlib import Path

from kanboard_agent_worker.agents import (
    ClaudeAgentWrapper,
    CodexAgentWrapper,
    SubprocessAgentWrapper,
)
from kanboard_agent_worker.config import AgentConfig


def test_subprocess_wrapper_starts_process_in_configured_pwd(tmp_path: Path) -> None:
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    wrapper = SubprocessAgentWrapper(
        AgentConfig(
            name="python",
            command=(sys.executable, "-c", "import os; print(os.getcwd())"),
            pwd=str(workdir),
        )
    )

    result = wrapper.exec("prompt")

    assert result.ok
    assert result.output == str(workdir)


def test_codex_wrapper_extracts_last_message_and_thread_id(tmp_path: Path) -> None:
    fake_codex = tmp_path / "codex"
    fake_codex.write_text(
        f"""#!{sys.executable}
import json
print(json.dumps({{"type": "thread.started", "thread_id": "thread-123"}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "hello"}}}}))
print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "done"}}}}))
""",
        encoding="utf-8",
    )
    fake_codex.chmod(0o755)
    wrapper = CodexAgentWrapper(AgentConfig(name="codex", command=(str(fake_codex),), pwd=str(tmp_path)))

    result = wrapper.exec("prompt")

    assert result.ok
    assert result.thread_id == "thread-123"
    assert result.output == "done"


def test_claude_wrapper_creates_then_resumes_named_session(tmp_path: Path) -> None:
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        f"""#!{sys.executable}
import sys
print(" ".join(sys.argv[1:]))
""",
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    wrapper = ClaudeAgentWrapper(
        AgentConfig(name="claude", command=(str(fake_claude),), pwd=str(tmp_path)),
        session_name="ryan",
    )

    first = wrapper.exec("hello")
    second = wrapper.exec("HELLO")

    assert first.output == "-n ryan -p hello"
    assert second.output == "--resume ryan -p HELLO"
    assert second.thread_id == "ryan"


def test_parse_codex_jsonl_helpers() -> None:
    events = CodexAgentWrapper._parse_jsonl_events(
        """
{"type":"thread.started","thread_id":"abc"}
not-json
{"type":"item.completed","item":{"type":"agent_message","text":"done"}}
"""
    )

    assert CodexAgentWrapper._thread_id_from_events(events) == "abc"
    assert CodexAgentWrapper._final_agent_text_from_events(events) == "done"
