# Learnings: building tools for agents

A running log of what matters when building MCP servers, skills and plugins. Add to it every session.
Anything under "Gotchas hit" happened in this repo.

---

## 1. Mental model

- **An MCP server is a normal program that exposes functions.** The *host* (Claude Code, Claude Desktop, Cursor…) runs the model and decides when to call your functions. Your server has no LLM, no API key, and no token cost.
- Three kinds of things a server can expose:
  - **Tools** are actions the *model* chooses to call (`search_notes`, `add_notes`). Most servers only need these.
  - **Resources** are data the *user/host* attaches as context (like `@`-mentioning a file). They are read-only.
  - **Prompts** are reusable templates the *user* picks (these show up as slash commands in Claude Code).
- **Transports:** `stdio` (host launches your process and talks over stdin/stdout, which suits local tools) vs. `streamable HTTP` (for remote or shared servers, which also need auth). Start with stdio.
- **stdout belongs to the protocol.** A `print()` in a stdio server corrupts the JSON-RPC stream. Log to stderr.

## 2. Tool design is the actual skill

Your "user" is a model that only sees the tool **name, description, and input schema**. That text is the whole interface.

- **Descriptions are prompts.** Say *when* to use the tool, not only what it does. Include examples of valid input (see the `query` field in `search_notes`, which lists real Anki search syntax).
- **Few, task-shaped tools beat many API-shaped ones.** Don't mirror AnkiConnect's ~100 actions. `get_weak_cards`combines findCards + cardsInfo + sorting because that is the question an agent actually asks.
- **Budget the context window.** The collection here has 13.7k notes. Return previews, paginate (`limit`/`offset` + `total`), strip HTML, truncate fields, and provide a separate "get full detail" tool (`get_notes`).
- **Errors must say what to do next.** Compare "deck not found" with
  `Deck 'Golang' does not exist. Similar: ['Go', 'Leetcode::golang']. Use list_decks.` With the second, the agent fixes itself.
- **Batch writes, and report per-item results.** A batch with 1 bad item should still add the other 49 and say which failed.
- **Make writes safe:** `dry_run`, tag everything the agent creates (`mcp-added`), no delete tool until you need one.
- **Constrain inputs with the schema** (`Field(ge=1, le=100)`, `min_length`). The SDK turns violations into errors the model can read, so it retries instead of flooding context.
- **Tool annotations** (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`) tell the host how risky a call is. Hosts can use them for permission prompts.
- **Server `instructions`** are a system-prompt-ish blurb sent once at connect. Use it for cross-tool workflow
 ("call list_decks first").

- **Tool names:** snake_case, verb_noun (`describe_deck`, `add_notes`), consistent across the server. The spec doesn't
  mandate a case, but hosts prefix names (`mcp__anki__add_notes`) and the model sees tools from many servers, so a name
  must be clear on its own. The Python SDK takes the name from the function name.
- **Discovery tools pay off immediately.** `describe_deck`'s first run found things I'd missed by hand: a field the user
  had just added (`Audio` on the Latin note type), a tag typo (`duoling`), and a template referencing a nonexistent field.
  A tool that reports *facts about the data* beats hardcoding assumptions in prompts.
- Fuzzy suggestions: substring matching missed `Spansh`. `difflib.get_close_matches` (stdlib) on the last deck segment fixes it.

- **A capability hidden inside another tool's option is invisible.** Audio existed only as `add_notes(audio=…)`. Asked to
  "add audio to my Latin cards", another session saw no fitting tool and offered to write a script with Google's Latin voice
  (the one we'd rejected). The fix is a dedicated `add_audio` tool whose *description* carries the lesson ("Latin: espeak:la;
  Google 'la' is not Latin"). Knowledge that lives only in LEARNINGS.md or a skill doesn't reach sessions that don't load them.
- **Dry runs must validate everything that can fail**, not just the data. Voice validation originally ran only while
  generating audio, so a dry run approved Google `la`. Now `validate_voice` runs up front.
- Skills installed globally as **symlinks** (`~/.claude/skills/x -> repo/.claude/skills/x`): available in every project,
  one source of truth, updated by `git pull`.

- **A fake of the external system makes tests possible anywhere, and finds real bugs.** `tests/conftest.py` has
  `FakeAnki`, which answers the ~15 AnkiConnect actions the server uses from an in-memory collection. It *raises* on any action or
  search term it doesn't model, so the fake can't silently drift from what the server needs. Its first run found two bugs
  in `_plain` (the HTML stripper that also feeds TTS): `&nbsp;` left a doubled space, and inline tags became spaces
  (`Marcum <b>excitamus</b>.` → `Marcum excitamus .`). The smoke test never hit these, because the real data it checked had no inline HTML.
- Keep the two test layers: **unit tests** (fast, deterministic, CI, edge cases on purpose) and the **smoke test**
  (real stdio + real collection, catches API quirks the fake doesn't know about, such as `getDeckStats` returning leaf names).

## 3. Gotchas hit

- **MCP Python SDK 2.x (2026) renamed `FastMCP` → `MCPServer`** (`from mcp.server.mcpserver import MCPServer`). Most tutorials, blog posts and LLM training data still show v1. Always check the installed version and read the package source or migration guide. Also, v2 uses snake_case client attributes (`result.is_error`, not `isError`).
- **In SDK 2.x, only `ToolError` messages reach the model.** Any other exception gets reduced to
  `Error executing tool X`, which hides the useful message. Make your domain error subclass `ToolError`.
- **The stdio client does not pass your shell environment to the server.** It forwards only a safe default set. Pass env explicitly (`StdioServerParameters(env=...)`, or `claude mcp add -e KEY=value`).
- Don't trust your first test that "passes". The offline-error test passed for the wrong reason (the env var never arrived), and that is how the gotcha above surfaced. Make each test able to fail.
- Look at the real data before designing tools. This collection has 50 note types, including `Yanki - Basic`
  (cards synced from Obsidian). A tool that assumed `Front/Back` everywhere would have broken.
- **Wrapped APIs have quirks you only find by printing results.** AnkiConnect's `getDeckStats` returns the
  *leaf* deck name (`04_Transactions`), not the full path, so subdecks collided. The fix is to key by deck id.
- **Check a heuristic against the counts before trusting it.** "Every deck with Yanki notes is Yanki-owned" looked true until counts showed `Languages::Latin` = 2 Yanki / 464 total. The rule became: `yanki` only if all own cards are Yanki, `mixed` if some are.
- Read the other system's config instead of inferring. Yanki's `data.json` lists exactly which vault folders sync.

## 3b. Server vs. skill: where each rule belongs

"Tech/interview cards go to Obsidian, everything else goes to Anki" is split in two:
- **The MCP server enforces the invariant.** `add_notes` refuses Yanki-owned decks, and its error says where to go instead. This holds no matter which agent, prompt or model calls it.
- **A skill carries the policy/workflow** (`.claude/skills/flashcards/SKILL.md`): how to route, the Yanki markdown format, and the reminder to sync. It composes two servers (`anki` + the existing `obsidian` one). There was no need to build an Obsidian writer, because a server already existed.
- Expose facts, not only rules: `list_decks` returns `source: yanki|anki|mixed`, so the agent can decide *before* it hits an error.
- **Data belongs in neither.** The first `language-cards` skill had a table of my decks, note types and voices. That made
  it useless to anyone else, and I had to edit the skill whenever the collection changed. Deck profiles
  (`get_deck_profile` / `set_deck_profile`, a JSON file) moved that data out. Now the skill holds only the workflow ("read
  the profile, follow it; no profile → describe_deck, propose one, save after the user confirms").
- Agent memory is just a tool that reads and writes a file. The design questions are: **who may write it**
  (the tool description says to save only after the user confirms), **what gets validated on write**
  (deck, note type, field names and voice are checked against Anki, so a bad profile fails at save time instead of
  weeks later), and **where it lives** (outside the collection, so saving never triggers an Anki sync). Separate
  *inferred* facts (`describe_deck`, recomputed each time) from *decided* ones (the profile). An inference can go stale;
  a decision is the user's to change.
- "Passes on my machine" isn't CI. The profile tests passed locally because `espeak-ng` is installed on my Mac, and
  failed on CI's Ubuntu runner, which doesn't have it. Unit tests shouldn't depend on system binaries, so the profile tests stub the eSpeak
  check. To reproduce CI's environment before pushing: `env PATH=/usr/bin:/bin .venv/bin/python -m pytest -q`.
- Tests must isolate any file the server writes: the `anki` fixture points `ANKI_MCP_PROFILES` at `tmp_path`. Otherwise
  a test run would overwrite the real profiles.

## 3c. Adding audio (TTS) to language cards

- **Mine the user's existing setup before picking a tool.** HyperTTS's presets (in the add-on's `meta.json`, which I read without printing API keys) showed that audio came from the *Google Translate* voice (`es-MX`, `fr`). `gTTS` calls the same free voices, so new cards sound like old ones, with no key and no cost.
- **Put the side effect inside the tool, not in a second tool.** `add_notes` takes an optional `audio` spec. The server synthesizes the MP3 and passes it to AnkiConnect's `addNotes` `audio` option, which stores the media file and inserts `[sound:…]`. An agent can't forget the audio step or insert a wrong filename.
- **Failure policy is a design decision.** If TTS fails, the note is *not* added (errors say so). The alternative, adding it silently without audio, would break the "every language card has audio" invariant (3,408/3,408 today).
- **"Supported" in a list isn't the same as "works". Listen to or look at the output.** gTTS lists `la` (Latin), and it produced a valid MP3, so all automated checks passed. It sounds like English text read aloud. Only a human listening caught it. For generated media, a person has to check a sample before you make it the default.
- **Same problem, second tool:** `say -v Nobody` silently uses the default voice instead of failing. Wrappers should turn silent fallbacks into explicit errors (check the voice list first), or the agent will "succeed" with wrong audio.
- Engine choice lives in the voice string (`es-MX`, `espeak:la`, `macos:Alice`). One parameter the agent already understands beats a new `engine` field plus rules about which combinations are valid.
- Latin voices checked: Google `la` (fake), Microsoft/Bing (none of 322 voices), Meta MMS-TTS `lat` (Italian-style
  pronunciation, not what the user is learning). eSpeak NG `la` is robotic but pronounces Latin by Latin rules, so it's the pick.
- **A smoke test that only prints is not a test.** The first version printed output that *looked* fine, but it showed only
  the first item of each list, and its "valid card" case had silently turned into an error case after the Yanki guard.
  Now every check asserts an expected result and the script exits non-zero on failure.
- CLI tools have format quirks: `espeak-ng` streams WAV with placeholder sizes that `afconvert` rejects, so the fix is to rewrite the header with Python's `wave` module.
- `open_world_hint=True` on `add_notes` now, because it calls an external service.
- **Skills can be commands.** `.claude/skills/language-cards/SKILL.md` triggers automatically, or runs as `/language-cards la cumbre, el atardecer`. `$ARGUMENTS` in the skill body receives the text after the command.
- Hardcoding the user's conventions (deck, note type, field names, voice) in the *skill* is fine. That's personal policy. The *server* stays generic: it validates field names against Anki rather than knowing about Spanish.
- **Cloze decks need `cloze_only`.** The stock `Cloze` note type has no audio field and the word lives mid-sentence, with IPA
  typed inside the deletion (`{{c1::virile /ˈvɪr.əl/}}`, `{{c1::extraneous (/…/)}}`). `add_audio(cloze_only=True)` speaks only
  the deletion text with IPA and `::hints` dropped, into `Back Extra` (no schema change). Notes without a real cloze are
  counted as `skipped_no_cloze`; in practice those were typos (`{{c1:word}}`, one colon) that Anki doesn't hide either.
- **An MCP server keeps running old code until reconnected** (`/mcp` → reconnect). A new tool parameter is silently ignored
  by the old process, so the first dry run after a change must show the new behavior before writing anything.

## 4. Workflow that works

1. Poke the underlying API by hand first (a 5-line Python call to AnkiConnect) and look at real data shapes and sizes.
2. Write the smallest server (3 to 5 tools), then a **stdio smoke test** (`scripts/smoke.py`) that talks to it exactly like a host does.
3. Test error paths on purpose: bad args, missing resources, dependency down.
4. Register it in Claude Code and *use it for real*. Watch where the model misuses tools. Fix **descriptions first**, code second.
5. Turn repeated misuses into eval cases (see §6).

## 5. Claude Code specifics

- Register: `claude mcp add anki -- uv --directory /abs/path/to/project run anki-mcp`
  - Scopes: `--scope local` (default, just you, this project), `project` (writes `.mcp.json` to share via git), `user` (all projects).
  - Env vars: `-e ANKI_CONNECT_URL=...`
- **Cost control per skill:** frontmatter `model: haiku` + `effort: low` switches models while the skill runs (for the rest of
  that turn; the session model comes back on your next prompt). `allowed-tools` pre-approves the MCP tools so there are no
  permission prompts. The MCP server itself never costs tokens. Only the model driving it does.
  Rule of thumb: mechanical work (vocab cards) → Haiku; work that needs judgment or fact-checking (technical cards) → a stronger model.
- `CONNECTION_CLOSED` means the server process died at startup. Debug with `claude mcp get <name>` and run the exact
  command yourself. Here, `--directory` pointed at `src/anki-mcp` instead of the project root (where `pyproject.toml` lives).
  Use absolute paths, including for `uv` itself (`/Users/.../.local/bin/uv`), because GUI apps may not inherit your shell's PATH.
- Check it with `claude mcp list`, or `/mcp` inside a session (shows status and tools).
- Tools appear to the model as `mcp__<server>__<tool>`, e.g. `mcp__anki__add_notes`.
- The **MCP Inspector** (`npx @modelcontextprotocol/inspector uv run anki-mcp`) is a GUI for calling tools by hand.
- The extension ladder, from least to most setup:
  - **CLAUDE.md** holds always-on project instructions.
  - **Skill** (`SKILL.md`) is loaded on demand when the task matches its description. Use it for workflows like "turn this solution into good flashcards".
  - **Slash command** is a prompt the user triggers explicitly.
  - **Hook** is a shell command the harness runs on events (after a tool call, on stop…). It is deterministic, with no model involved.
  - **MCP server** gives new capabilities that need code or external systems.
  - **Plugin** is a bundle of the above that people install in one step.
  - Rule of thumb: if it is only *knowledge or process*, make a skill. If it needs *access to something*, make an MCP server.

## 6. Evals (next step, and the most valuable skill to show)

- Headless runs: `claude -p "prompt" --output-format stream-json --verbose` shows every tool call. These count against your plan's usage, not API billing.
- Eval case = prompt + expected tool(s) + checks on arguments/result. Example: "add a card about Go channels to my Go deck"
  should call `add_notes` with deck=`Go`, and should not call `add_notes` 3 times with 1 note each.
- Change one description and re-run. If the score moves, you are doing tool design with evidence rather than guessing.

## 6b. Distribution: making it work for any agent

- **The server is already portable.** MCP is a standard, so Claude Desktop, Cursor, VS Code/Copilot, Codex and Gemini CLI can all run
  it. The work is in making it *easy to install*, not in porting it.
- `uvx --from git+https://github.com/<user>/<repo> <script>` installs and runs straight from GitHub, with no clone or venv.
  It relies on `[project.scripts]` in `pyproject.toml`. Next step: publish to PyPI so it's just `uvx anki-mcp`.
- Config formats differ slightly: most clients use `{"mcpServers": {...}}`, VS Code uses `{"servers": {...}}`, and Codex uses TOML.
- **AGENTS.md** is the cross-agent instruction file (Codex, Cursor, Copilot, …). Claude Code reads CLAUDE.md, which can
  import it with `@AGENTS.md`, so there's one source of truth for all agents.
- Skills (SKILL.md) are less portable than servers. Put *capabilities* in the server and *personal policy* in the skill,
  so other people get the useful part.

## 7. Glossary

- **Host**: the app running the model (Claude Code). **Client**: the host's connection to one server. **Server**: your program.
- **JSON-RPC**: the wire protocol MCP uses. **Structured output**: tools can return typed JSON alongside text.
- **Agent loop**: model → tool call → result → model… until done. Your tool results are fed back into that loop.
