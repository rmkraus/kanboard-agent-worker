"""Agent wrapper factory and public wrapper exports."""

from __future__ import annotations

from ..config import AgentConfig
from .base import AgentExecResult, AgentExecutionError, AgentWrapper, BaseAgentWrapper
from .claude import ClaudeAgentWrapper
from .codex import CodexAgentWrapper
from .subprocess import SubprocessAgentWrapper

__all__ = [
    "AgentExecResult",
    "AgentExecutionError",
    "AgentWrapper",
    "BaseAgentWrapper",
    "ClaudeAgentWrapper",
    "CodexAgentWrapper",
    "SubprocessAgentWrapper",
    "create_agent_wrapper",
]


def create_agent_wrapper(config: AgentConfig) -> AgentWrapper:
    """Return the concrete wrapper selected by ``config.name``."""

    name = config.name.lower()
    if name == "codex":
        return CodexAgentWrapper(config)
    if name == "claude":
        return ClaudeAgentWrapper(config)
    return SubprocessAgentWrapper(config)
