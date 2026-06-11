from __future__ import annotations

from pathlib import Path

from kanboard_agent_worker.config import AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings
from kanboard_agent_worker.worker import Worker, default_thread_id_for_task, thread_metadata_key


def test_thread_metadata_key_uses_server_user() -> None:
    assert thread_metadata_key("codex-node1") == "kanboard_agent.codex-node1.thread_id"


def test_default_thread_id_includes_worker_project_and_task() -> None:
    assert default_thread_id_for_task("codex-node1", 3, 42) == "kanboard-codex-node1-3-42"


def test_worker_saves_changed_agent_thread_id(tmp_path: Path) -> None:
    saved = {}

    class FakeClient:
        def get_task_metadata_by_name(self, task_id, name):
            return ""

        def save_task_metadata(self, task_id, values):
            saved[task_id] = values

    worker = Worker(_config(tmp_path), client=FakeClient())

    worker._save_agent_thread_id({"id": "42"}, "thread-123")

    assert saved == {"42": {"kanboard_agent.codex-node1.thread_id": "thread-123"}}


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        server=ServerConfig(user="codex-node1", token="secret", url="http://localhost:8080"),
        worker=WorkerSettings(max_concurrency=1, poll_interval=10),
        agent=AgentConfig(name="codex", command=("codex",), pwd=str(tmp_path)),
        boards=(BoardConfig(id=1, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),),
    )
