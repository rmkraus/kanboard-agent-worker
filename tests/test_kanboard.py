from __future__ import annotations

from kanboard_agent_worker.kanboard import KanboardClient, column_lookup, normalize_endpoint


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


def test_task_metadata_methods_call_expected_rpc() -> None:
    calls = []

    class FakeClient(KanboardClient):
        def __init__(self) -> None:
            pass

        def call(self, method, params=None):
            calls.append((method, params))
            if method == "getTaskMetadataByName":
                return "thread-123"
            return True

    client = FakeClient()

    assert client.get_task_metadata_by_name("12", "kanboard_agent.codex-node1.thread_id") == "thread-123"
    client.save_task_metadata("12", {"kanboard_agent.codex-node1.thread_id": "thread-123"})

    assert calls == [
        ("getTaskMetadataByName", [12, "kanboard_agent.codex-node1.thread_id"]),
        ("saveTaskMetadata", [12, {"kanboard_agent.codex-node1.thread_id": "thread-123"}]),
    ]
