from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kanboard_agent_worker.agents import (
    AcpAgent,
    AgentExecResult,
    AgentExecutionError,
    create_acp_agent,
)
from kanboard_agent_worker.agents.acp import KanboardAcpClient
from kanboard_agent_worker.config import AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings


def test_acp_client_terminal_runs_command(tmp_path: Path) -> None:
    async def run_terminal() -> None:
        client = KanboardAcpClient(tmp_path)
        terminal = await client.create_terminal(
            sys.executable,
            session_id="session-1",
            args=["-c", "print('ok')"],
        )

        exited = await client.wait_for_terminal_exit("session-1", terminal.terminal_id)
        output = await client.terminal_output("session-1", terminal.terminal_id)

        assert exited.exit_code == 0
        assert output.exit_status.exit_code == 0
        assert output.output.strip() == "ok"

    asyncio.run(run_terminal())


def test_create_acp_agent_uses_codex_default_command(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="codex")

    agent = create_acp_agent(config.agent, config)

    assert isinstance(agent, AcpAgent)
    assert agent.command == ("codex-acp",)


def test_create_acp_agent_uses_claude_default_command(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="claude")

    agent = create_acp_agent(config.agent, config)

    assert isinstance(agent, AcpAgent)
    assert agent.command == ("claude-agent-acp",)


def test_create_acp_agent_accepts_explicit_command(tmp_path: Path) -> None:
    command = ("npx", "-y", "@zed-industries/codex-acp")
    config = _app_config(tmp_path, agent_name="codex", command=command)

    agent = create_acp_agent(config.agent, config)

    assert isinstance(agent, AcpAgent)
    assert agent.command == command


def test_create_acp_agent_requires_command_for_unknown_agent(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="other")
    with pytest.raises(AgentExecutionError, match="agent.command is required"):
        create_acp_agent(config.agent, config)


def test_agent_exec_result_card_text_prefers_output() -> None:
    result = AgentExecResult(
        exit_code=0,
        output="Finished.\n",
        stdout="raw stdout",
        stderr="",
        command=("agent",),
    )

    assert result.card_text() == "Finished."


def test_agent_exec_result_uses_default_empty_output_message() -> None:
    result = AgentExecResult(
        exit_code=0,
        output="",
        stdout="",
        stderr="",
        command=("agent",),
    )

    assert result.card_text() == "Agent completed without output."


def _app_config(tmp_path: Path, agent_name: str, command: tuple[str, ...] = ()) -> AppConfig:
    return AppConfig(
        server=ServerConfig(user="codex-node1", token="secret", url="http://localhost:8080"),
        worker=WorkerSettings(max_concurrency=1, poll_interval=10),
        agent=AgentConfig(name=agent_name, command=command, pwd=str(tmp_path)),
        boards=(BoardConfig(id=1, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),),
    )
