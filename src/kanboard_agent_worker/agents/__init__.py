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
    worker_username: str | None = None,
    project_id: int | str | None = None,
    task_id: int | str | None = None,
) -> AgentWrapper:
    name = config.name.lower()
    if name == "codex":
        return CodexAgentWrapper(config, thread_id=thread_id)
    if name == "claude":
        session_name = thread_id or _create_session_id(ClaudeAgentWrapper, worker_username, project_id, task_id)
        return ClaudeAgentWrapper(config, session_name=session_name, session_exists=bool(thread_id))
    return SubprocessAgentWrapper(config)


def _create_session_id(
    wrapper_class: type[AgentWrapper],
    worker_username: str | None,
    project_id: int | str | None,
    task_id: int | str | None,
) -> str:
    if worker_username is None or project_id is None or task_id is None:
        raise AgentExecutionError("worker_username, project_id, and task_id are required to create a session id")
    return wrapper_class.create_session_id(worker_username, project_id, task_id)
