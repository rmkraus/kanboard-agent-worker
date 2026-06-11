"""MCP tools exposed to ACP agents for Kanboard operations."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .kanboard import KanboardClient, KanboardError

TOOL_INSTRUCTIONS = """Kanboard task tools. Use these tools for task files,
subtasks, and moving cards between configured board columns."""

mcp = FastMCP("kanboard", instructions=TOOL_INSTRUCTIONS)


def _client() -> KanboardClient:
    return KanboardClient(
        os.environ["KANBOARD_URL"],
        os.environ["KANBOARD_USER"],
        os.environ["KANBOARD_TOKEN"],
    )


def _board(project_id: int | str) -> dict[str, Any]:
    boards = json.loads(os.environ.get("KANBOARD_WORKER_BOARDS", "[]"))
    for board in boards:
        if str(board.get("id")) == str(project_id):
            return board
    raise KanboardError(f"Project {project_id} is not configured for this worker")


def _root() -> Path:
    return Path(os.environ.get("KANBOARD_AGENT_PWD", ".")).expanduser().resolve()


def _path(path: str) -> Path:
    root = _root()
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = root / target
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise KanboardError(f"Path outside configured agent.pwd: {path}") from exc
    return target


@mcp.tool()
def list_attachments(task_id: int) -> list[dict[str, Any]]:
    """List files attached to a Kanboard task."""

    return _client().get_all_task_files(task_id)


@mcp.tool()
def get_attachment(file_id: int, output_path: str) -> dict[str, Any]:
    """Download a Kanboard task attachment to a local file path."""

    content = _client().download_task_file(file_id)
    path = _path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return {"file_id": file_id, "path": str(path), "bytes": len(content)}


@mcp.tool()
def delete_attachment(file_id: int) -> dict[str, Any]:
    """Delete a Kanboard task attachment."""

    _client().remove_task_file(file_id)
    return {"file_id": file_id, "deleted": True}


@mcp.tool()
def upload_attachment(project_id: int, task_id: int, path: str, filename: str | None = None) -> dict[str, Any]:
    """Upload a local file as a Kanboard task attachment."""

    source = _path(path)
    file_id = _client().create_task_file(project_id, task_id, filename or source.name, source.read_bytes())
    return {"file_id": file_id, "task_id": task_id, "filename": filename or source.name}


@mcp.tool()
def add_subtask(task_id: int, title: str, assignee: str | None = None) -> dict[str, Any]:
    """Add a subtask to a Kanboard task and optionally assign it to a user."""

    client = _client()
    user_id = 0
    if assignee:
        user_id = int(client.get_user_by_name(assignee)["id"])
    subtask_id = client.create_subtask(task_id, title, user_id=user_id)
    return {"subtask_id": subtask_id, "task_id": task_id, "title": title, "assignee": assignee}


@mcp.tool()
def move_column(project_id: int, task_id: int, column: str, swimlane_id: int = 0) -> dict[str, Any]:
    """Move a task to one of the configured board columns: todo, working, blocked, or done."""

    board = _board(project_id)
    if column not in {"todo", "working", "blocked", "done"}:
        raise KanboardError("column must be one of: todo, working, blocked, done")

    client = _client()
    columns = client.get_columns(project_id)
    by_title = {item["title"]: item for item in columns}
    target_title = board[column]
    target = by_title[target_title]
    task = client.get_task(task_id)
    target_swimlane_id = task.get("swimlane_id", swimlane_id)
    client.move_task_to_column(project_id, task_id, target["id"], swimlane_id=target_swimlane_id)
    return {"task_id": task_id, "column": column, "column_title": target_title, "column_id": target["id"]}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
