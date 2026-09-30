"""The eval harness without `claude`: stream-json parsing, case checks, the Obsidian stub, the FakeAnki-backed server."""

import asyncio
import importlib
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError

EVALS = Path(__file__).resolve().parent.parent / "evals"
sys.path.insert(0, str(EVALS))

import cases  # noqa: E402
import run as runner  # noqa: E402


def _stream(*calls, text="Done.", init_servers=("anki", "obsidian")):
    """Canned stream-json: an init event, then one tool_use + tool_result per (name, input, result) and a final text."""
    yield json.dumps({"type": "system", "subtype": "init", "plugins": [], "skills": ["flashcards", "language-cards"],
                      "mcp_servers": [{"name": s, "status": "connected"} for s in init_servers]})
    for i, (name, inp, result) in enumerate(calls):
        yield json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": name, "input": inp}]}})
        yield json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}",
                                                                   "content": [{"type": "text", "text": json.dumps(result)}]}]}})
    yield "not json: stray stderr line"
    yield json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})
    yield json.dumps({"type": "result", "total_cost_usd": 0.01, "num_turns": 3, "result": text})


def _note(word, gender, wt="N"):
    return {"deck": "Languages::Spanish", "note_type": "Spanish", "tags": ["Spanish::Added"],
            "fields": {"Word": word, "Meaning": "x", "WordType": wt, "Gender": gender},
            "audio": {"field": "Audio", "voice": "es-MX"}}


def _add(notes, dry):
    status = "valid" if dry else "added"
    return ("mcp__anki__add_notes", {"notes": notes, "dry_run": dry},
            {"results": [{"index": i, "status": status} for i in range(len(notes))]})


def _checks(case_id, run):
    case = next(c for c in cases.CASES if c["id"] == case_id)
    return runner.score(case, run)


def test_parse_and_check_a_good_batch():
    notes = [_note("la ventana", "f"), _note("la madrugada", "f"), _note("el mapa", "m"), _note("bailar", "", "V"),
             _note("sin embargo", "", "Expr")]
    run = runner.parse(_stream(("mcp__anki__get_deck_profile", {}, {}), _add(notes, True), _add(notes, False)))
    assert [(c.server, c.name) for c in run.calls][0] == ("anki", "get_deck_profile")
    assert run.result["num_turns"] == 3 and run.text == "Done."
    assert runner.isolation_problems(run) == []
    assert all(_checks("es-batch", run).values()), _checks("es-batch", run)


def test_checks_catch_split_adds_and_wrong_gender():
    a, b = [_note("la ventana", "f"), _note("la madrugada", "f")], [_note("la mapa", "f"), _note("bailar", "", "V"),
                                                                  _note("sin embargo", "", "Expr")]
    run = runner.parse(_stream(_add(a + b, True), _add(a, False), _add(b, False)))
    result = _checks("es-batch", run)
    assert not result["exactly 1 real add_notes"] and not result["gender codes"] and not result["articles on nouns"]
    assert result["dry run before real add"]


def test_isolation_guard_flags_extra_servers():
    run = runner.parse(_stream(init_servers=("anki", "obsidian", "anki-real")))
    assert "MCP servers" in runner.isolation_problems(run)[0]


def test_yanki_checks():
    good = runner.parse(_stream(
        ("mcp__anki__list_decks", {}, []),
        ("mcp__obsidian__obsidian_search_vault", {"vault": "personal", "query": "select"}, {"ok": True}),
        ("mcp__obsidian__obsidian_create_note", {"vault": "personal", "path": "03 resources/Anki/Go/empty-select-blocks.md",
                                                 "content": "**What does `select {}` do?**\n\n---\n\nBlocks forever.\n"}, {"ok": True}),
        text="Created it. Run Yanki: Sync in Obsidian."))
    assert all(_checks("yanki-go", good).values()), _checks("yanki-go", good)
    bad = runner.parse(_stream(
        ("mcp__obsidian__obsidian_create_note", {"vault": "personal", "path": "03 resources/Anki/Go/Empty Select.md",
                                                 "content": "---\nnoteId: 1\n---\nQ\n---\nA"}, {"ok": True})))
    result = _checks("yanki-go", bad)
    assert not result["path + filename"] and not result["front/---/back, no noteId"] and not result["searched vault first"]


@pytest.fixture
def obsidian(tmp_path, monkeypatch):
    log = tmp_path / "obsidian.jsonl"
    monkeypatch.setenv("EVAL_OBSIDIAN_LOG", str(log))
    import fake_obsidian
    return importlib.reload(fake_obsidian), log  # fresh in-memory vault per test


def test_obsidian_stub(obsidian):
    o, log = obsidian
    assert o.obsidian_list_vaults() == {"ok": True, "data": {"vaults": ["personal"], "count": 1}}
    with pytest.raises(ToolError, match="Unknown vault"):
        o.obsidian_search_vault("work", "x")
    hits = o.obsidian_search_vault("personal", "nil channel", scope="03 resources/Anki/Go")["data"]["results"]
    assert [h["path"] for h in hits] == [cases.GO_NOTE]
    assert o.obsidian_search_vault("personal", "nil channel", scope="03 resources/Anki/DDIA")["data"]["results"] == []
    for bad in ["/abs/x.md", "a\\b.md", "03 resources/../x.md", "./x.md"]:
        with pytest.raises(ToolError, match="Invalid path"):
            o.obsidian_create_note("personal", bad, "x")
    with pytest.raises(ToolError, match="already exists"):
        o.obsidian_create_note("personal", cases.GO_NOTE, "x")
    assert o.obsidian_create_note("personal", "03 resources/Anki/Go/new.md", "q\n---\na")["data"]["created"]
    with pytest.raises(ToolError, match="already exists"):
        o.obsidian_create_note("personal", "03 resources/Anki/Go/new.md", "again")
    assert len(log.read_text().splitlines()) == 11  # every call, failed ones too
    schemas = {t.name: t.parameters for t in o.mcp._tool_manager.list_tools()}
    assert all(s["additionalProperties"] is False for s in schemas.values())
    assert set(schemas["obsidian_search_vault"]["properties"]) == {"vault", "query", "mode", "scope", "limit", "case_sensitive", "cursor"}


def test_fake_server_serves_fake_collection():
    async def go():
        params = StdioServerParameters(command=sys.executable, args=[str(EVALS / "fake_server.py")])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            decks = [json.loads(c.text)["deck"] for c in (await s.call_tool("list_decks", {})).content]
            profile = json.loads((await s.call_tool("get_deck_profile", {"deck": "Languages::Latin"})).content[0].text)
            gap = await s.call_tool("search_notes", {"query": "is:new"})
            return decks, profile, gap

    decks, profile, gap = asyncio.run(go())
    assert "Languages::_Pimsleur" in decks  # only the fake has this deck
    assert profile["profile"]["audio"]["voice"] == "espeak:la"
    assert gap.is_error and "eval fake:" in gap.content[0].text
