from __future__ import annotations

from pathlib import Path

from kanboard_agent_worker.config import load_config


def test_load_config_with_env_overrides(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yml"
    path.write_text(
        """
server:
  user: ignored
  token: ignored
  url: http://ignored
worker:
  max_concurrency: 1
  poll_interval: 10
agent:
  name: codex
  command: "python -m example"
boards:
  - id: 1
    todo: Intake
    working: In Process
    blocked: Escalate
    done: Complete
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("KANBOARD_USER", "codex-node1")
    monkeypatch.setenv("KANBOARD_TOKEN", "secret")
    monkeypatch.setenv("KANBOARD_URL", "http://localhost:8080")
    monkeypatch.setenv("WORKER_MAX_CONCURRENCY", "3")

    config = load_config(path)

    assert config.server.user == "codex-node1"
    assert config.server.token == "secret"
    assert config.server.url == "http://localhost:8080"
    assert config.worker.max_concurrency == 3
    assert config.agent.command == ("python", "-m", "example")
    assert config.boards[0].working == "In Process"
