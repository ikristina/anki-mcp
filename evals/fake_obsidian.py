"""Record-only stand-in for the user's Obsidian MCP server, for evals. Nothing touches a real vault.

Same tool names, arguments and {"ok": true, "data": ...} envelope as the real server (registered as "obsidian", vault
"personal"). Notes live in memory, seeded with one existing card for the duplicate case. Every call is appended as a
JSON line to $EVAL_OBSIDIAN_LOG when set.
"""

import json
import os
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ConfigDict, Field

VAULT = "personal"
SEED = {
    "03 resources/Anki/Go/nil-channel-send.md": "**What happens when you send on a nil channel in Go?**\n\n---\n\n"
    "It blocks forever (a receive on a nil channel blocks forever too).\n\nTags: go/concurrency/channels\n",
}
notes: dict[str, str] = dict(SEED)
mcp = MCPServer("obsidian")


def _record(tool: str, **args) -> None:
    if log := os.environ.get("EVAL_OBSIDIAN_LOG"):
        with open(log, "a") as f:
            f.write(json.dumps({"tool": tool, "args": args}) + "\n")


def _vault(vault: str) -> None:
    if vault != VAULT:
        raise ToolError(f"Unknown vault {vault!r}. Available vaults: [{VAULT!r}]. Use obsidian_list_vaults.")


def _ok(data: dict) -> dict:
    return {"ok": True, "data": data}


@mcp.tool()
def obsidian_list_vaults() -> dict:
    """List the vault ids this server can access."""
    _record("obsidian_list_vaults")
    return _ok({"vaults": [VAULT], "count": 1})


@mcp.tool()
def obsidian_search_vault(
    vault: str,
    query: Annotated[str, Field(min_length=1, max_length=1000)],
    mode: Literal["content", "filename", "both", "tag"] = "content",
    scope: str | None = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 50,
    case_sensitive: bool = False,
    cursor: str | None = None,
) -> dict:
    """Search notes in a vault by content, filename, both, or tag. scope limits the search to a vault-relative directory."""
    _record("obsidian_search_vault", vault=vault, query=query, mode=mode, scope=scope, limit=limit,
            case_sensitive=case_sensitive, cursor=cursor)
    _vault(vault)
    norm = (lambda s: s) if case_sensitive else str.lower
    q = norm(query)
    hits = []
    for path, content in notes.items():
        if scope and not path.startswith(scope.strip("/") + "/"):
            continue
        text = {"content": content, "filename": path.rsplit("/", 1)[-1], "both": path + "\n" + content,
                "tag": content}[mode]
        # ponytail: whole-query substring or all words present; the real server's ranking isn't modeled
        if q in norm(text) or all(w in norm(text) for w in q.split()):
            hits.append({"path": path, "snippet": content[:200]})
    return _ok({"results": hits[:limit], "count": len(hits[:limit]), "next_cursor": None})


@mcp.tool()
def obsidian_create_note(vault: str, path: str, content: str = "", create_parents: bool = True) -> dict:
    """Create a note at a vault-relative path (forward slashes). Never overwrites: fails if the file exists."""
    _record("obsidian_create_note", vault=vault, path=path, content=content, create_parents=create_parents)
    _vault(vault)
    parts = path.split("/")
    if path.startswith("/") or "\\" in path or ":" in parts[0] or any(p in (".", "..", "") for p in parts):
        raise ToolError(f"Invalid path {path!r}: use a vault-relative path with forward slashes, no '.' or '..' segments.")
    if path in notes:
        raise ToolError(f"A note already exists at {path!r}; it was not overwritten.")
    notes[path] = content
    return _ok({"path": path, "created": True})


# The real server's schemas reject unknown arguments; make the stub's match (published schema and validation).
for _tool in mcp._tool_manager.list_tools():
    _tool.parameters["additionalProperties"] = False
    _tool.fn_metadata.arg_model.model_config = ConfigDict(**_tool.fn_metadata.arg_model.model_config, extra="forbid")
    _tool.fn_metadata.arg_model.model_rebuild(force=True)

if __name__ == "__main__":
    mcp.run()
