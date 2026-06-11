"""Agent wrapper factory and public wrapper exports."""

from __future__ import annotations

from ..config import AgentConfig, AppConfig
from .acp import ClaudeAcpAgentWrapper, CodexAcpAgentWrapper
from .base import AgentExecResult, AgentExecutionError, AgentWrapper, BaseAgentWrapper
from .subprocess import SubprocessAgentWrapper

__all__ = [
    "AgentExecResult",
    "AgentExecutionError",
    "AgentWrapper",
    "BaseAgentWrapper",
    "ClaudeAcpAgentWrapper",
    "CodexAcpAgentWrapper",
    "SubprocessAgentWrapper",
    "create_agent_wrapper",
]


def create_agent_wrapper(config: AgentConfig, app_config: AppConfig | None = None) -> AgentWrapper:
    """Return the concrete wrapper selected by ``config.name``."""

    name = config.name.lower()
    if name == "codex":
        if app_config is None:
            raise AgentExecutionError("codex agent requires AppConfig so ACP tools can be configured")
        return CodexAcpAgentWrapper(config, app_config)
    if name == "claude":
        if app_config is None:
            raise AgentExecutionError("claude agent requires AppConfig so ACP tools can be configured")
        return ClaudeAcpAgentWrapper(config, app_config)
    return SubprocessAgentWrapper(config)
