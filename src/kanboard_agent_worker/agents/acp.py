"""ACP agent wrapper and minimal ACP client implementation."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import acp
from acp import PROTOCOL_VERSION, Client, RequestError, text_block
from acp.schema import (
    AgentMessageChunk,
    AgentPlanUpdate,
    AgentThoughtChunk,
    AvailableCommandsUpdate,
    ClientCapabilities,
    ConfigOptionUpdate,
    CreateTerminalResponse,
    CurrentModeUpdate,
    EnvVariable,
    FileSystemCapabilities,
    Implementation,
    KillTerminalResponse,
    McpServerStdio,
    ReadTextFileResponse,
    ReleaseTerminalResponse,
    SessionInfoUpdate,
    TerminalExitStatus,
    TerminalOutputResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    UsageUpdate,
    UserMessageChunk,
    WaitForTerminalExitResponse,
    WriteTextFileResponse,
)

from ..config import AppConfig, BoardConfig
from .base import AgentExecResult, AgentExecutionError, BaseAgentWrapper


class AcpAgentWrapper(BaseAgentWrapper):
    """Run a single agent turn through an ACP-compatible agent process."""

    def __init__(
        self,
        config,
        app_config: AppConfig,
        executable: str,
        legacy_executable: str | None = None,
    ) -> None:
        super().__init__(config)
        self.app_config = app_config
        self.executable = executable
        self.legacy_executable = legacy_executable
        self._session_id: str | None = None

    def create_thread_id(self, project_id: int | str, task_id: int | str) -> str:
        return ""

    def exec(self, thread_id: str, prompt: str) -> AgentExecResult:
        try:
            return asyncio.run(
                asyncio.wait_for(
                    self._exec_async(thread_id, prompt),
                    timeout=self.config.timeout_seconds,
                )
            )
        except TimeoutError as exc:
            raise AgentExecutionError(f"ACP agent timed out after {self.config.timeout_seconds} seconds") from exc
        except Exception as exc:
            raise AgentExecutionError(f"ACP agent execution failed: {exc}") from exc

    async def _exec_async(self, thread_id: str, prompt: str) -> AgentExecResult:
        command = self._command()
        client = KanboardAcpClient(Path(self.config.pwd))
        proc = await asyncio.create_subprocess_exec(
            command[0],
            *command[1:],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.config.pwd,
        )
        if proc.stdin is None or proc.stdout is None:
            raise AgentExecutionError("ACP agent process did not expose stdio pipes")

        conn = acp.connect_to_agent(client, proc.stdin, proc.stdout)
        stderr_task = asyncio.create_task(proc.stderr.read() if proc.stderr else _empty_bytes())

        try:
            await conn.initialize(
                protocol_version=PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(
                    fs=FileSystemCapabilities(read_text_file=True, write_text_file=True),
                    terminal=True,
                ),
                client_info=Implementation(name="kanboard-agent-worker", version="0.1.0"),
            )
            session_id = await self._session_id_for_turn(conn, thread_id)
            self._session_id = session_id
            response = await conn.prompt(
                session_id=session_id,
                prompt=[text_block(prompt)],
                message_id=str(uuid.uuid4()),
            )
            with contextlib.suppress(Exception):
                await conn.close_session(session_id)
            return AgentExecResult(
                exit_code=0 if response.stop_reason == "end_turn" else 1,
                output=client.agent_text(),
                stdout=client.agent_text(),
                stderr="",
                command=tuple(command),
                thread_id=session_id,
            )
        finally:
            if proc.returncode is None:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    proc.kill()
                    with contextlib.suppress(Exception):
                        await proc.wait()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(stderr_task, timeout=1)

    def _command(self) -> tuple[str, ...]:
        if self.config.command:
            first = Path(self.config.command[0]).name
            if first != self.legacy_executable:
                return tuple(self.config.command)
        return (self.executable,)

    async def _session_id_for_turn(self, conn, thread_id: str) -> str:
        cwd = str(Path(self.config.pwd).resolve())
        mcp_servers = [self._kanboard_mcp_server()]
        if thread_id:
            try:
                loaded = await conn.load_session(
                    cwd=cwd,
                    session_id=thread_id,
                    mcp_servers=mcp_servers,
                )
            except Exception:
                loaded = None
            if loaded is not None:
                return thread_id

        session = await conn.new_session(cwd=cwd, mcp_servers=mcp_servers)
        return session.session_id

    def _kanboard_mcp_server(self) -> McpServerStdio:
        return McpServerStdio(
            name="kanboard",
            command=sys.executable,
            args=["-m", "kanboard_agent_worker.kanboard_mcp"],
            env=[
                EnvVariable(name="KANBOARD_URL", value=self.app_config.server.url),
                EnvVariable(name="KANBOARD_USER", value=self.app_config.server.user),
                EnvVariable(name="KANBOARD_TOKEN", value=self.app_config.server.token),
                EnvVariable(name="KANBOARD_WORKER_BOARDS", value=_boards_env(self.app_config.boards)),
                EnvVariable(name="KANBOARD_AGENT_PWD", value=str(Path(self.config.pwd).resolve())),
            ],
        )


class CodexAcpAgentWrapper(AcpAgentWrapper):
    """ACP wrapper for Codex."""

    def __init__(self, config, app_config: AppConfig) -> None:
        super().__init__(config, app_config, "codex-acp", legacy_executable="codex")


class ClaudeAcpAgentWrapper(AcpAgentWrapper):
    """ACP wrapper for Claude."""

    def __init__(self, config, app_config: AppConfig) -> None:
        super().__init__(config, app_config, "claude-agent-acp", legacy_executable="claude")


class KanboardAcpClient(Client):
    """ACP client that exposes a constrained local filesystem and terminal."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.messages: list[str] = []
        self.terminals: dict[str, TerminalState] = {}

    async def session_update(
        self,
        session_id: str,
        update: UserMessageChunk
        | AgentMessageChunk
        | AgentThoughtChunk
        | ToolCallStart
        | ToolCallProgress
        | AgentPlanUpdate
        | AvailableCommandsUpdate
        | CurrentModeUpdate
        | ConfigOptionUpdate
        | SessionInfoUpdate
        | UsageUpdate,
        **kwargs: Any,
    ) -> None:
        if isinstance(update, AgentMessageChunk) and isinstance(update.content, TextContentBlock):
            self.messages.append(update.content.text)

    async def read_text_file(
        self,
        path: str,
        session_id: str,
        limit: int | None = None,
        line: int | None = None,
        **kwargs: Any,
    ) -> ReadTextFileResponse:
        target = self._path(path)
        text = target.read_text(encoding="utf-8")
        if line is not None:
            text = "\n".join(text.splitlines()[max(0, line - 1) :])
        if limit is not None:
            text = text[:limit]
        return ReadTextFileResponse(content=text)

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs: Any) -> WriteTextFileResponse:
        target = self._path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return WriteTextFileResponse()

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[EnvVariable] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> CreateTerminalResponse:
        terminal_id = str(uuid.uuid4())
        run_env = os.environ.copy()
        for item in env or []:
            run_env[item.name] = item.value
        try:
            proc = await asyncio.create_subprocess_exec(
                command,
                *(args or []),
                cwd=str(self._path(cwd or ".")),
                env=run_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise RequestError.invalid_params(f"Failed to start terminal command {command!r}: {exc}") from exc

        output_task = asyncio.create_task(proc.communicate())
        self.terminals[terminal_id] = TerminalState(
            proc=proc,
            output_task=output_task,
            output_byte_limit=output_byte_limit,
        )
        return CreateTerminalResponse(terminal_id=terminal_id)

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse:
        state = self.terminals[terminal_id]
        output = ""
        truncated = False
        if state.output_task.done():
            output, truncated = _terminal_output(state)
        return TerminalOutputResponse(
            output=output,
            truncated=truncated,
            exit_status=_terminal_exit_status(state.proc.returncode),
        )

    async def wait_for_terminal_exit(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> WaitForTerminalExitResponse:
        state = self.terminals[terminal_id]
        await state.output_task
        if state.proc.returncode is not None and state.proc.returncode < 0:
            return WaitForTerminalExitResponse(signal=str(-state.proc.returncode))
        return WaitForTerminalExitResponse(exit_code=max(0, state.proc.returncode or 0))

    async def release_terminal(
        self,
        session_id: str,
        terminal_id: str,
        **kwargs: Any,
    ) -> ReleaseTerminalResponse | None:
        state = self.terminals.pop(terminal_id, None)
        if state and state.proc.returncode is None:
            state.proc.kill()
            with contextlib.suppress(Exception):
                await state.output_task
        return ReleaseTerminalResponse()

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> KillTerminalResponse | None:
        state = self.terminals.pop(terminal_id, None)
        if state and state.proc.returncode is None:
            state.proc.kill()
            with contextlib.suppress(Exception):
                await state.output_task
        return KillTerminalResponse()

    async def request_permission(self, *args, **kwargs):
        raise RequestError.method_not_found("session/request_permission")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise RequestError.method_not_found(method)

    def agent_text(self) -> str:
        return "".join(self.messages).strip()

    def _path(self, path: str) -> Path:
        target = Path(path).expanduser()
        if not target.is_absolute():
            target = self.root / target
        target = target.resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise RequestError.invalid_params(f"Path outside configured pwd: {path}") from exc
        return target


def _boards_env(boards: tuple[BoardConfig, ...]) -> str:
    import json

    return json.dumps(
        [
            {
                "id": board.id,
                "todo": board.todo,
                "working": board.working,
                "blocked": board.blocked,
                "done": board.done,
            }
            for board in boards
        ]
    )


@dataclass
class TerminalState:
    proc: asyncio.subprocess.Process
    output_task: asyncio.Task[tuple[bytes, bytes]]
    output_byte_limit: int | None


def _terminal_output(state: TerminalState) -> tuple[str, bool]:
    stdout, stderr = state.output_task.result()
    output = (stdout or b"").decode(errors="replace")
    if stderr:
        output += "\n" + stderr.decode(errors="replace")
    if state.output_byte_limit is None or len(output.encode()) <= state.output_byte_limit:
        return output, False
    return output.encode()[: state.output_byte_limit].decode(errors="replace"), True


def _terminal_exit_status(returncode: int | None) -> TerminalExitStatus | None:
    if returncode is None:
        return None
    if returncode < 0:
        return TerminalExitStatus(signal=str(-returncode))
    return TerminalExitStatus(exit_code=returncode)


async def _empty_bytes() -> bytes:
    return b""
