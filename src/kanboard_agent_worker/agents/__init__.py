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


def create_agent_wrapper(
    config: AgentConfig,
    thread_id: str | None = None,
) -> AgentWrapper:
    name = config.name.lower()
    if name == "codex":
        return CodexAgentWrapper(config, thread_id=thread_id)
    if name == "claude":
        return ClaudeAgentWrapper(config, session_name=thread_id, session_exists=bool(thread_id))
    return SubprocessAgentWrapper(config)
