from __future__ import annotations

import sys
from pathlib import Path

from kanboard_agent_worker.agents import SubprocessAgentRunner
from kanboard_agent_worker.config import AgentConfig


def test_agent_runner_starts_process_in_configured_pwd(tmp_path: Path) -> None:
    workdir = tmp_path / "checkout"
    workdir.mkdir()
    runner = SubprocessAgentRunner(
        AgentConfig(
            name="python",
            command=(sys.executable, "-c", "import os; print(os.getcwd())"),
            pwd=str(workdir),
        )
    )

    result = runner.run({"id": "123", "title": "Test"}, "prompt")

    assert result.ok
    assert result.output.strip() == str(workdir)
