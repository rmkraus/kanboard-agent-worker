from __future__ import annotations

from pathlib import Path

import pytest

from kanboard_agent_worker.agents import AcpSession, AcpSessionError
from kanboard_agent_worker.config import AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings


def test_acp_session_uses_codex_default_command(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="codex")

    assert AcpSession.command_for_config(config.agent) == ("codex-acp",)


def test_acp_session_uses_claude_default_command(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="claude")

    assert AcpSession.command_for_config(config.agent) == ("claude-agent-acp",)


def test_acp_session_accepts_explicit_command(tmp_path: Path) -> None:
    command = ("npx", "-y", "@zed-industries/codex-acp")
    config = _app_config(tmp_path, agent_name="codex", command=command)

    assert AcpSession.command_for_config(config.agent) == command


def test_acp_session_requires_command_for_unknown_agent(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="other")

    with pytest.raises(AcpSessionError, match="agent.command is required"):
        AcpSession.command_for_config(config.agent)


def _app_config(tmp_path: Path, agent_name: str, command: tuple[str, ...] = ()) -> AppConfig:
    return AppConfig(
        server=ServerConfig(user="codex-node1", token="secret", url="http://localhost:8080"),
        worker=WorkerSettings(max_concurrency=1, poll_interval=10),
        agent=AgentConfig(name=agent_name, command=command, pwd=str(tmp_path)),
        boards=(BoardConfig(id=1, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),),
    )
