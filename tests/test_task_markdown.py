from __future__ import annotations

from kanboard_agent_worker.task_markdown import extract_section, replace_output_section


def test_extract_section() -> None:
    markdown = "## Spec\nDo this\n\n## Config\nagent: codex\n\n## Output\nold"

    assert extract_section(markdown, "Spec") == "Do this"
    assert extract_section(markdown, "Config") == "agent: codex"


def test_replace_output_section() -> None:
    markdown = "## Spec\nDo this\n\n## Output\nold\n"

    assert replace_output_section(markdown, "new").strip() == "## Spec\nDo this\n\n## Output\nnew"
