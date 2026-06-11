from __future__ import annotations

from pathlib import Path

from kanboard_agent_worker.agents import AgentExecResult
from kanboard_agent_worker.config import AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings
from kanboard_agent_worker.worker import (
    ClaimedTask,
    RECOVERY_COMMENT,
    SUBTASK_STATUS_DONE,
    SUBTASK_STATUS_IN_PROGRESS,
    SUBTASK_STATUS_TODO,
    SUBTASK_WORK_STARTED_COMMENT,
    Worker,
    WORK_STARTED_COMMENT,
    thread_metadata_key,
)


def test_thread_metadata_key_uses_server_user() -> None:
    assert thread_metadata_key("codex-node1") == "kanboard_worker.codex-node1.thread_id"


def test_worker_saves_changed_agent_thread_id(tmp_path: Path) -> None:
    saved = {}

    class FakeClient:
        def get_task_metadata_by_name(self, task_id, name):
            return ""

        def save_task_metadata(self, task_id, values):
            saved[task_id] = values

    worker = Worker(_config(tmp_path), client=FakeClient())

    worker._save_agent_thread_id(
        ClaimedTask(
            board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
            task={"id": "42"},
            done_column_id=3,
            blocked_column_id=4,
        ),
        "thread-123",
    )

    assert saved == {"42": {"kanboard_worker.codex-node1.thread_id": "thread-123"}}


def test_worker_returns_empty_thread_id_when_metadata_is_missing(tmp_path: Path) -> None:
    saved = {}

    class FakeClient:
        def get_task_metadata_by_name(self, task_id, name):
            return ""

        def save_task_metadata(self, task_id, values):
            saved[task_id] = values

    worker = Worker(_config(tmp_path), client=FakeClient())
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42"},
        done_column_id=3,
        blocked_column_id=4,
    )
    metadata = {}

    thread_id = worker._agent_thread_id(claimed, metadata)

    assert thread_id == ""
    assert metadata == {}
    assert saved == {}


def test_worker_comments_when_claiming_task(tmp_path: Path) -> None:
    comments = []
    moves = []

    class FakeClient:
        def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        def get_board(self, project_id):
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

        def get_all_subtasks(self, task_id):
            return []

        def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append((project_id, task_id, column_id, swimlane_id))

        def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        def get_task(self, task_id):
            return {"id": task_id, "assignee_username": "codex-node1", "swimlane_id": 8}

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker._user_id = 9

    claimed = worker.claim_available(limit=1)

    assert len(claimed) == 1
    assert moves == [(1, "42", 2, 8)]
    assert comments == [("42", 9, WORK_STARTED_COMMENT)]


def test_worker_recovers_assigned_working_tasks_to_queue(tmp_path: Path) -> None:
    comments = []
    moves = []

    class FakeClient:
        def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        def get_board(self, project_id):
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

        def get_all_subtasks(self, task_id):
            return []

        def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append((project_id, task_id, column_id, swimlane_id))

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker._user_id = 9

    recovered = worker.recover_in_process_tasks()

    assert recovered == 1
    assert comments == [("42", 9, RECOVERY_COMMENT)]
    assert moves == [(1, "42", 1, 8)]


def test_worker_claims_assigned_subtasks_before_tasks(tmp_path: Path) -> None:
    comments = []
    subtask_updates = []
    timers = []

    class FakeClient:
        def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        def get_board(self, project_id):
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

        def get_all_subtasks(self, task_id):
            if task_id == "42":
                return [{"id": "99", "task_id": "42", "title": "Subtask work", "user_id": 9, "status": 0}]
            return []

        def update_subtask(self, subtask_id, task_id, **values):
            subtask_updates.append((subtask_id, task_id, values))

        def start_subtask_timer(self, subtask_id, user_id):
            timers.append((subtask_id, user_id))

        def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        def get_task(self, task_id):
            return {"id": task_id, "title": "Parent", "assignee_username": "someone-else", "swimlane_id": 8}

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker._user_id = 9

    claimed = worker.claim_available(limit=1)

    assert len(claimed) == 1
    assert claimed[0].subtask["id"] == "99"
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
        def get_columns(self, project_id):
            return [
                {"id": 1, "title": "Ready"},
                {"id": 2, "title": "In Progress"},
                {"id": 3, "title": "Done"},
                {"id": 4, "title": "Blocked"},
            ]

        def get_board(self, project_id):
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

        def get_all_subtasks(self, task_id):
            if task_id == "42":
                return [{"id": "99", "task_id": "42", "title": "Pending", "user_id": 0, "status": 0}]
            return []

        def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append((project_id, task_id, column_id, swimlane_id))

        def create_comment(self, task_id, user_id, comment):
            pass

        def get_task(self, task_id):
            return {"id": task_id, "assignee_username": "codex-node1", "swimlane_id": 8}

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker._user_id = 9

    claimed = worker.claim_available(limit=2)

    assert [item.task["id"] for item in claimed] == ["43"]
    assert moves == [(1, "43", 2, 8)]


def test_worker_respects_card_move_done_by_agent_tool(tmp_path: Path) -> None:
    key = thread_metadata_key("codex-node1")
    comments = []
    moves = []
    descriptions = []

    class FakeClient:
        def get_all_comments(self, task_id):
            return []

        def get_task_metadata(self, task_id):
            return {key: "thread-123"}

        def create_comment(self, task_id, user_id, comment):
            comments.append(comment)

        def get_task_metadata_by_name(self, task_id, name):
            return "thread-123"

        def save_task_metadata(self, task_id, values):
            raise AssertionError("existing thread id should not be saved again")

        def update_task_description(self, task_id, description):
            descriptions.append(description)

        def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append(column_id)

        def get_task(self, task_id):
            return {"id": task_id, "column_id": 4, "assignee_username": "codex-node1", "swimlane_id": 8}

    class FakeAgent:
        def exec(self, thread_id, prompt):
            assert thread_id == "thread-123"
            return AgentExecResult(
                exit_code=0,
                output="Need a human answer before I can continue.",
                stdout="",
                stderr="",
                command=("fake-agent",),
            )

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker._user_id = 9
    worker._agent = lambda: FakeAgent()
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42", "column_id": 2, "description": "## Spec\nDo it\n\n## Output\nold"},
        done_column_id=3,
        blocked_column_id=4,
    )

    worker.execute_claimed(claimed)

    assert comments == ["Need a human answer before I can continue."]
    assert descriptions[-1].strip().endswith("Need a human answer before I can continue.")
    assert moves == []


def test_worker_does_not_complete_parent_when_agent_tool_created_pending_subtasks(tmp_path: Path) -> None:
    comments = []
    descriptions = []
    moves = []
    key = thread_metadata_key("codex-node1")

    class FakeClient:
        def get_all_comments(self, task_id):
            return []

        def get_task_metadata(self, task_id):
            return {key: "thread-123"}

        def create_comment(self, task_id, user_id, comment):
            comments.append(comment)

        def get_task_metadata_by_name(self, task_id, name):
            return "thread-123"

        def save_task_metadata(self, task_id, values):
            raise AssertionError("existing thread id should not be saved again")

        def update_task_description(self, task_id, description):
            descriptions.append(description)

        def move_task_to_column(self, project_id, task_id, column_id, swimlane_id=0):
            moves.append(column_id)

        def get_all_subtasks(self, task_id):
            return [{"id": "101", "task_id": "42", "title": "Follow-up", "user_id": 11, "status": 0}]

    class FakeAgent:
        def exec(self, thread_id, prompt):
            return AgentExecResult(
                exit_code=0,
                output="Split this into follow-up work and created a subtask.",
                stdout="",
                stderr="",
                command=("fake-agent",),
            )

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker._user_id = 9
    worker._agent = lambda: FakeAgent()
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42", "description": "## Spec\nDo it\n\n## Output\nold"},
        todo_column_id=1,
        done_column_id=3,
        blocked_column_id=4,
    )

    worker.execute_claimed(claimed)

    assert comments == ["Split this into follow-up work and created a subtask."]
    assert descriptions[-1].strip().endswith("Split this into follow-up work and created a subtask.")
    assert moves == []


def test_worker_completes_subtask_and_comments_on_parent(tmp_path: Path) -> None:
    comments = []
    updates = []
    stopped_timers = []
    key = thread_metadata_key("codex-node1", "99")

    class FakeClient:
        def get_all_comments(self, task_id):
            return []

        def get_task_metadata(self, task_id):
            return {key: "thread-123"}

        def create_comment(self, task_id, user_id, comment):
            comments.append((task_id, user_id, comment))

        def get_task_metadata_by_name(self, task_id, name):
            return "thread-123"

        def save_task_metadata(self, task_id, values):
            raise AssertionError("existing thread id should not be saved again")

        def has_subtask_timer(self, subtask_id, user_id):
            return True

        def stop_subtask_timer(self, subtask_id, user_id):
            stopped_timers.append((subtask_id, user_id))

        def update_subtask(self, subtask_id, task_id, **values):
            updates.append((subtask_id, task_id, values))

    class FakeAgent:
        def exec(self, thread_id, prompt):
            assert "Subtask #99: Subtask work" in prompt
            return AgentExecResult(
                exit_code=0,
                output="Subtask complete.",
                stdout="",
                stderr="",
                command=("fake-agent",),
            )

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker._user_id = 9
    worker._agent = lambda: FakeAgent()
    claimed = ClaimedTask(
        board=BoardConfig(id=7, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),
        task={"id": "42", "description": "## Spec\nParent"},
        subtask={"id": "99", "task_id": "42", "title": "Subtask work", "user_id": 9, "status": 1},
        done_column_id=3,
        blocked_column_id=4,
    )

    worker.execute_claimed(claimed)

    assert comments == [("42", 9, "Subtask complete.")]
    assert stopped_timers == [("99", 9)]
    assert updates == [
        (
            "99",
            "42",
            {"title": "Subtask work", "user_id": 9, "status": SUBTASK_STATUS_DONE},
        )
    ]


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        server=ServerConfig(user="codex-node1", token="secret", url="http://localhost:8080"),
        worker=WorkerSettings(max_concurrency=1, poll_interval=10),
        agent=AgentConfig(name="codex", command=("codex",), pwd=str(tmp_path)),
        boards=(BoardConfig(id=1, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),),
    )
