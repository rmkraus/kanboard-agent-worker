"""Agent wrapper factory and public wrapper exports."""

from __future__ import annotations

from ..config import AgentConfig
from ..config import AppConfig
from .acp import ClaudeAcpAgentWrapper, CodexAcpAgentWrapper
from .base import AgentExecResult, AgentExecutionError, AgentWrapper, BaseAgentWrapper
from .claude import ClaudeAgentWrapper
from .codex import CodexAgentWrapper
from .subprocess import SubprocessAgentWrapper

__all__ = [
    "AgentExecResult",
    "AgentExecutionError",
    "AgentWrapper",
    "BaseAgentWrapper",
    "ClaudeAcpAgentWrapper",
    "ClaudeAgentWrapper",
    "CodexAcpAgentWrapper",
    "CodexAgentWrapper",
    "SubprocessAgentWrapper",
    "create_agent_wrapper",
]


def create_agent_wrapper(config: AgentConfig, app_config: AppConfig | None = None) -> AgentWrapper:
    """Return the concrete wrapper selected by ``config.name``."""

    name = config.name.lower()
    if name == "codex":
        if app_config is None:
            return CodexAgentWrapper(config)
        return CodexAcpAgentWrapper(config, app_config)
    if name == "claude":
        if app_config is None:
            return ClaudeAgentWrapper(config)
        return ClaudeAcpAgentWrapper(config, app_config)
    return SubprocessAgentWrapper(config)
