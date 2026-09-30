"""Skills name MCP tools as plain text; check those names exist, so a rename can't silently strand a skill."""

import re
import sys
from pathlib import Path

from anki_mcp import server

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "evals"))
import fake_obsidian  # noqa: E402  (same tool names and args as the user's real Obsidian server)

TOOLS = {"anki": {t.name for t in server.mcp._tool_manager.list_tools()},
         "obsidian": {t.name for t in fake_obsidian.mcp._tool_manager.list_tools()}}


def test_skills_only_name_tools_that_exist():
    for skill in (ROOT / ".claude" / "skills").glob("*/SKILL.md"):
        for srv, tool in re.findall(r"mcp__(\w+?)__(\w+)", skill.read_text()):
            assert tool in TOOLS[srv], f"{skill.parent.name} names mcp__{srv}__{tool}, which doesn't exist"


def test_flashcards_names_the_obsidian_write_path():
    text = (ROOT / ".claude" / "skills" / "flashcards" / "SKILL.md").read_text()
    assert "mcp__obsidian__obsidian_create_note" in text and "mcp__obsidian__obsidian_search_vault" in text
    assert "obsidian_simple_search" not in text
