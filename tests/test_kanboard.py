from __future__ import annotations

import base64
import asyncio

from kanboard_agent_worker import kanboard
from kanboard_agent_worker.kanboard import KanboardClient, _is_database_locked_error, column_lookup, normalize_endpoint


def test_normalize_endpoint_appends_jsonrpc_path() -> None:
    assert normalize_endpoint("http://localhost:8080") == "http://localhost:8080/jsonrpc.php"
    assert normalize_endpoint("http://localhost:8080/jsonrpc.php") == "http://localhost:8080/jsonrpc.php"


def test_column_lookup_returns_configured_columns() -> None:
    lookup = column_lookup(
        [
            {"id": "1", "title": "Intake"},
            {"id": "2", "title": "In Process"},
            {"id": "3", "title": "Escalate"},
            {"id": "4", "title": "Complete"},
        ],
        {"todo": "Intake", "working": "In Process", "blocked": "Escalate", "done": "Complete"},
    )

    assert lookup.todo["id"] == "1"
    assert lookup.working["id"] == "2"
    assert lookup.blocked["id"] == "3"
    assert lookup.done["id"] == "4"


def test_get_me_sync_runs_short_async_client(monkeypatch) -> None:
    class FakeClient:
        def __init__(self, url, user, token, **kwargs) -> None:
            self.args = (url, user, token, kwargs)

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get_me(self):
            return {"id": 9, "username": "codex-node1"}

    monkeypatch.setattr(kanboard, "KanboardClient", FakeClient)

    assert kanboard.get_me_sync("http://localhost:8080", "codex-node1", "secret") == {
        "id": 9,
        "username": "codex-node1",
    }


def test_task_metadata_methods_call_expected_rpc() -> None:
    calls = []

    class FakeClient(KanboardClient):
        def __init__(self) -> None:
            pass

        async def call(self, method, params=None):
            calls.append((method, params))
            if method == "getAllComments":
                return [{"comment": "hello"}]
            if method == "getTaskMetadata":
                return [{"foo": "bar"}]
            if method == "getTaskMetadataByName":
                return "thread-123"
            return True

    client = FakeClient()

    async def run() -> None:
        assert await client.get_all_comments("12") == [{"comment": "hello"}]
        assert await client.get_task_metadata("12") == {"foo": "bar"}
        assert await client.get_task_metadata_by_name("12", "kanboard_worker.codex-node1.thread_id") == "thread-123"
        await client.save_task_metadata("12", {"kanboard_worker.codex-node1.thread_id": "thread-123"})

    asyncio.run(run())

    assert calls == [
        ("getAllComments", {"task_id": 12}),
        ("getTaskMetadata", [12]),
        ("getTaskMetadataByName", [12, "kanboard_worker.codex-node1.thread_id"]),
        ("saveTaskMetadata", [12, {"kanboard_worker.codex-node1.thread_id": "thread-123"}]),
    ]


def test_task_file_methods_call_expected_rpc() -> None:
    calls = []

    class FakeClient(KanboardClient):
        def __init__(self) -> None:
            pass

        async def call(self, method, params=None):
            calls.append((method, params))
            if method == "getAllTaskFiles":
                return [{"id": 5, "name": "notes.txt"}]
            if method == "downloadTaskFile":
                return base64.b64encode(b"hello").decode("ascii")
            if method == "createTaskFile":
                return 7
            return True

    client = FakeClient()

    async def run() -> None:
        assert await client.get_all_task_files("12") == [{"id": 5, "name": "notes.txt"}]
        assert await client.download_task_file("5") == b"hello"
        assert await client.create_task_file("1", "12", "notes.txt", b"hello") == 7
        await client.remove_task_file("5")

    asyncio.run(run())

    encoded = base64.b64encode(b"hello").decode("ascii")
    assert calls == [
        ("getAllTaskFiles", {"task_id": 12}),
        ("downloadTaskFile", {"file_id": 5}),
        (
            "createTaskFile",
            {"project_id": 1, "task_id": 12, "filename": "notes.txt", "blob": encoded},
        ),
        ("removeTaskFile", {"file_id": 5}),
    ]


def test_database_locked_error_detection() -> None:
    assert _is_database_locked_error({"message": "SQLSTATE[HY000]: General error: 5 database is locked"})
    assert _is_database_locked_error("SQL Error: database is locked")
    assert not _is_database_locked_error({"message": "Unauthorized"})
