from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any

import requests


class KanboardError(RuntimeError):
    """Raised when Kanboard returns an HTTP or JSON-RPC error."""


@dataclass(frozen=True)
class ColumnLookup:
    todo: dict[str, Any]
    working: dict[str, Any]
    blocked: dict[str, Any]
    done: dict[str, Any]


class KanboardClient:
    def __init__(self, url: str, user: str, token: str, timeout: int = 30) -> None:
        self.endpoint = normalize_endpoint(url)
        self.user = user
        self.token = token
        self.timeout = timeout
        self._ids = itertools.count(1)
        self.session = requests.Session()
        self.session.auth = (user, token)

    def call(self, method: str, params: Any | None = None) -> Any:
        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "method": method,
            "id": next(self._ids),
        }
        if params is not None:
            payload["params"] = params

        response = self.session.post(self.endpoint, json=payload, timeout=self.timeout)
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise KanboardError(f"Kanboard HTTP {response.status_code}: {response.text}") from exc

        data = response.json()
        if "error" in data:
            error = data["error"]
            raise KanboardError(f"Kanboard JSON-RPC error in {method}: {error}")
        return data.get("result")

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
    clean = url.rstrip("/")
    if clean.endswith("/jsonrpc.php"):
        return clean
    return f"{clean}/jsonrpc.php"


def column_lookup(columns: list[dict[str, Any]], names: dict[str, str]) -> ColumnLookup:
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
