from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from kanboard_agent_worker.agents import (
    AgentExecResult,
    AgentExecutionError,
    ClaudeAcpAgentWrapper,
    CodexAcpAgentWrapper,
    SubprocessAgentWrapper,
    create_agent_wrapper,
)
from kanboard_agent_worker.agents.acp import KanboardAcpClient
from kanboard_agent_worker.config import AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings


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

    result = wrapper.exec("", "prompt")

    assert result.ok
    assert result.output == str(workdir)


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


@pytest.mark.parametrize("agent_name", ["codex", "claude"])
def test_wrapper_factory_requires_app_config_for_builtin_acp_agents(tmp_path: Path, agent_name: str) -> None:
    with pytest.raises(AgentExecutionError, match="requires AppConfig"):
        create_agent_wrapper(AgentConfig(name=agent_name, command=(), pwd=str(tmp_path)))


def test_wrapper_factory_uses_acp_when_app_config_is_available(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="codex")

    wrapper = create_agent_wrapper(config.agent, config)

    assert isinstance(wrapper, CodexAcpAgentWrapper)


def test_codex_acp_wrapper_ignores_legacy_codex_command(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="codex", command=("codex",))
    wrapper = create_agent_wrapper(config.agent, config)

    assert isinstance(wrapper, CodexAcpAgentWrapper)
    assert wrapper._command() == ("codex-acp",)


def test_claude_acp_wrapper_ignores_legacy_claude_command(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="claude", command=("claude",))
    wrapper = create_agent_wrapper(config.agent, config)

    assert isinstance(wrapper, ClaudeAcpAgentWrapper)
    assert wrapper._command() == ("claude-agent-acp",)


def test_codex_acp_wrapper_accepts_explicit_acp_command(tmp_path: Path) -> None:
    command = ("npx", "-y", "@zed-industries/codex-acp")
    config = _app_config(tmp_path, agent_name="codex", command=command)
    wrapper = create_agent_wrapper(config.agent, config)

    assert isinstance(wrapper, CodexAcpAgentWrapper)
    assert wrapper._command() == command


def test_claude_acp_wrapper_accepts_explicit_acp_command(tmp_path: Path) -> None:
    command = ("npx", "-y", "@agentclientprotocol/claude-agent-acp@0.44.0")
    config = _app_config(tmp_path, agent_name="claude", command=command)
    wrapper = create_agent_wrapper(config.agent, config)

    assert isinstance(wrapper, ClaudeAcpAgentWrapper)
    assert wrapper._command() == command


def test_wrapper_factory_uses_claude_acp_when_app_config_is_available(tmp_path: Path) -> None:
    config = _app_config(tmp_path, agent_name="claude")

    wrapper = create_agent_wrapper(config.agent, config)

    assert isinstance(wrapper, ClaudeAcpAgentWrapper)


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
