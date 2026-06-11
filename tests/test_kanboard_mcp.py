from __future__ import annotations

import asyncio

import pytest

from kanboard_agent_worker import kanboard_mcp
from kanboard_agent_worker.kanboard import KanboardError


def test_mcp_file_paths_are_limited_to_agent_pwd(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("KANBOARD_AGENT_PWD", str(tmp_path))

    assert kanboard_mcp._path("notes.txt") == tmp_path / "notes.txt"

    with pytest.raises(KanboardError):
        kanboard_mcp._path("../outside.txt")


def test_mcp_add_comment_uses_authenticated_user(monkeypatch) -> None:
    calls = []

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get_me(self):
            return {"id": 9}

        async def create_comment(self, task_id, user_id, content):
            calls.append((task_id, user_id, content))
            return 123

    monkeypatch.setattr(kanboard_mcp, "_client", FakeClient)

    result = asyncio.run(kanboard_mcp.add_comment(42, "See https://example.test/result"))

    assert result == {"comment_id": 123, "task_id": 42}
    assert calls == [(42, 9, "See https://example.test/result")]
