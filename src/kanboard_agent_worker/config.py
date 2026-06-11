"""Configuration loading and validation for the Kanboard worker."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """Raised when the worker configuration is invalid."""


@dataclass(frozen=True)
class ServerConfig:
    """Kanboard server credentials for a single worker identity."""

    user: str
    token: str
    url: str


@dataclass(frozen=True)
class BoardConfig:
    """Board-specific column mapping used by the worker lifecycle."""

    id: int | str
    todo: str
    working: str
    blocked: str
    done: str


@dataclass(frozen=True)
class AgentConfig:
    """Local CLI agent execution settings."""

    name: str
    command: tuple[str, ...]
    pwd: str
    system_prompt: str = ""
    timeout_seconds: int = 3600
    pass_task_on_stdin: bool = True


@dataclass(frozen=True)
class WorkerSettings:
    """Polling and concurrency settings for one worker process."""

    max_concurrency: int = 1
    poll_interval: int = 10


@dataclass(frozen=True)
class RosterEntry:
    """One agent that can receive generated subtasks."""

    name: str
    description: str = ""


@dataclass(frozen=True)
class AppConfig:
    """Fully validated application configuration."""

    server: ServerConfig
    worker: WorkerSettings
    agent: AgentConfig
    boards: tuple[BoardConfig, ...]
    roster: tuple[RosterEntry, ...] = ()


def load_config(path: str | Path) -> AppConfig:
    """Load a YAML config file and apply supported environment overrides."""

    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")
    config_dir = config_path.resolve().parent

    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}

    if not isinstance(raw, dict):
        raise ConfigError("Config root must be a mapping")

    expanded = _expand_env(raw)
    return _from_mapping(expanded, config_dir)


def _from_mapping(raw: dict[str, Any], config_dir: Path) -> AppConfig:
    server_raw = _required_mapping(raw, "server")
    worker_raw = raw.get("worker") or {}
    agent_raw = raw.get("agent") or {}
    boards_raw = raw.get("boards")
    roster_raw = raw.get("roster") or []

    if not isinstance(worker_raw, dict):
        raise ConfigError("worker must be a mapping")
    if not isinstance(agent_raw, dict):
        raise ConfigError("agent must be a mapping")
    if not isinstance(boards_raw, list) or not boards_raw:
        raise ConfigError("boards must be a non-empty list")

    server = ServerConfig(
        user=_env_or_value("KANBOARD_USER", server_raw.get("user"), "server.user"),
        token=_env_or_value("KANBOARD_TOKEN", server_raw.get("token"), "server.token"),
        url=_env_or_value("KANBOARD_URL", server_raw.get("url"), "server.url"),
    )

    worker = WorkerSettings(
        max_concurrency=_positive_int(
            os.getenv("WORKER_MAX_CONCURRENCY", worker_raw.get("max_concurrency", 1)),
            "worker.max_concurrency",
        ),
        poll_interval=_positive_int(
            os.getenv("WORKER_POLL_INTERVAL", worker_raw.get("poll_interval", 10)),
            "worker.poll_interval",
        ),
    )

    agent = AgentConfig(
        name=str(agent_raw.get("name", "local")),
        command=_command_tuple(agent_raw.get("command")),
        pwd=_agent_pwd(agent_raw, config_dir),
        system_prompt=str(agent_raw.get("system_prompt", "")).strip(),
        timeout_seconds=_positive_int(agent_raw.get("timeout_seconds", 3600), "agent.timeout_seconds"),
        pass_task_on_stdin=bool(agent_raw.get("pass_task_on_stdin", True)),
    )

    boards = tuple(_board_from_mapping(item, index) for index, item in enumerate(boards_raw))
    roster = tuple(_roster_entry_from_mapping(item, index) for index, item in enumerate(roster_raw))
    return AppConfig(server=server, worker=worker, agent=agent, boards=boards, roster=roster)


def _required_mapping(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a mapping")
    return value


def _board_from_mapping(raw: Any, index: int) -> BoardConfig:
    if not isinstance(raw, dict):
        raise ConfigError(f"boards[{index}] must be a mapping")

    missing = [key for key in ("id", "todo", "working", "blocked", "done") if raw.get(key) in (None, "")]
    if missing:
        raise ConfigError(f"boards[{index}] missing required fields: {', '.join(missing)}")

    return BoardConfig(
        id=raw["id"],
        todo=str(raw["todo"]),
        working=str(raw["working"]),
        blocked=str(raw["blocked"]),
        done=str(raw["done"]),
    )


def _roster_entry_from_mapping(raw: Any, index: int) -> RosterEntry:
    if not isinstance(raw, dict):
        raise ConfigError(f"roster[{index}] must be a mapping")
    if raw.get("name") in (None, ""):
        raise ConfigError(f"roster[{index}] missing required field: name")

    return RosterEntry(name=str(raw["name"]), description=str(raw.get("description", "")).strip())


def _env_or_value(env_name: str, value: Any, path: str) -> str:
    value = os.getenv(env_name, value)
    if value in (None, ""):
        raise ConfigError(f"{path} is required")
    return str(value)


def _positive_int(value: Any, path: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{path} must be an integer") from exc
    if parsed < 1:
        raise ConfigError(f"{path} must be >= 1")
    return parsed


def _command_tuple(value: Any) -> tuple[str, ...]:
    env_command = os.getenv("AGENT_COMMAND")
    if env_command:
        return tuple(shlex.split(env_command))

    if isinstance(value, str):
        command = tuple(shlex.split(value))
    elif isinstance(value, list):
        command = tuple(str(part) for part in value)
    elif value is None:
        command = ()
    else:
        command = ()

    return command


def _agent_pwd(agent_raw: dict[str, Any], config_dir: Path) -> str:
    raw_value = os.getenv("AGENT_PWD", agent_raw.get("pwd", agent_raw.get("cwd", ".")))
    if raw_value in (None, ""):
        raise ConfigError("agent.pwd must not be empty")

    path = Path(str(raw_value)).expanduser()
    if not path.is_absolute():
        path = config_dir / path
    path = path.resolve()
    if not path.is_dir():
        raise ConfigError(f"agent.pwd must be an existing directory: {path}")
    return str(path)


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value
