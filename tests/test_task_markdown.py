from __future__ import annotations

from kanboard_agent_worker.agents import AgentRunResult
from kanboard_agent_worker.task_markdown import build_summary_prompt, extract_section, replace_output_section


def test_extract_section() -> None:
    markdown = "## Spec\nDo this\n\n## Config\nagent: codex\n\n## Output\nold"

    assert extract_section(markdown, "Spec") == "Do this"
    assert extract_section(markdown, "Config") == "agent: codex"


def test_replace_output_section() -> None:
    markdown = "## Spec\nDo this\n\n## Output\nold\n"

    assert replace_output_section(markdown, "new").strip() == "## Spec\nDo this\n\n## Output\nnew"


def test_build_summary_prompt_contains_run_bundle() -> None:
    result = AgentRunResult(
        exit_code=1,
        stdout="stdout text",
        stderr="stderr text",
        command=("codex", "exec"),
    )

    prompt = build_summary_prompt({"id": "7", "title": "Fix bug", "description": "Details"}, result)

    assert "Task id: 7" in prompt
    assert "Run status: failed" in prompt
    assert "STDOUT:\nstdout text" in prompt
    assert "STDERR:\nstderr text" in prompt
