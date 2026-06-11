from __future__ import annotations

import pytest

from kanboard_agent_worker import kanboard_mcp
from kanboard_agent_worker.kanboard import KanboardError


def test_mcp_file_paths_are_limited_to_agent_pwd(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KANBOARD_AGENT_PWD", str(tmp_path))

    assert kanboard_mcp._path("notes.txt") == tmp_path / "notes.txt"

    with pytest.raises(KanboardError):
        kanboard_mcp._path("../outside.txt")
