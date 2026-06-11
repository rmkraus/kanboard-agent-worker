from __future__ import annotations

import concurrent.futures
import logging
import time
from dataclasses import dataclass
from typing import Any

from .agents import AgentExecutionError, SubprocessAgentRunner
from .config import AppConfig, BoardConfig
from .kanboard import KanboardClient, KanboardError, column_lookup
from .task_markdown import build_agent_prompt, replace_output_section, summarize_output

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedTask:
    board: BoardConfig
    task: dict[str, Any]
    done_column_id: int | str
    blocked_column_id: int | str


class CommentStreamer:
    def __init__(self, client: KanboardClient, task_id: int | str, user_id: int | str, flush_chars: int = 3000) -> None:
        self.client = client
        self.task_id = task_id
        self.user_id = user_id
        self.flush_chars = flush_chars
        self._buffer: list[str] = []
        self._buffer_size = 0

    def write(self, line: str) -> None:
        if not line:
            return
        self._buffer.append(line)
        self._buffer_size += len(line) + 1
        if self._buffer_size >= self.flush_chars:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        content = "```\n" + "\n".join(self._buffer).strip() + "\n```"
        self.client.create_comment(self.task_id, self.user_id, content)
        self._buffer.clear()
        self._buffer_size = 0


class Worker:
    def __init__(self, config: AppConfig, client: KanboardClient | None = None) -> None:
        self.config = config
        self.client = client or KanboardClient(config.server.url, config.server.user, config.server.token)
        self.runner = SubprocessAgentRunner(config.agent)
        self.user_id: int | str | None = None

    def check(self) -> list[str]:
        user = self.client.get_me()
        lines = [f"Authenticated as {user.get('username')} (id={user.get('id')})"]
        for board in self.config.boards:
            columns = self.client.get_columns(board.id)
            column_lookup(
                columns,
                {
                    "todo": board.todo,
                    "working": board.working,
                    "blocked": board.blocked,
                    "done": board.done,
                },
            )
            lines.append(f"Board {board.id}: found configured columns")
        return lines

    def run_forever(self) -> None:
        self._ensure_user_id()
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.worker.max_concurrency) as executor:
            futures: set[concurrent.futures.Future[None]] = set()
            while True:
                futures = self._collect_done_futures(futures)
                capacity = self._remaining_capacity()
                if capacity > 0:
                    for claimed in self.claim_available(limit=capacity):
                        futures.add(executor.submit(self.execute_claimed, claimed))

                time.sleep(self.config.worker.poll_interval)

    def run_once(self) -> int:
        self._ensure_user_id()
        claimed = self.claim_available(limit=self.config.worker.max_concurrency)
        if not claimed:
            LOGGER.info("No matching assigned tasks found")
            return 0

        exit_code = 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.config.worker.max_concurrency) as executor:
            futures = [executor.submit(self.execute_claimed, item) for item in claimed]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception:
                    LOGGER.exception("Task execution failed unexpectedly")
                    exit_code = 1
        return exit_code

    def claim_available(self, limit: int) -> list[ClaimedTask]:
        claimed: list[ClaimedTask] = []
        for board in self.config.boards:
            if len(claimed) >= limit:
                break

            lookup = self._lookup_columns(board)
            board_tasks = self._tasks_by_column(board)
            working_count = self._assigned_count(board_tasks.get(str(lookup.working["id"]), []))
            remaining_for_board = max(0, self.config.worker.max_concurrency - working_count)
            remaining = min(limit - len(claimed), remaining_for_board)
            if remaining < 1:
                continue

            todo_tasks = self._assigned_tasks(board_tasks.get(str(lookup.todo["id"]), []))
            for task in todo_tasks[:remaining]:
                self.client.move_task_to_column(
                    project_id=board.id,
                    task_id=task["id"],
                    column_id=lookup.working["id"],
                    swimlane_id=task.get("swimlane_id", 0),
                )
                self.client.create_comment(task["id"], self._ensure_user_id(), "Claimed by worker.")
                claimed.append(
                    ClaimedTask(
                        board=board,
                        task=self.client.get_task(task["id"]),
                        done_column_id=lookup.done["id"],
                        blocked_column_id=lookup.blocked["id"],
                    )
                )
                if len(claimed) >= limit:
                    break

        return claimed

    def execute_claimed(self, claimed: ClaimedTask) -> None:
        task = claimed.task
        task_id = task["id"]
        streamer = CommentStreamer(self.client, task_id, self._ensure_user_id())

        try:
            self.client.create_comment(task_id, self._ensure_user_id(), f"Starting `{self.config.agent.name}`.")
            prompt = build_agent_prompt(task)
            result = self.runner.run(task, prompt, on_output=streamer.write)
            streamer.flush()

            if result.ok:
                summary = summarize_output(result.output)
                updated = replace_output_section(str(task.get("description") or ""), summary)
                self.client.update_task_description(task_id, updated)
                self.client.create_comment(task_id, self._ensure_user_id(), "Agent completed successfully.")
                self.client.move_task_to_column(
                    project_id=claimed.board.id,
                    task_id=task_id,
                    column_id=claimed.done_column_id,
                    swimlane_id=task.get("swimlane_id", 0),
                )
            else:
                self._block_task(claimed, f"Agent exited with code {result.exit_code}.")
        except (AgentExecutionError, KanboardError, Exception) as exc:
            streamer.flush()
            self._block_task(claimed, f"Worker error: {exc}")
            raise

    def _block_task(self, claimed: ClaimedTask, message: str) -> None:
        task = claimed.task
        task_id = task["id"]
        LOGGER.error("%s", message)
        self.client.create_comment(task_id, self._ensure_user_id(), message)
        self.client.move_task_to_column(
            project_id=claimed.board.id,
            task_id=task_id,
            column_id=claimed.blocked_column_id,
            swimlane_id=task.get("swimlane_id", 0),
        )

    def _lookup_columns(self, board: BoardConfig):
        return column_lookup(
            self.client.get_columns(board.id),
            {
                "todo": board.todo,
                "working": board.working,
                "blocked": board.blocked,
                "done": board.done,
            },
        )

    def _tasks_by_column(self, board: BoardConfig) -> dict[str, list[dict[str, Any]]]:
        tasks: dict[str, list[dict[str, Any]]] = {}
        for swimlane in self.client.get_board(board.id):
            for column in swimlane.get("columns", []):
                column_id = str(column.get("id"))
                tasks.setdefault(column_id, []).extend(column.get("tasks", []))
        return tasks

    def _remaining_capacity(self) -> int:
        working = 0
        for board in self.config.boards:
            lookup = self._lookup_columns(board)
            tasks = self._tasks_by_column(board)
            working += self._assigned_count(tasks.get(str(lookup.working["id"]), []))
        return max(0, self.config.worker.max_concurrency - working)

    def _assigned_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [task for task in tasks if task.get("assignee_username") == self.config.server.user]

    def _assigned_count(self, tasks: list[dict[str, Any]]) -> int:
        return len(self._assigned_tasks(tasks))

    def _ensure_user_id(self) -> int | str:
        if self.user_id is None:
            user = self.client.get_me()
            self.user_id = user["id"]
        return self.user_id

    def _collect_done_futures(
        self, futures: set[concurrent.futures.Future[None]]
    ) -> set[concurrent.futures.Future[None]]:
        active = set()
        for future in futures:
            if not future.done():
                active.add(future)
                continue
            try:
                future.result()
            except Exception:
                LOGGER.exception("Background task execution failed")
        return active
