from __future__ import annotations

from pathlib import Path

from kanboard_agent_worker.agents import AgentRunResult
from kanboard_agent_worker.config import AgentConfig, AppConfig, BoardConfig, ServerConfig, WorkerSettings
from kanboard_agent_worker.worker import Worker, thread_metadata_key


def test_thread_metadata_key_uses_server_user() -> None:
    assert thread_metadata_key("codex-node1") == "kanboard_agent.codex-node1.thread_id"


def test_worker_creates_and_saves_missing_agent_thread_id(tmp_path: Path) -> None:
    saved = {}

    class FakeClient:
        def get_task_metadata_by_name(self, task_id, name):
            return ""

        def save_task_metadata(self, task_id, values):
            saved[task_id] = values

    class FakeRunner:
        uses_threads = True

        def create_thread(self, task):
            return AgentRunResult(
                exit_code=0,
                stdout="",
                stderr="",
                command=("codex",),
                thread_id="thread-123",
            )

    worker = Worker(_config(tmp_path), client=FakeClient())
    worker.runner = FakeRunner()

    assert worker._ensure_agent_thread_id({"id": "42"}) == "thread-123"
    assert saved == {"42": {"kanboard_agent.codex-node1.thread_id": "thread-123"}}


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        server=ServerConfig(user="codex-node1", token="secret", url="http://localhost:8080"),
        worker=WorkerSettings(max_concurrency=1, poll_interval=10),
        agent=AgentConfig(name="codex", command=("codex",), pwd=str(tmp_path)),
        boards=(BoardConfig(id=1, todo="Ready", working="In Progress", blocked="Blocked", done="Done"),),
    )
