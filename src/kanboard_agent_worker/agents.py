from __future__ import annotations

import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from .config import AgentConfig


OutputCallback = Callable[[str], None]


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    output: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class AgentExecutionError(RuntimeError):
    """Raised when the configured local agent cannot be started or times out."""


class SubprocessAgentRunner:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config

    def run(self, task: dict[str, Any], prompt: str, on_output: OutputCallback | None = None) -> AgentResult:
        command = [part.format(task_id=task.get("id", ""), task_title=task.get("title", "")) for part in self.config.command]
        try:
            process = subprocess.Popen(
                command,
                cwd=self.config.pwd,
                stdin=subprocess.PIPE if self.config.pass_task_on_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise AgentExecutionError(f"Failed to start agent command {command!r}: {exc}") from exc

        if self.config.pass_task_on_stdin and process.stdin is not None:
            process.stdin.write(prompt)
            process.stdin.close()

        output_lines: list[str] = []
        output_queue: queue.Queue[str | None] = queue.Queue()

        def pump_stdout() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                output_queue.put(line)
            output_queue.put(None)

        thread = threading.Thread(target=pump_stdout, daemon=True)
        thread.start()

        deadline = time.monotonic() + self.config.timeout_seconds
        stream_closed = False
        while not stream_closed:
            if time.monotonic() > deadline:
                process.kill()
                process.wait(timeout=5)
                raise AgentExecutionError(f"Agent timed out after {self.config.timeout_seconds} seconds")

            try:
                item = output_queue.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None and not thread.is_alive():
                    break
                continue

            if item is None:
                stream_closed = True
                continue

            output_lines.append(item)
            if on_output:
                on_output(item.rstrip("\n"))

        exit_code = process.wait()
        thread.join(timeout=1)
        return AgentResult(exit_code=exit_code, output="".join(output_lines))
