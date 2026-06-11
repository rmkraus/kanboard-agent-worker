"""Kanboard polling worker and task lifecycle orchestration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from acp.schema import PromptResponse
from asyncio_pool import AioPool

from .agents import AcpSession, AcpSessionError
from .config import AppConfig, BoardConfig
from .kanboard import KanboardClient, KanboardError, column_lookup
from .task_markdown import build_agent_prompt, replace_output_section, summarize_output

LOGGER = logging.getLogger(__name__)
WORK_STARTED_COMMENT = "Started working on this task."
SUBTASK_WORK_STARTED_COMMENT = "Started working on subtask #{subtask_id}: {title}"
RECOVERY_COMMENT = "Sorry, I fell asleep on the job. I'll get back to this."
SUBTASK_STATUS_TODO = 0
SUBTASK_STATUS_IN_PROGRESS = 1
SUBTASK_STATUS_DONE = 2


@dataclass(frozen=True)
class ClaimedTask:
    """A task already moved into the working column and ready to execute."""

    board: BoardConfig
    task: dict[str, Any]
    done_column_id: int | str
    blocked_column_id: int | str
    todo_column_id: int | str | None = None
    subtask: dict[str, Any] | None = None


class Worker:
    """Poll Kanboard, run assigned tasks through an agent, and route results."""

    def __init__(self, config: AppConfig, client: KanboardClient | None = None) -> None:
        self.config = config
        self.client = client or KanboardClient(config.server.url, config.server.user, config.server.token)
        self._user_id: int | str | None = None

    @property
    def user_id(self) -> int | str:
        if self._user_id is None:
            user = self.client.get_me()
            self._user_id = user["id"]
        return self._user_id

    def check(self) -> list[str]:
        """Validate credentials and configured board columns."""

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
        for entry in self.config.roster:
            user = self.client.get_user_by_name(entry.name)
            lines.append(f"Roster {entry.name}: Kanboard user id={user.get('id')}")
        return lines

    async def run_forever(self) -> None:
        """Poll indefinitely, executing claimed work up to the concurrency limit."""

        await asyncio.to_thread(self.recover_in_process_tasks)
        async with AioPool(size=self.config.worker.max_concurrency) as pool:
            while True:
                claimed_any = False
                async for claimed in self.iter_claimed_work(pool):
                    claimed_any = True
                    await pool.spawn(self.execute_claimed(claimed), cb=self._log_execution_failure)
                if not claimed_any:
                    await asyncio.sleep(self.config.worker.poll_interval)

    async def iter_claimed_work(self, pool: AioPool) -> AsyncIterator[ClaimedTask]:
        """Yield claimed work while the pool has room and Kanboard has work."""

        while True:
            while pool.is_full:
                await asyncio.sleep(min(1, self.config.worker.poll_interval))
            claimed = await asyncio.to_thread(self.claim_next_available)
            if claimed is None:
                return
            yield claimed

    def claim_next_available(self) -> ClaimedTask | None:
        """Claim and return the next available unit of work, if one exists."""

        return self.claim_next_subtask() or self.claim_next_task()

    def claim_next_task(self) -> ClaimedTask | None:
        """Claim the next assigned top-level task from a configured todo column."""

        for board in self.config.boards:
            lookup = self._lookup_columns(board)
            board_tasks = self._tasks_by_column(board)
            todo_tasks = (
                task
                for task in self._assigned_tasks(board_tasks.get(str(lookup.todo["id"]), []))
                if not self._task_has_pending_subtasks(task)
            )
            for task in todo_tasks:
                self.client.move_task_to_column(
                    project_id=board.id,
                    task_id=task["id"],
                    column_id=lookup.working["id"],
                    swimlane_id=task.get("swimlane_id", 0),
                )
                self.client.create_comment(task["id"], self.user_id, WORK_STARTED_COMMENT)
                return ClaimedTask(
                    board=board,
                    task=self.client.get_task(task["id"]),
                    todo_column_id=lookup.todo["id"],
                    done_column_id=lookup.done["id"],
                    blocked_column_id=lookup.blocked["id"],
                )

        return None

    def claim_next_subtask(self) -> ClaimedTask | None:
        """Claim the next assigned todo subtask from any configured board column."""

        user_id = self.user_id
        for board in self.config.boards:
            lookup = self._lookup_columns(board)
            for task in self._all_board_tasks(board):
                for subtask in self._assigned_subtasks(task, status=SUBTASK_STATUS_TODO):
                    self.client.update_subtask(
                        subtask["id"],
                        task["id"],
                        title=subtask.get("title"),
                        user_id=user_id,
                        status=SUBTASK_STATUS_IN_PROGRESS,
                    )
                    self.client.start_subtask_timer(subtask["id"], user_id)
                    self.client.create_comment(
                        task["id"],
                        user_id,
                        SUBTASK_WORK_STARTED_COMMENT.format(
                            subtask_id=subtask["id"],
                            title=subtask.get("title", ""),
                        ),
                    )
                    return ClaimedTask(
                        board=board,
                        task=self.client.get_task(task["id"]),
                        subtask={**subtask, "status": SUBTASK_STATUS_IN_PROGRESS},
                        todo_column_id=lookup.todo["id"],
                        done_column_id=lookup.done["id"],
                        blocked_column_id=lookup.blocked["id"],
                    )

        return None

    def recover_in_process_tasks(self) -> int:
        """Return this worker's assigned in-process tasks to the todo columns."""

        recovered = 0
        user_id = self.user_id
        for board in self.config.boards:
            lookup = self._lookup_columns(board)
            board_tasks = self._tasks_by_column(board)
            working_tasks = self._assigned_tasks(board_tasks.get(str(lookup.working["id"]), []))
            for task in working_tasks:
                self.client.create_comment(task["id"], user_id, RECOVERY_COMMENT)
                self.client.move_task_to_column(
                    project_id=board.id,
                    task_id=task["id"],
                    column_id=lookup.todo["id"],
                    swimlane_id=task.get("swimlane_id", 0),
                )
                recovered += 1

            for task in self._all_tasks_from_columns(board_tasks):
                for subtask in self._assigned_subtasks(task, status=SUBTASK_STATUS_IN_PROGRESS):
                    if self.client.has_subtask_timer(subtask["id"], user_id):
                        self.client.stop_subtask_timer(subtask["id"], user_id)
                    self.client.update_subtask(
                        subtask["id"],
                        task["id"],
                        title=subtask.get("title"),
                        user_id=user_id,
                        status=SUBTASK_STATUS_TODO,
                    )
                    self.client.create_comment(task["id"], user_id, RECOVERY_COMMENT)
                    recovered += 1

        if recovered:
            LOGGER.info("Recovered %s in-process task(s) back to the queue", recovered)
        return recovered

    async def execute_claimed(self, claimed: ClaimedTask) -> None:
        """Run the configured agent for one claimed task and route the card."""

        task = claimed.task
        task_id = task["id"]

        try:
            comments = await asyncio.to_thread(self.client.get_all_comments, task_id)
            metadata = await asyncio.to_thread(self.client.get_task_metadata, task_id)
            thread_id = await asyncio.to_thread(self._agent_thread_id, claimed, metadata)
            prompt = build_agent_prompt(
                task,
                comments=comments,
                metadata=metadata,
                subtask=claimed.subtask,
                roster=self.config.roster,
                worker_username=self.config.server.user,
                system_prompt=self.config.agent.system_prompt,
            )
            async with await self._acp_session() as session:
                response = await session.run_turn(thread_id, prompt)
                await asyncio.to_thread(self._save_agent_thread_id, claimed, session.session_id)
                card_text = summarize_output(session.agent_text())

            await asyncio.to_thread(self.client.create_comment, task_id, self.user_id, card_text)

            if claimed.subtask:
                await asyncio.to_thread(self._route_subtask_result, claimed, response)
                return

            updated = replace_output_section(str(task.get("description") or ""), card_text)
            await asyncio.to_thread(self.client.update_task_description, task_id, updated)

            if response.stop_reason != "end_turn":
                await asyncio.to_thread(self._block_task, claimed, f"Agent stopped with reason {response.stop_reason}.")
            elif await asyncio.to_thread(self._agent_moved_task, claimed):
                return
            elif await asyncio.to_thread(self._task_has_pending_subtasks, task):
                return
            else:
                await asyncio.to_thread(self._move_task_to_column, claimed, claimed.done_column_id)
        except (AcpSessionError, KanboardError, Exception) as exc:
            if claimed.subtask:
                await asyncio.to_thread(self._block_subtask, claimed, f"Worker error: {exc}")
            else:
                await asyncio.to_thread(self._block_task, claimed, f"Worker error: {exc}")
            raise

    def _block_task(self, claimed: ClaimedTask, message: str) -> None:
        task = claimed.task
        task_id = task["id"]
        LOGGER.error("%s", message)
        self.client.create_comment(task_id, self.user_id, message)
        self._move_task_to_column(claimed, claimed.blocked_column_id)

    def _route_subtask_result(self, claimed: ClaimedTask, response: PromptResponse) -> None:
        """Stop subtask timing and update subtask state after an ACP turn."""

        if not claimed.subtask:
            return

        if self.client.has_subtask_timer(claimed.subtask["id"], self.user_id):
            self.client.stop_subtask_timer(claimed.subtask["id"], self.user_id)

        if response.stop_reason != "end_turn":
            self._block_subtask(claimed, f"Agent stopped with reason {response.stop_reason}.")
        else:
            self.client.update_subtask(
                claimed.subtask["id"],
                claimed.task["id"],
                title=claimed.subtask.get("title"),
                user_id=self.user_id,
                status=SUBTASK_STATUS_DONE,
            )

    def _block_subtask(self, claimed: ClaimedTask, message: str) -> None:
        if not claimed.subtask:
            return

        LOGGER.error("%s", message)
        if self.client.has_subtask_timer(claimed.subtask["id"], self.user_id):
            self.client.stop_subtask_timer(claimed.subtask["id"], self.user_id)
        self.client.create_comment(claimed.task["id"], self.user_id, message)
        self.client.update_subtask(
            claimed.subtask["id"],
            claimed.task["id"],
            title=claimed.subtask.get("title"),
            user_id=0,
            status=SUBTASK_STATUS_TODO,
        )

    def _move_task_to_column(self, claimed: ClaimedTask, column_id: int | str) -> None:
        task = claimed.task
        self.client.move_task_to_column(
            project_id=claimed.board.id,
            task_id=task["id"],
            column_id=column_id,
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

    def _all_board_tasks(self, board: BoardConfig) -> list[dict[str, Any]]:
        return self._all_tasks_from_columns(self._tasks_by_column(board))

    def _all_tasks_from_columns(self, tasks_by_column: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for column_tasks in tasks_by_column.values():
            tasks.extend(column_tasks)
        return tasks

    def _assigned_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [task for task in tasks if task.get("assignee_username") == self.config.server.user]

    def _assigned_subtasks(self, task: dict[str, Any], status: int | None = None) -> list[dict[str, Any]]:
        subtasks = []
        for subtask in self.client.get_all_subtasks(task["id"]):
            if _coerce_int(subtask.get("user_id")) != _coerce_int(self.user_id):
                continue
            if status is not None and _coerce_int(subtask.get("status")) != status:
                continue
            subtasks.append(subtask)
        return subtasks

    def _task_has_pending_subtasks(self, task: dict[str, Any]) -> bool:
        return any(_coerce_int(subtask.get("status")) != SUBTASK_STATUS_DONE for subtask in self.client.get_all_subtasks(task["id"]))

    async def _acp_session(self) -> AcpSession:
        """Create a connected ACP session for the configured worker agent."""

        return await AcpSession.create(self.config.agent, self.config)

    def _agent_thread_id(self, claimed: ClaimedTask, metadata: dict[str, str]) -> str:
        task = claimed.task
        key = thread_metadata_key(self.config.server.user, claimed.subtask.get("id") if claimed.subtask else None)
        thread_id = metadata.get(key)
        if not thread_id:
            thread_id = self.client.get_task_metadata_by_name(task["id"], key)
        if thread_id:
            metadata[key] = thread_id
            return thread_id
        return ""

    def _save_agent_thread_id(self, claimed: ClaimedTask, thread_id: str | None) -> None:
        if not thread_id:
            return
        key = thread_metadata_key(self.config.server.user, claimed.subtask.get("id") if claimed.subtask else None)
        existing = self.client.get_task_metadata_by_name(claimed.task["id"], key)
        if existing == thread_id:
            return
        self.client.save_task_metadata(claimed.task["id"], {key: thread_id})

    def _agent_moved_task(self, claimed: ClaimedTask) -> bool:
        if claimed.task.get("column_id") is None:
            return False
        current = self.client.get_task(claimed.task["id"])
        return str(current.get("column_id")) != str(claimed.task.get("column_id"))

    async def _log_execution_failure(self, result: Any, error: tuple[BaseException, str] | None, context: Any) -> None:
        """Log exceptions raised by a pooled task execution."""

        if error:
            exc, traceback_text = error
            LOGGER.error("Background task execution failed: %s\n%s", exc, traceback_text)


def thread_metadata_key(server_user: str, subtask_id: int | str | None = None) -> str:
    """Return the Kanboard task metadata key for this worker's agent thread."""

    if subtask_id is not None:
        return f"kanboard_worker.{server_user}.subtask.{subtask_id}.thread_id"
    return f"kanboard_worker.{server_user}.thread_id"


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
