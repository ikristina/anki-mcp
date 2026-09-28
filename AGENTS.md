# AGENTS.md

Instructions for coding agents working on this repo. (Claude Code reads this via `CLAUDE.md`.)

## What this is

An MCP server (Python, stdio) that exposes a local Anki collection to agents through the AnkiConnect add-on
(`http://127.0.0.1:8765`). Plus Claude Code skills in `.claude/skills/` that use it.

## Commands

```bash
uv sync                               # install deps
uv run pytest -q                      # unit tests with a fake AnkiConnect; no Anki or network needed (CI runs these)
uv run python scripts/smoke.py        # end-to-end check over stdio; exits non-zero on failure. Needs Anki running
uv run anki-mcp                       # run the server (stdio); normally launched by the agent host, not by hand
```

Run `pytest` after every change to `src/`, and the smoke test too when Anki is available. New tools need unit tests;
extend `tests/conftest.py`'s `FakeAnki` when the server starts using a new AnkiConnect action or search term (it raises on unknown ones).

## Layout

- `src/anki_mcp/server.py`: the MCP tools (`list_decks`, `describe_deck`, `search_notes`, `get_notes`, `get_weak_cards`, `add_notes`, `add_audio`, `get_deck_profile`, `set_deck_profile`)
- `src/anki_mcp/client.py`: AnkiConnect HTTP client and `AnkiError`
- `src/anki_mcp/profiles.py`: deck profiles, a JSON file at `~/.config/anki-mcp/profiles.json` (`ANKI_MCP_PROFILES` overrides it; tests point it at a tmp dir)
- `examples/profiles.json`: the maintainer's real profiles, as an example
- `src/anki_mcp/tts.py`: free TTS engines selected by voice string: `es-MX` (Google/gTTS), `espeak:la`, `macos:Alice`
- `tests/`: unit tests; `conftest.py` has `FakeAnki`, an in-memory AnkiConnect
- `scripts/smoke.py`: stdio client that asserts on real tool results against the real collection
- `.claude/skills/`: `flashcards` (routes technical cards to Obsidian/Yanki) and `language-cards` (vocab + audio)
- `LEARNINGS.md`: design notes and gotchas. **Append to it** whenever you learn something non-obvious.
- `ROADMAP.md`: planned headless / AnkiWeb-sync work

## Rules

- **MCP Python SDK is 2.x**: `from mcp.server.mcpserver import MCPServer`, not v1's `FastMCP`. Check the installed
  package source before copying examples from the web.
- Errors meant for the model must raise `AnkiError` (a `ToolError` subclass). Any other exception reaches the
  model only as a generic "Error executing tool", and the message is lost.
- Error messages say what went wrong **and** what to do next (e.g. list similar deck names).
- Keep results compact: paginate, strip HTML, truncate previews. The real collection has ~14k notes.
- Never print to stdout in the server (stdout carries the protocol). Log to stderr.
- **Never write to the user's real collection in tests.** Use `dry_run=True`. No delete tools without explicit request.
- Decks with `source="yanki"` are generated from the user's Obsidian vault. `add_notes` must keep refusing them.
- TTS wrappers must fail loudly on unsupported voices. Google's `la` and unknown macOS voices silently produce
  wrong audio, so they are rejected explicitly.
- Deck-specific conventions (deck names, voices, field rules) belong in deck profiles, not in skills or server code.
- Tool descriptions and `Field` descriptions are the model's only documentation. Update them with any behavior change.
