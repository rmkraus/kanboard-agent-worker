"""Thin JSON-RPC client and lookup helpers for Kanboard."""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from typing import Any

import requests


class KanboardError(RuntimeError):
    """Raised when Kanboard returns an HTTP or JSON-RPC error."""


@dataclass(frozen=True)
class ColumnLookup:
    """Resolved Kanboard columns required by the worker."""

    todo: dict[str, Any]
    working: dict[str, Any]
    blocked: dict[str, Any]
    done: dict[str, Any]


class KanboardClient:
    """Small Kanboard JSON-RPC client using user/PAT HTTP Basic auth."""

    def __init__(
        self,
        url: str,
        user: str,
        token: str,
        timeout: int = 30,
        retry_attempts: int = 8,
        retry_delay_seconds: float = 0.25,
    ) -> None:
        self.endpoint = normalize_endpoint(url)
        self.user = user
        self.token = token
        self.timeout = timeout
        self.retry_attempts = retry_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self._ids = itertools.count(1)
        self.session = requests.Session()
        self.session.auth = (user, token)

    def call(self, method: str, params: Any | None = None) -> Any:
        """Call a Kanboard JSON-RPC method and return its result value."""

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next(self._ids),
        }
        if params is not None:
            payload["params"] = params

        for attempt in range(1, self.retry_attempts + 1):
            response = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
            try:
                response.raise_for_status()
            except requests.HTTPError as exc:
                if _is_database_locked_error(response.text) and attempt < self.retry_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise KanboardError(f"Kanboard HTTP {response.status_code}: {response.text}") from exc

            data = response.json()
            if "error" in data:
                error = data["error"]
                if _is_database_locked_error(error) and attempt < self.retry_attempts:
                    self._sleep_before_retry(attempt)
                    continue
                raise KanboardError(f"Kanboard JSON-RPC error in {method}: {error}")
            return data.get("result")

        raise KanboardError(f"Kanboard JSON-RPC retry attempts exhausted in {method}")

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(self.retry_delay_seconds * attempt)

    def get_me(self) -> dict[str, Any]:
        result = self.call("getMe")
        if not result:
            raise KanboardError("getMe returned no user data")
        return result

    def get_board(self, project_id: int | str) -> list[dict[str, Any]]:
        result = self.call("getBoard", [_coerce_id(project_id)])
        if result is False or result is None:
            raise KanboardError(f"getBoard failed for project {project_id}")
        return result

    def get_columns(self, project_id: int | str) -> list[dict[str, Any]]:
        result = self.call("getColumns", [_coerce_id(project_id)])
        if result is False or result is None:
            raise KanboardError(f"getColumns failed for project {project_id}")
        return result

    def get_task(self, task_id: int | str) -> dict[str, Any]:
        result = self.call("getTask", {"task_id": _coerce_id(task_id)})
        if not result:
            raise KanboardError(f"getTask failed for task {task_id}")
        return result

    def get_user_by_name(self, username: str) -> dict[str, Any]:
        result = self.call("getUserByName", {"username": username})
        if not result:
            raise KanboardError(f"getUserByName failed for user {username!r}")
        return result

    def get_all_comments(self, task_id: int | str) -> list[dict[str, Any]]:
        result = self.call("getAllComments", {"task_id": _coerce_id(task_id)})
        if result is False or result is None:
            raise KanboardError(f"getAllComments failed for task {task_id}")
        return result

    def create_comment(self, task_id: int | str, user_id: int | str, content: str) -> int:
        result = self.call(
            "createComment",
            {
                "task_id": _coerce_id(task_id),
                "user_id": _coerce_id(user_id),
                "content": content,
            },
        )
        if result is False or result is None:
            raise KanboardError(f"createComment failed for task {task_id}")
        return int(result)

    def update_task_description(self, task_id: int | str, description: str) -> None:
        result = self.call("updateTask", {"id": _coerce_id(task_id), "description": description})
        if result is not True:
            raise KanboardError(f"updateTask failed for task {task_id}")

    def get_task_metadata(self, task_id: int | str) -> dict[str, str]:
        result = self.call("getTaskMetadata", [_coerce_id(task_id)])
        if result is False or result is None:
            return {}
        if isinstance(result, dict):
            return {str(key): str(value) for key, value in result.items()}
        if isinstance(result, list):
            metadata: dict[str, str] = {}
            for item in result:
                if isinstance(item, dict):
                    metadata.update({str(key): str(value) for key, value in item.items()})
            return metadata
        return {}

    def get_task_metadata_by_name(self, task_id: int | str, name: str) -> str:
        result = self.call("getTaskMetadataByName", [_coerce_id(task_id), name])
        if result is False or result is None:
            return ""
        return str(result)

    def save_task_metadata(self, task_id: int | str, values: dict[str, str]) -> None:
        result = self.call("saveTaskMetadata", [_coerce_id(task_id), values])
        if result is not True:
            raise KanboardError(f"saveTaskMetadata failed for task {task_id}")

    def get_all_subtasks(self, task_id: int | str) -> list[dict[str, Any]]:
        result = self.call("getAllSubtasks", {"task_id": _coerce_id(task_id)})
        if result is False or result is None:
            raise KanboardError(f"getAllSubtasks failed for task {task_id}")
        return result

    def create_subtask(
        self,
        task_id: int | str,
        title: str,
        user_id: int | str = 0,
        status: int = 0,
    ) -> int:
        result = self.call(
            "createSubtask",
            {
                "task_id": _coerce_id(task_id),
                "title": title,
                "user_id": _coerce_id(user_id),
                "status": status,
            },
        )
        if result is False or result is None:
            raise KanboardError(f"createSubtask failed for task {task_id}")
        return int(result)

    def update_subtask(
        self,
        subtask_id: int | str,
        task_id: int | str,
        *,
        title: str | None = None,
        user_id: int | str | None = None,
        status: int | None = None,
    ) -> None:
        params: dict[str, Any] = {
            "id": _coerce_id(subtask_id),
            "task_id": _coerce_id(task_id),
        }
        if title is not None:
            params["title"] = title
        if user_id is not None:
            params["user_id"] = _coerce_id(user_id)
        if status is not None:
            params["status"] = status

        result = self.call("updateSubtask", params)
        if result is not True:
            raise KanboardError(f"updateSubtask failed for subtask {subtask_id}")

    def has_subtask_timer(self, subtask_id: int | str, user_id: int | str) -> bool:
        return bool(self.call("hasSubtaskTimer", [_coerce_id(subtask_id), _coerce_id(user_id)]))

    def start_subtask_timer(self, subtask_id: int | str, user_id: int | str) -> None:
        result = self.call("setSubtaskStartTime", [_coerce_id(subtask_id), _coerce_id(user_id)])
        if result is not True:
            raise KanboardError(f"setSubtaskStartTime failed for subtask {subtask_id}")

    def stop_subtask_timer(self, subtask_id: int | str, user_id: int | str) -> None:
        result = self.call("setSubtaskEndTime", [_coerce_id(subtask_id), _coerce_id(user_id)])
        if result is not True:
            raise KanboardError(f"setSubtaskEndTime failed for subtask {subtask_id}")

    def move_task_to_column(
        self,
        project_id: int | str,
        task_id: int | str,
        column_id: int | str,
        swimlane_id: int | str = 0,
        position: int = 1,
    ) -> None:
        result = self.call(
            "moveTaskPosition",
            {
                "project_id": _coerce_id(project_id),
                "task_id": _coerce_id(task_id),
                "column_id": _coerce_id(column_id),
                "position": position,
                "swimlane_id": _coerce_id(swimlane_id),
            },
        )
        if result is not True:
            raise KanboardError(f"moveTaskPosition failed for task {task_id}")


def normalize_endpoint(url: str) -> str:
    """Return a URL that points directly at Kanboard's JSON-RPC endpoint."""

    clean = url.rstrip("/")
    if clean.endswith("/jsonrpc.php"):
        return clean
    return f"{clean}/jsonrpc.php"


def column_lookup(columns: list[dict[str, Any]], names: dict[str, str]) -> ColumnLookup:
    """Resolve configured column names against Kanboard's column records."""

    by_title = {str(column.get("title")): column for column in columns}
    missing = [label for label, title in names.items() if title not in by_title]
    if missing:
        expected = ", ".join(f"{label}={names[label]!r}" for label in missing)
        available = ", ".join(sorted(by_title))
        raise KanboardError(f"Missing configured columns: {expected}. Available columns: {available}")

    return ColumnLookup(
        todo=by_title[names["todo"]],
        working=by_title[names["working"]],
        blocked=by_title[names["blocked"]],
        done=by_title[names["done"]],
    )


def _coerce_id(value: int | str) -> int | str:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return value


def _is_database_locked_error(value: Any) -> bool:
    if isinstance(value, dict):
        value = value.get("message", "")
    return "database is locked" in str(value).casefold()
