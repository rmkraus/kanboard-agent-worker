from __future__ import annotations

from pathlib import Path

from kanboard_agent_worker.config import load_config


def test_load_config_with_env_overrides(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "config.yml"
    workdir = tmp_path / "workdir"
    workdir.mkdir()
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
  pwd: ./workdir
  system_prompt: Prefer small changes.
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
    monkeypatch.delenv("AGENT_COMMAND", raising=False)
    monkeypatch.delenv("AGENT_PWD", raising=False)

    config = load_config(path)

    assert config.server.user == "codex-node1"
    assert config.server.token == "secret"
    assert config.server.url == "http://localhost:8080"
    assert config.worker.max_concurrency == 3
    assert config.agent.command == ("python", "-m", "example")
    assert config.agent.pwd == str(workdir.resolve())
    assert config.agent.system_prompt == "Prefer small changes."
    assert config.boards[0].working == "In Process"


def test_agent_pwd_env_override(tmp_path: Path, monkeypatch) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    configured_pwd = config_dir / "configured"
    configured_pwd.mkdir()
    env_pwd = tmp_path / "env-pwd"
    env_pwd.mkdir()
    path = config_dir / "config.yml"
    path.write_text(
        """
server:
  user: admin
  token: admin
  url: http://localhost:8080
agent:
  name: codex
  pwd: ./configured
  command:
    - codex
boards:
  - id: 1
    todo: Intake
    working: In Process
    blocked: Escalate
    done: Complete
""",
        encoding="utf-8",
    )

    monkeypatch.setenv("AGENT_PWD", str(env_pwd))
    monkeypatch.delenv("AGENT_COMMAND", raising=False)

    config = load_config(path)

    assert config.agent.pwd == str(env_pwd.resolve())
