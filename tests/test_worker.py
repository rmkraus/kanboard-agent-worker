from __future__ import annotations

import asyncio
from pathlib import Path

from acp.schema import PromptResponse

from kanboard_agent_worker.config import AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings
from kanboard_agent_worker.worker import (
    RECOVERY_COMMENT,
    SUBTASK_STATUS_DONE,
    SUBTASK_STATUS_IN_PROGRESS,
    SUBTASK_WORK_STARTED_COMMENT,
    WORK_STARTED_COMMENT,
    ClaimedTask,
    Worker,
    session_metadata_key,
)


def test_session_metadata_key_uses_server_user() -> None:
    assert session_metadata_key("codex-node1") == "kanboard_worker.codex-node1.session_id"


def test_worker_saves_changed_agent_session_id(tmp_path: Path) -> None:
    saved = {}

    class FakeClient:
        async def save_task_metadata(self, task_id, values):
            saved[task_id] = values

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)
    metadata = {}

    asyncio.run(
        worker._save_agent_session_id(
            ClaimedTask(
                board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
                task={"id": "42"},
                todo_column_id=1,
                done_column_id=3,
                blocked_column_id=4,
            ),
            metadata,
            "session-123",
        ),
    )

    assert saved == {"42": {"kanboard_worker.codex-node1.session_id": "session-123"}}
    assert metadata == {"kanboard_worker.codex-node1.session_id": "session-123"}


def test_worker_returns_empty_session_id_when_metadata_is_missing(tmp_path: Path) -> None:
    saved = {}

    class FakeClient:
        async def save_task_metadata(self, task_id, values):
            saved[task_id] = values

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42"},
        todo_column_id=1,
        done_column_id=3,
        blocked_column_id=4,
    )
    metadata = {}

    session_id = worker._agent_session_id(claimed, metadata)

    assert session_id == ""
    assert metadata == {}
    assert saved == {}


def test_worker_comments_when_claiming_task(tmp_path: Path) -> None:
    comments = []
    moves = []

    class FakeClient:
        async def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        async def get_board(self, project_id):
            return [
                {
                    "columns": [
                        {
                            "id": 1,
                            "tasks": [
                                {
                                    "id": "42",
                                    "assignee_username": "codex-node1",
                                    "swimlane_id": 8,
                                }
                            ],
                        },
                        {"id": 2, "tasks": []},
                    ]
                }
            ]

        async def get_all_subtasks(self, task_id):
            return []

        async def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append((project_id, task_id, column_id, swimlane_id))

        async def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        async def get_task(self, task_id):
            return {"id": task_id, "assignee_username": "codex-node1", "swimlane_id": 8}

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)

    claimed = asyncio.run(worker.claim_next_available())

    assert claimed is not None
    assert claimed.task["id"] == "42"
    assert moves == [(1, "42", 2, 8)]
    assert comments == [("42", 9, WORK_STARTED_COMMENT)]


def test_worker_recovers_assigned_working_tasks_to_queue(tmp_path: Path) -> None:
    comments = []
    moves = []

    class FakeClient:
        async def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        async def get_board(self, project_id):
            return [
                {
                    "columns": [
                        {"id": 1, "tasks": []},
                        {
                            "id": 2,
                            "tasks": [
                                {
                                    "id": "42",
                                    "assignee_username": "codex-node1",
                                    "swimlane_id": 8,
                                },
                                {
                                    "id": "43",
                                    "assignee_username": "other-worker",
                                    "swimlane_id": 8,
                                },
                            ],
                        },
                    ]
                }
            ]

        async def get_all_subtasks(self, task_id):
            return []

        async def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        async def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append((project_id, task_id, column_id, swimlane_id))

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)

    recovered = asyncio.run(worker.recover_in_process_tasks())

    assert recovered == 1
    assert comments == [("42", 9, RECOVERY_COMMENT)]
    assert moves == [(1, "42", 1, 8)]


def test_worker_claims_assigned_subtasks_before_tasks(tmp_path: Path) -> None:
    comments = []
    subtask_updates = []
    timers = []

    class FakeClient:
        async def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        async def get_board(self, project_id):
            return [
                {
                    "columns": [
                        {
                            "id": 4,
                            "tasks": [
                                {
                                    "id": "42",
                                    "title": "Parent",
                                    "assignee_username": "someone-else",
                                    "swimlane_id": 8,
                                }
                            ],
                        },
                        {
                            "id": 1,
                            "tasks": [
                                {
                                    "id": "43",
                                    "title": "Top-level",
                                    "assignee_username": "codex-node1",
                                    "swimlane_id": 8,
                                }
                            ],
                        },
                    ]
                }
            ]

        async def get_all_subtasks(self, task_id):
            if task_id == "42":
                return [{"id": "99", "task_id": "42", "title": "Subtask work", "user_id": 9, "status": 0}]
            return []

        async def update_subtask(self, subtask_id, task_id, **values):
            subtask_updates.append((subtask_id, task_id, values))

        async def start_subtask_timer(self, subtask_id, user_id):
            timers.append((subtask_id, user_id))

        async def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        async def get_task(self, task_id):
            return {"id": task_id, "title": "Parent", "assignee_username": "someone-else", "swimlane_id": 8}

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)

    claimed = asyncio.run(worker.claim_next_available())

    assert claimed is not None
    assert claimed.subtask["id"] == "99"
    assert subtask_updates == [
        (
            "99",
            "42",
            {"title": "Subtask work", "user_id": 9, "status": SUBTASK_STATUS_IN_PROGRESS},
        )
    ]
    assert timers == [("99", 9)]
    assert comments == [("42", 9, SUBTASK_WORK_STARTED_COMMENT.format(subtask_id="99", title="Subtask work"))]


def test_worker_skips_top_level_tasks_with_pending_subtasks(tmp_path: Path) -> None:
    moves = []

    class FakeClient:
        async def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        async def get_board(self, project_id):
            return [
                {
                    "columns": [
                        {
                            "id": 1,
                            "tasks": [
                                {"id": "42", "assignee_username": "codex-node1", "swimlane_id": 8},
                                {"id": "43", "assignee_username": "codex-node1", "swimlane_id": 8},
                            ],
                        },
                        {"id": 2, "tasks": []},
                    ]
                }
            ]

        async def get_all_subtasks(self, task_id):
            if task_id == "42":
                return [{"id": "99", "task_id": "42", "title": "Pending", "user_id": 0, "status": 0}]
            return []

        async def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append((project_id, task_id, column_id, swimlane_id))

        async def create_comment(self, task_id, user_id, comment):
            pass

        async def get_task(self, task_id):
            return {"id": task_id, "assignee_username": "codex-node1", "swimlane_id": 8}

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)

    claimed = asyncio.run(worker.claim_next_available())

    assert claimed is not None
    assert claimed.task["id"] == "43"
    assert moves == [(1, "43", 2, 8)]


def test_worker_iter_claimed_work_yields_until_no_work(tmp_path: Path) -> None:
    claim = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42"},
        todo_column_id=1,
        done_column_id=3,
        blocked_column_id=4,
    )
    claims = [claim]

    class FakePool:
        is_full = False

    class FakeClient:
        pass

    async def collect() -> list[ClaimedTask]:
        return [item async for item in worker.iter_claimed_work(FakePool())]

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)

    async def claim_next_available() -> ClaimedTask | None:
        return claims.pop(0) if claims else None

    worker.claim_next_available = claim_next_available

    assert asyncio.run(collect()) == [claim]


def test_worker_respects_card_move_done_by_agent_tool(tmp_path: Path) -> None:
    key = session_metadata_key("codex-node1")
    comments = []
    moves = []

    class FakeClient:
        async def get_all_comments(self, task_id):
            return []

        async def get_task_metadata(self, task_id):
            return {key: "session-123"}

        async def create_comment(self, task_id, user_id, comment):
            comments.append(comment)

        async def save_task_metadata(self, task_id, values):
            raise AssertionError("existing session id should not be saved again")

        async def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append(column_id)

        async def get_task(self, task_id):
            return {"id": task_id, "column_id": 4, "assignee_username": "codex-node1", "swimlane_id": 8}

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)
    _install_fake_acp_session(worker, text="Need a human answer before I can continue.")
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42", "column_id": 2, "description": "## Spec\nDo it"},
        todo_column_id=1,
        done_column_id=3,
        blocked_column_id=4,
    )

    asyncio.run(worker.execute_claimed(claimed))

    assert comments == ["Need a human answer before I can continue."]
    assert moves == []


def test_worker_truncates_agent_text_inline(tmp_path: Path) -> None:
    comments = []
    moves = []
    key = session_metadata_key("codex-node1")

    class FakeClient:
        async def get_all_comments(self, task_id):
            return []

        async def get_task_metadata(self, task_id):
            return {key: "session-123"}

        async def create_comment(self, task_id, user_id, comment):
            comments.append(comment)

        async def save_task_metadata(self, task_id, values):
            raise AssertionError("existing session id should not be saved again")

        async def get_all_subtasks(self, task_id):
            return []

        async def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append(column_id)

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)
    _install_fake_acp_session(worker, text="x" * 6005)
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42", "description": "## Spec\nDo it"},
        todo_column_id=1,
        done_column_id=3,
        blocked_column_id=4,
    )

    asyncio.run(worker.execute_claimed(claimed))

    assert comments == ["x" * 6000]
    assert moves == [3]


def test_worker_returns_parent_to_ready_when_agent_tool_created_pending_subtasks(tmp_path: Path) -> None:
    comments = []
    moves = []
    key = session_metadata_key("codex-node1")

    class FakeClient:
        async def get_all_comments(self, task_id):
            return []

        async def get_task_metadata(self, task_id):
            return {key: "session-123"}

        async def create_comment(self, task_id, user_id, comment):
            comments.append(comment)

        async def save_task_metadata(self, task_id, values):
            raise AssertionError("existing session id should not be saved again")

        async def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append(column_id)

        async def get_all_subtasks(self, task_id):
            return [{"id": "101", "task_id": "42", "title": "Follow-up", "user_id": 11, "status": 0}]

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)
    _install_fake_acp_session(worker, text="Split this into follow-up work and created a subtask.")
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42", "description": "## Spec\nDo it"},
        todo_column_id=1,
        done_column_id=3,
        blocked_column_id=4,
    )

    asyncio.run(worker.execute_claimed(claimed))

    assert comments == ["Split this into follow-up work and created a subtask."]
    assert moves == [1]


def test_worker_completes_subtask_and_comments_on_parent(tmp_path: Path) -> None:
    comments = []
    updates = []
    stopped_timers = []
    key = session_metadata_key("codex-node1", "99")

    class FakeClient:
        async def get_all_comments(self, task_id):
            return []

        async def get_task_metadata(self, task_id):
            return {key: "session-123"}

        async def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        async def save_task_metadata(self, task_id, values):
            raise AssertionError("existing session id should not be saved again")

        async def has_subtask_timer(self, subtask_id, user_id):
            return True

        async def stop_subtask_timer(self, subtask_id, user_id):
            stopped_timers.append((subtask_id, user_id))

        async def update_subtask(self, subtask_id, task_id, **values):
            updates.append((subtask_id, task_id, values))

    worker = Worker(_config(tmp_path), client=FakeClient(), user_id=9)
    _install_fake_acp_session(
        worker,
        text="Subtask complete.",
        prompt_fragment="Subtask #99: Subtask work",
    )
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42", "description": "## Spec\nParent"},
        subtask={"id": "99", "task_id": "42", "title": "Subtask work", "user_id": 9, "status": 1},
        todo_column_id=1,
        done_column_id=3,
        blocked_column_id=4,
    )

    asyncio.run(worker.execute_claimed(claimed))

    assert comments == [("42", 9, "Subtask complete.")]
    assert stopped_timers == [("99", 9)]
    assert updates == [
        (
            "99",
            "42",
            {"title": "Subtask work", "user_id": 9, "status": SUBTASK_STATUS_DONE},
        )
    ]


class FakeAcpSession:
    def __init__(
        self,
        *,
        text: str,
        prompt_fragment: str | None = None,
        stop_reason: str = "end_turn",
        session_id: str = "session-123",
    ) -> None:
        self.session_id = session_id
        self._text = text
        self._prompt_fragment = prompt_fragment
        self._response = PromptResponse(stop_reason=stop_reason)

    async def __aenter__(self) -> FakeAcpSession:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def run_turn(self, prompt: str) -> PromptResponse:
        if self._prompt_fragment is not None:
            assert self._prompt_fragment in prompt
        return self._response

    def agent_text(self) -> str:
        return self._text


def _install_fake_acp_session(
    worker: Worker,
    *,
    text: str,
    prompt_fragment: str | None = None,
    stop_reason: str = "end_turn",
) -> FakeAcpSession:
    session = FakeAcpSession(text=text, prompt_fragment=prompt_fragment, stop_reason=stop_reason)

    async def fake_acp_session(session_id: str = "") -> FakeAcpSession:
        assert session_id == "session-123"
        session.session_id = session_id
        return session

    worker._acp_session = fake_acp_session
    return session


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        server=ServerConfig(user="codex-node1", token="secret", url="http://localhost:8080"),
        worker=WorkerSettings(max_concurrency=1, poll_interval=10),
        agent=AgentConfig(name="codex", command=("codex",), pwd=str(tmp_path)),
        boards=(BoardConfig(id=1, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),),
    )
