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
from .kanboard import KanboardClient, KanboardError, column_lookup, get_me_sync
from .task_markdown import build_agent_prompt

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
    todo_column_id: int | str
    done_column_id: int | str
    blocked_column_id: int | str
    subtask: dict[str, Any] | None = None


class Worker:
    """Poll Kanboard, run assigned tasks through an agent, and route results."""

    def __init__(
        self, config: AppConfig, client: KanboardClient, user_id: int | str
    ) -> None:
        """Create a worker with an explicit Kanboard client and numeric user id."""

        self.config = config
        self.client = client
        self._user_id = user_id

    @classmethod
    def from_config(cls, config: AppConfig) -> "Worker":
        """Build a production worker from config, resolving its Kanboard user id once."""

        user = get_me_sync(config.server.url, config.server.user, config.server.token)
        client = KanboardClient(
            config.server.url, config.server.user, config.server.token
        )
        return cls(config=config, client=client, user_id=user["id"])

    @property
    def user_id(self) -> int | str:
        return self._user_id

    async def close(self) -> None:
        """Close the Kanboard client if it owns async resources."""

        close = getattr(self.client, "close", None)
        if close is not None:
            await close()

    async def check(self) -> list[str]:
        """Validate credentials and configured board columns."""

        user = await self.client.get_me()
        lines = [f"Authenticated as {user.get('username')} (id={user.get('id')})"]
        for board in self.config.boards:
            columns = await self.client.get_columns(board.id)
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
            user = await self.client.get_user_by_name(entry.name)
            lines.append(f"Roster {entry.name}: Kanboard user id={user.get('id')}")
        return lines

    async def run_forever(self) -> None:
        """Poll indefinitely, executing claimed work up to the concurrency limit."""

        try:
            await self.recover_in_process_tasks()
            async with AioPool(size=self.config.worker.max_concurrency) as pool:
                while True:
                    claimed_any = False
                    async for claimed in self.iter_claimed_work(pool):
                        claimed_any = True
                        await pool.spawn(
                            self.execute_claimed(claimed),
                            cb=self._log_execution_failure,
                        )
                    if not claimed_any:
                        await asyncio.sleep(self.config.worker.poll_interval)
        finally:
            await self.close()

    async def iter_claimed_work(self, pool: AioPool) -> AsyncIterator[ClaimedTask]:
        """Yield claimed work while the pool has room and Kanboard has work."""

        while True:
            while pool.is_full:
                await asyncio.sleep(min(1, self.config.worker.poll_interval))
            claimed = await self.claim_next_available()
            if claimed is None:
                return
            yield claimed

    async def claim_next_available(self) -> ClaimedTask | None:
        """Claim and return the next available unit of work, if one exists."""

        subtask = await self.claim_next_subtask()
        if subtask is not None:
            return subtask
        return await self.claim_next_task()

    async def claim_next_task(self) -> ClaimedTask | None:
        """Claim the next assigned top-level task from a configured todo column."""

        for board in self.config.boards:
            lookup = await self._lookup_columns(board)
            board_tasks = await self._tasks_by_column(board)
            for task in self._assigned_tasks(
                board_tasks.get(str(lookup.todo["id"]), [])
            ):
                if await self._task_has_pending_subtasks(task):
                    continue
                await self.client.move_task_to_column(
                    project_id=board.id,
                    task_id=task["id"],
                    column_id=lookup.working["id"],
                    swimlane_id=task.get("swimlane_id", 0),
                )
                await self.client.create_comment(
                    task["id"], self.user_id, WORK_STARTED_COMMENT
                )
                return ClaimedTask(
                    board=board,
                    task=await self.client.get_task(task["id"]),
                    todo_column_id=lookup.todo["id"],
                    done_column_id=lookup.done["id"],
                    blocked_column_id=lookup.blocked["id"],
                )

        return None

    async def claim_next_subtask(self) -> ClaimedTask | None:
        """Claim the next assigned todo subtask from any configured board column."""

        user_id = self.user_id
        for board in self.config.boards:
            lookup = await self._lookup_columns(board)
            for task in await self._all_board_tasks(board):
                for subtask in await self._assigned_subtasks(
                    task, status=SUBTASK_STATUS_TODO
                ):
                    await self.client.update_subtask(
                        subtask["id"],
                        task["id"],
                        title=subtask.get("title"),
                        user_id=user_id,
                        status=SUBTASK_STATUS_IN_PROGRESS,
                    )
                    await self.client.start_subtask_timer(subtask["id"], user_id)
                    await self.client.create_comment(
                        task["id"],
                        user_id,
                        SUBTASK_WORK_STARTED_COMMENT.format(
                            subtask_id=subtask["id"],
                            title=subtask.get("title", ""),
                        ),
                    )
                    return ClaimedTask(
                        board=board,
                        task=await self.client.get_task(task["id"]),
                        subtask={**subtask, "status": SUBTASK_STATUS_IN_PROGRESS},
                        todo_column_id=lookup.todo["id"],
                        done_column_id=lookup.done["id"],
                        blocked_column_id=lookup.blocked["id"],
                    )

        return None

    async def recover_in_process_tasks(self) -> int:
        """Return this worker's assigned in-process tasks to the todo columns."""

        recovered = 0
        user_id = self.user_id
        for board in self.config.boards:
            lookup = await self._lookup_columns(board)
            board_tasks = await self._tasks_by_column(board)
            working_tasks = self._assigned_tasks(
                board_tasks.get(str(lookup.working["id"]), [])
            )
            for task in working_tasks:
                await self.client.create_comment(task["id"], user_id, RECOVERY_COMMENT)
                await self.client.move_task_to_column(
                    project_id=board.id,
                    task_id=task["id"],
                    column_id=lookup.todo["id"],
                    swimlane_id=task.get("swimlane_id", 0),
                )
                recovered += 1

            for task in self._all_tasks_from_columns(board_tasks):
                for subtask in await self._assigned_subtasks(
                    task, status=SUBTASK_STATUS_IN_PROGRESS
                ):
                    if await self.client.has_subtask_timer(subtask["id"], user_id):
                        await self.client.stop_subtask_timer(subtask["id"], user_id)
                    await self.client.update_subtask(
                        subtask["id"],
                        task["id"],
                        title=subtask.get("title"),
                        user_id=user_id,
                        status=SUBTASK_STATUS_TODO,
                    )
                    await self.client.create_comment(
                        task["id"], user_id, RECOVERY_COMMENT
                    )
                    recovered += 1

        if recovered:
            LOGGER.info("Recovered %s in-process task(s) back to the queue", recovered)
        return recovered

    async def execute_claimed(self, claimed: ClaimedTask) -> None:
        """Run the configured agent for one claimed task and route the card."""

        task = claimed.task
        task_id = task["id"]

        try:
            # create prompt for agent
            comments = await self.client.get_all_comments(task_id)
            metadata = await self.client.get_task_metadata(task_id)
            session_id = self._agent_session_id(claimed, metadata)
            prompt = build_agent_prompt(
                task,
                comments=comments,
                metadata=metadata,
                subtask=claimed.subtask,
                roster=self.config.roster,
                worker_username=self.config.server.user,
                system_prompt=self.config.agent.system_prompt,
            )

            # execute the agent in existing or new session
            async with await self._acp_session(session_id) as session:
                response = await session.run_turn(prompt)
                await self._save_agent_session_id(claimed, metadata, session.session_id)
                card_text = session.agent_text().strip()[:6000]

            # add comment to the task chat log
            await self.client.create_comment(task_id, self.user_id, card_text)

            # if this was a subtask, close it
            if claimed.subtask:
                await self._route_subtask_result(claimed, response)
                return

            # route tasks to next step
            if response.stop_reason != "end_turn":
                # there was an error processing the task, move to blocked
                await self._block_task(
                    claimed, f"Agent stopped with reason {response.stop_reason}."
                )
            elif await self._agent_moved_task(claimed):
                # agent has already moved its own card, leave it alone
                return
            elif await self._task_has_pending_subtasks(task):
                await self._move_task_to_column(claimed, claimed.todo_column_id)
                return
            else:
                await self._move_task_to_column(claimed, claimed.done_column_id)
        except (AcpSessionError, KanboardError, Exception) as exc:
            if claimed.subtask:
                await self._block_subtask(claimed, f"Worker error: {exc}")
            else:
                await self._block_task(claimed, f"Worker error: {exc}")
            raise

    async def _block_task(self, claimed: ClaimedTask, message: str) -> None:
        task = claimed.task
        task_id = task["id"]
        LOGGER.error("%s", message)
        await self.client.create_comment(task_id, self.user_id, message)
        await self._move_task_to_column(claimed, claimed.blocked_column_id)

    async def _route_subtask_result(
        self, claimed: ClaimedTask, response: PromptResponse
    ) -> None:
        """Stop subtask timing and update subtask state after an ACP turn."""

        if not claimed.subtask:
            return

        user_id = self.user_id
        if await self.client.has_subtask_timer(claimed.subtask["id"], user_id):
            await self.client.stop_subtask_timer(claimed.subtask["id"], user_id)

        if response.stop_reason != "end_turn":
            await self._block_subtask(
                claimed, f"Agent stopped with reason {response.stop_reason}."
            )
        else:
            await self.client.update_subtask(
                claimed.subtask["id"],
                claimed.task["id"],
                title=claimed.subtask.get("title"),
                user_id=user_id,
                status=SUBTASK_STATUS_DONE,
            )

    async def _block_subtask(self, claimed: ClaimedTask, message: str) -> None:
        if not claimed.subtask:
            return

        LOGGER.error("%s", message)
        user_id = self.user_id
        if await self.client.has_subtask_timer(claimed.subtask["id"], user_id):
            await self.client.stop_subtask_timer(claimed.subtask["id"], user_id)
        await self.client.create_comment(claimed.task["id"], user_id, message)
        await self.client.update_subtask(
            claimed.subtask["id"],
            claimed.task["id"],
            title=claimed.subtask.get("title"),
            user_id=0,
            status=SUBTASK_STATUS_TODO,
        )

    async def _move_task_to_column(
        self, claimed: ClaimedTask, column_id: int | str
    ) -> None:
        task = claimed.task
        await self.client.move_task_to_column(
            project_id=claimed.board.id,
            task_id=task["id"],
            column_id=column_id,
            swimlane_id=task.get("swimlane_id", 0),
        )

    async def _lookup_columns(self, board: BoardConfig):
        return column_lookup(
            await self.client.get_columns(board.id),
            {
                "todo": board.todo,
                "working": board.working,
                "blocked": board.blocked,
                "done": board.done,
            },
        )

    async def _tasks_by_column(
        self, board: BoardConfig
    ) -> dict[str, list[dict[str, Any]]]:
        tasks: dict[str, list[dict[str, Any]]] = {}
        for swimlane in await self.client.get_board(board.id):
            for column in swimlane.get("columns", []):
                column_id = str(column.get("id"))
                tasks.setdefault(column_id, []).extend(column.get("tasks", []))
        return tasks

    async def _all_board_tasks(self, board: BoardConfig) -> list[dict[str, Any]]:
        return self._all_tasks_from_columns(await self._tasks_by_column(board))

    def _all_tasks_from_columns(
        self, tasks_by_column: dict[str, list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for column_tasks in tasks_by_column.values():
            tasks.extend(column_tasks)
        return tasks

    def _assigned_tasks(self, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            task
            for task in tasks
            if task.get("assignee_username") == self.config.server.user
        ]

    async def _assigned_subtasks(
        self, task: dict[str, Any], status: int | None = None
    ) -> list[dict[str, Any]]:
        subtasks = []
        for subtask in await self.client.get_all_subtasks(task["id"]):
            if _coerce_int(subtask.get("user_id")) != _coerce_int(self.user_id):
                continue
            if status is not None and _coerce_int(subtask.get("status")) != status:
                continue
            subtasks.append(subtask)
        return subtasks

    async def _task_has_pending_subtasks(self, task: dict[str, Any]) -> bool:
        return any(
            _coerce_int(subtask.get("status")) != SUBTASK_STATUS_DONE
            for subtask in await self.client.get_all_subtasks(task["id"])
        )

    async def _acp_session(self, session_id: str = "") -> AcpSession:
        """Create a connected ACP session for the configured worker agent."""

        return await AcpSession.create(
            self.config.agent, self.config, session_id=session_id
        )

    def _agent_session_id(self, claimed: ClaimedTask, metadata: dict[str, str]) -> str:
        """Return the stored ACP session id for the claimed work, if present."""

        key = session_metadata_key(
            self.config.server.user,
            claimed.subtask.get("id") if claimed.subtask else None,
        )
        session_id = metadata.get(key)
        return session_id or ""

    async def _save_agent_session_id(
        self,
        claimed: ClaimedTask,
        metadata: dict[str, str],
        session_id: str | None,
    ) -> None:
        """Persist the ACP session id when the turn created a new session."""

        if not session_id:
            return
        key = session_metadata_key(
            self.config.server.user,
            claimed.subtask.get("id") if claimed.subtask else None,
        )
        if metadata.get(key) == session_id:
            return
        await self.client.save_task_metadata(claimed.task["id"], {key: session_id})
        metadata[key] = session_id

    async def _agent_moved_task(self, claimed: ClaimedTask) -> bool:
        if claimed.task.get("column_id") is None:
            return False
        current = await self.client.get_task(claimed.task["id"])
        return str(current.get("column_id")) != str(claimed.task.get("column_id"))

    async def _log_execution_failure(
        self, result: Any, error: tuple[BaseException, str] | None, context: Any
    ) -> None:
        """Log exceptions raised by a pooled task execution."""

        if error:
            exc, traceback_text = error
            LOGGER.error(
                "Background task execution failed: %s\n%s", exc, traceback_text
            )


def session_metadata_key(server_user: str, subtask_id: int | str | None = None) -> str:
    """Return the Kanboard task metadata key for this worker's ACP session."""

    if subtask_id is not None:
        return f"kanboard_worker.{server_user}.subtask.{subtask_id}.session_id"
    return f"kanboard_worker.{server_user}.session_id"


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
