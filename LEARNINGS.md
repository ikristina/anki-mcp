# Learnings: building tools for agents

A running log of what matters when building MCP servers, skills and plugins. Add to it every session. Anything under "Gotchas hit" happened in this repo.

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
- **Errors must say what to do next.** Compare "deck not found" with `Deck 'Golang' does not exist. Similar: ['Go', 'Leetcode::golang']. Use list_decks.` With the second, the agent fixes itself.
- **Batch writes, and report per-item results.** A batch with 1 bad item should still add the other 49 and say which failed.
- **Make writes safe:** `dry_run`, tag everything the agent creates (`mcp-added`), no delete tool until you need one.
- **Constrain inputs with the schema** (`Field(ge=1, le=100)`, `min_length`). The SDK turns violations into errors the model can read, so it retries instead of flooding context.
- **Tool annotations** (`read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint`) tell the host how risky a call is. Hosts can use them for permission prompts.
- **Server `instructions`** are a system-prompt-ish blurb sent once at connect. Use it for cross-tool workflow ("call list_decks first").

- **Tool names:** snake_case, verb_noun (`describe_deck`, `add_notes`), consistent across the server. The spec doesn't mandate a case, but hosts prefix names (`mcp__anki__add_notes`) and the model sees tools from many servers, so a name must be clear on its own. The Python SDK takes the name from the function name.
- **Discovery tools pay off immediately.** `describe_deck`'s first run found things I'd missed by hand: a field the user had just added (`Audio` on the Latin note type), a tag typo (`duoling`), and a template referencing a nonexistent field. A tool that reports *facts about the data* beats hardcoding assumptions in prompts.
- Fuzzy suggestions: substring matching missed `Spansh`. `difflib.get_close_matches` (stdlib) on the last deck segment fixes it.

- **A capability hidden inside another tool's option is invisible.** Audio existed only as `add_notes(audio=…)`. Asked to "add audio to my Latin cards", another session saw no fitting tool and offered to write a script with Google's Latin voice (the one we'd rejected). The fix is a dedicated `add_audio` tool whose *description* carries the lesson ("Latin: espeak:la; Google 'la' is not Latin"). Knowledge that lives only in LEARNINGS.md or a skill doesn't reach sessions that don't load them.
- **Dry runs must validate everything that can fail**, not just the data. Voice validation originally ran only while generating audio, so a dry run approved Google `la`. Now `validate_voice` runs up front.
- Skills installed globally as **symlinks** (`~/.claude/skills/x -> repo/.claude/skills/x`): available in every project, one source of truth, updated by `git pull`.

- **A fake of the external system makes tests possible anywhere, and finds real bugs.** `tests/conftest.py` has `FakeAnki`, which answers the ~15 AnkiConnect actions the server uses from an in-memory collection. It *raises* on any action or search term it doesn't model, so the fake can't silently drift from what the server needs. Its first run found two bugs in `_plain` (the HTML stripper that also feeds TTS): `&nbsp;` left a doubled space, and inline tags became spaces (`Marcum <b>excitamus</b>.` → `Marcum excitamus .`). The smoke test never hit these, because the real data it checked had no inline HTML.
- Keep the two test layers: **unit tests** (fast, deterministic, CI, edge cases on purpose) and the **smoke test** (real stdio + real collection, catches API quirks the fake doesn't know about, such as `getDeckStats` returning leaf names).

## 3. Gotchas hit

- **MCP Python SDK 2.x (2026) renamed `FastMCP` → `MCPServer`** (`from mcp.server.mcpserver import MCPServer`). Most tutorials, blog posts and LLM training data still show v1. Always check the installed version and read the package source or migration guide. Also, v2 uses snake_case client attributes (`result.is_error`, not `isError`).
- **In SDK 2.x, only `ToolError` messages reach the model.** Any other exception gets reduced to `Error executing tool X`, which hides the useful message. Make your domain error subclass `ToolError`.
- **The stdio client does not pass your shell environment to the server.** It forwards only a safe default set. Pass env explicitly (`StdioServerParameters(env=...)`, or `claude mcp add -e KEY=value`).
- Don't trust your first test that "passes". The offline-error test passed for the wrong reason (the env var never arrived), and that is how the gotcha above surfaced. Make each test able to fail.
- Look at the real data before designing tools. This collection has 50 note types, including `Yanki - Basic` (cards synced from Obsidian). A tool that assumed `Front/Back` everywhere would have broken.
- **Wrapped APIs have quirks you only find by printing results.** AnkiConnect's `getDeckStats` returns the *leaf* deck name (`04_Transactions`), not the full path, so subdecks collided. The fix is to key by deck id.
- **Check a heuristic against the counts before trusting it.** "Every deck with Yanki notes is Yanki-owned" looked true until counts showed `Languages::Latin` = 2 Yanki / 464 total. The rule became: `yanki` only if all own cards are Yanki, `mixed` if some are.
- Read the other system's config instead of inferring. Yanki's `data.json` lists exactly which vault folders sync.
- **`describe_deck` can't help with an empty deck**, which is exactly when a user has just imported a shared deck's note type and wants to start filling a new deck. `list_note_types` fills that gap. Counting its notes uses `"note:<name>"`, escaped the same way as deck names (`_` and `*` are wildcards), so a type like `Basic_v2` doesn't match other types.
- **AnkiConnect's `sync` is safe to call unattended.** It runs `col.sync_collection()`, which does a normal sync and returns the required change *without* doing a full sync; anything but no-changes/normal raises `Sync status N not one of [0, 1]` (2 = conflict, 3 = full download, 4 = full upload; enum in `anki/sync_pb2.pyi`). Only then does it call `mw.onSync()`, which syncs media in the background, so audio lands on AnkiWeb a little after the tool returns. The tool turns the status number into a reason.
- **AnkiConnect handlers run on Anki's main thread**, so a modal dialog blocks every request until it closes. urllib then raises `TimeoutError` (not `URLError`), which the client must catch too, or the model sees only "Error executing tool".
- **An `.apkg` is a zip you can check.** After AnkiConnect's `exportPackage`, `export_deck` opens the file and counts the media entries (named `0`, `1`, ...) instead of trusting `result: true`. That catches an export that silently left out the audio. `includeSched=False` gives a share-ready deck where every card is new.
- The easiest way to reach a stdio server from a phone isn't a remote server: it's Claude Code Remote Control on the machine that already runs it. Remote HTTP (roadmap Phase 4) only adds value for hosts that can't run Claude Code.

## 3b. Server vs. skill: where each rule belongs

"Tech cards go to Obsidian, everything else goes to Anki" is split in two:
- **The MCP server enforces the invariant.** `add_notes` refuses Yanki-owned decks, and its error says where to go instead. This holds no matter which agent, prompt or model calls it.
- **A skill carries the policy/workflow** (`.claude/skills/flashcards/SKILL.md`): how to route, the Yanki markdown format, and the reminder to sync. It composes two servers (`anki` + the existing `obsidian` one). There was no need to build an Obsidian writer, because a server already existed.
- Expose facts, not only rules: `list_decks` returns `source: yanki|anki|mixed`, so the agent can decide *before* it hits an error.
- **Data belongs in neither.** The first `language-cards` skill had a table of my decks, note types and voices. That made it useless to anyone else, and I had to edit the skill whenever the collection changed. Deck profiles (`get_deck_profile` / `set_deck_profile`, a JSON file) moved that data out. Now the skill holds only the workflow ("read the profile, follow it; no profile → describe_deck, propose one, save after the user confirms").
- Agent memory is just a tool that reads and writes a file. The design questions are: **who may write it** (the tool description says to save only after the user confirms), **what gets validated on write** (deck, note type, field names and voice are checked against Anki, so a bad profile fails at save time instead of weeks later), and **where it lives** (outside the collection, so saving never triggers an Anki sync). Separate *inferred* facts (`describe_deck`, recomputed each time) from *decided* ones (the profile). An inference can go stale; a decision is the user's to change.
- "Passes on my machine" isn't CI. The profile tests passed locally because `espeak-ng` is installed on my Mac, and failed on CI's Ubuntu runner, which doesn't have it. Unit tests shouldn't depend on system binaries, so the profile tests stub the eSpeak check. To reproduce CI's environment before pushing: `env PATH=/usr/bin:/bin .venv/bin/python -m pytest -q`.
- Tests must isolate any file the server writes: the `anki` fixture points `ANKI_MCP_PROFILES` at `tmp_path`. Otherwise a test run would overwrite the real profiles.

## 3c. Adding audio (TTS) to language cards

- **Mine the user's existing setup before picking a tool.** HyperTTS's presets (in the add-on's `meta.json`, which I read without printing API keys) showed that audio came from the *Google Translate* voice (`es-MX`, `fr`). `gTTS` calls the same free voices, so new cards sound like old ones, with no key and no cost.
- **Put the side effect inside the tool, not in a second tool.** `add_notes` takes an optional `audio` spec. The server synthesizes the MP3 and passes it to AnkiConnect's `addNotes` `audio` option, which stores the media file and inserts `[sound:…]`. An agent can't forget the audio step or insert a wrong filename.
- **Failure policy is a design decision.** If TTS fails, the note is *not* added (errors say so). The alternative, adding it silently without audio, would break the "every language card has audio" invariant (3,408/3,408 today).
- **"Supported" in a list isn't the same as "works". Listen to or look at the output.** gTTS lists `la` (Latin), and it produced a valid MP3, so all automated checks passed. It sounds like English text read aloud. Only a human listening caught it. For generated media, a person has to check a sample before you make it the default.
- **Same problem, second tool:** `say -v Nobody` silently uses the default voice instead of failing. Wrappers should turn silent fallbacks into explicit errors (check the voice list first), or the agent will "succeed" with wrong audio.
- Engine choice lives in the voice string (`es-MX`, `espeak:la`, `macos:Alice`). One parameter the agent already understands beats a new `engine` field plus rules about which combinations are valid.
- Latin voices checked: Google `la` (fake), Microsoft/Bing (none of 322 voices), Meta MMS-TTS `lat` (Italian-style pronunciation, not what the user is learning). eSpeak NG `la` is robotic but pronounces Latin by Latin rules, so it's the pick.
- **A smoke test that only prints is not a test.** The first version printed output that *looked* fine, but it showed only the first item of each list, and its "valid card" case had silently turned into an error case after the Yanki guard. Now every check asserts an expected result and the script exits non-zero on failure.
- CLI tools have format quirks: `espeak-ng` streams WAV with placeholder sizes that `afconvert` rejects, so the fix is to rewrite the header with Python's `wave` module.
- `open_world_hint=True` on `add_notes` now, because it calls an external service.
- **Skills can be commands.** `.claude/skills/language-cards/SKILL.md` triggers automatically, or runs as `/language-cards la cumbre, el atardecer`. `$ARGUMENTS` in the skill body receives the text after the command.
- Hardcoding the user's conventions (deck, note type, field names, voice) in the *skill* is fine. That's personal policy. The *server* stays generic: it validates field names against Anki rather than knowing about Spanish.
- **Cloze decks need `cloze_only`.** The stock `Cloze` note type has no audio field and the word lives mid-sentence, with IPA typed inside the deletion (`{{c1::virile /ˈvɪr.əl/}}`, `{{c1::extraneous (/…/)}}`). `add_audio(cloze_only=True)` speaks only the deletion text with IPA and `::hints` dropped, into `Back Extra` (no schema change). Notes without a real cloze are counted as `skipped_no_cloze`; in practice those were typos (`{{c1:word}}`, one colon) that Anki doesn't hide either.
- **An MCP server keeps running old code until reconnected** (`/mcp` → reconnect). A new tool parameter is silently ignored by the old process, so the first dry run after a change must show the new behavior before writing anything.
- **A good voice still mispronounces rare words, and nothing detects it.** Google `en-US` (and `en-CA`) said *chimerical* wrong while `en-GB` matched Merriam-Webster; *kvetch* was wrong in every Google voice and only `macos:Samantha` got it. The MP3 is valid either way, so the agent can't know; only the user listening does. What works: generate the word in several voices to a scratch dir, let the user pick, then re-voice just that note. `add_audio` never overwrites, so the user clears the audio field first. The fallback order lives in the deck profile's conventions, not in the server.
- **Shared note types can hold logic in the template's own JS; read it before choosing a field format.** The Memrise (Lτ) Tapping+Typing preset builds its tapping tiles from all of the answer's text, so `;` alternatives got scrambled into the tiles. Alternatives go in `<span class="alt">a|b</span>`, which is kept out of the tiles but still accepted as an answer. The same templates auto-rate a correct answer as Good after 1.5 s, which cuts off the audio. The undocumented fix is `<script>window.alwaysShowInfo = true;</script>` at the top of each back template (`window.flipDelay = <s>` only lengthens the wait). `#error loading answer!` in Preview is expected: the back reads the answer from `sessionStorage`, and Preview never sets it. The Memrise add-on (884199977) breaks the editor on Anki 26.09 (`NewEditor` has no `.note`), but the templates work without it.
- **Tapping cards tested little, so the Spanish deck moved to multiple choice, and decoys are the whole card.** With Choices empty, tapping tiles are only the answer's own words: a one-word note is a single tile, and a sentence only tests word order. The Lτ MultipleChoice+Typing preset is a drop-in swap with the same fields (the first is `Learnable`, not `Learnable Sentence`) and the same card order. Its front is wrapped in `{{#Choices}}`, so a note with no decoys gets a **blank** card. It shows 6 options (`window.mchOptionsN`, default 6), meaning 5 decoys, split on `|`, and the answer is left out of Choices. Choosing the right option only tests something when the decoys are near-misses (wrong person, ser/estar, a swapped season). It's broken when a decoy is also correct: "you" in the English makes `tú`/`ustedes`/`usted` all valid, so those swaps are banned in the deck profile. Changing the note type is a schema change and forces a full sync, so the Choices were filled on the old note type first (the field carries over).
- **Template fixes live on the note type, so switching note types silently drops them.** After the move to the MultipleChoice preset, sentence audio was cut off again: `window.alwaysShowInfo = true` had only ever been added to the old Tapping preset's backs. The new preset's type-in fronts also shipped the demo on-screen keyboard (`<setting id="static_keys">this layout is customizable</setting>`), shown on phones as letter tiles spelling that sentence. Emptying `static_keys` (and `random_keys`) leaves plain typing. Editing template text via `updateModelTemplates` is not a schema change, so it syncs normally. Diff the old and new templates before moving notes.
- **An edit tool needs a raw read path.** `get_notes` strips HTML to keep results small, so a model editing from it would quietly erase `<b>`, alt spans and `[sound:]` tags. `update_notes` therefore comes with `get_notes(raw=True)`, only changes the fields it is given, and refuses any edit that drops a `[sound:]` tag. It is a dry run by default, like `add_audio`. TTS text also skips `<span class="alt">`, so a note with alternatives is voiced with only its main answer.
- **Training a Latin voice ([docs/building-a-tts-voice.md](docs/building-a-tts-voice.md)).** Fine-tuned Piper from `en_US-lessac-medium` on one reader (~5.4 h) of Vox Classica, a CC-BY sentence-aligned Classical Latin corpus. Piper phonemizes with eSpeak, so Latin *rules* come from `espeak:la` and only the sound gets neural. Gotchas: the old checkpoint needs `--model.warmstart_ckpt` (not `--ckpt_path`, which hits torch's `weights_only` load), and Piper saves `warmstart_ckpt` in the hparams, so every *resume* silently re-copied the base weights over the trained ones until `train.py` skipped it. Found only by diffing a weight tensor across a resume; the logs said "Restored all states".
- **Macrons decide Latin stress in eSpeak.** `cīvitātēs` → `kiːwɪtˈaːteːs`, `civitates` → `kɪwˈɪtatɛs` (wrong stress). The training text is macronized; the 232 Latin cards have none. So for the Piper voice, `audio.text` should be the macronized Latin while Front stays as typed: a deck-profile convention, not code.
- **`delete_notes` was added only when the user hit the need (2026-11-16).** Bulk-adding Duolingo screenshots produced overlapping sentences, and the only cleanup route was tagging `dupe` and deleting by hand in Anki's browser. The tool is a dry run by default, takes explicit ids (no search query, so it can't delete "everything that matched"), caps at 50, and refuses Yanki notes. Duplicate detection at add time is still the better fix: `add_notes` only sees an identical first field in the same deck, so search for the key phrase before adding.
- **Re-voicing a deck needs an explicit overwrite.** `add_audio` skips notes with audio (that makes it re-runnable) and `update_notes` refuses to drop `[sound:]`, so a new voice couldn't reach old cards. `replace=True` plus per-note `texts` (for the macrons) fixes that. It still converges because media names are `sha224(voice|text)`: a note already holding the file for this exact voice + text is skipped as done.
- **Audio that plays only on tap: `<audio controls src="file.mp3">`, not `[sound:]`.** Anki autoplays every `[sound:]` on a side, and the only off switch (deck option "Don't play audio automatically") is all-or-nothing. A plain HTML `<audio>` element in a field is left alone, so an example sentence can have its own player while the word still autoplays. Tested 2026-10-02 on desktop and AnkiMobile (iPad). Not yet checked: whether Tools → Check Media counts the file as used.

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
- **Cost control per skill:** frontmatter `model: haiku` + `effort: low` switches models while the skill runs (for the rest of that turn; the session model comes back on your next prompt). `allowed-tools` pre-approves the MCP tools so there are no permission prompts. The MCP server itself never costs tokens. Only the model driving it does. Rule of thumb: mechanical work (vocab cards) → Haiku; work that needs judgment or fact-checking (technical cards) → a stronger model.
- `CONNECTION_CLOSED` means the server process died at startup. Debug with `claude mcp get <name>` and run the exact command yourself. Here, `--directory` pointed at `src/anki-mcp` instead of the project root (where `pyproject.toml` lives). Use absolute paths, including for `uv` itself (`/Users/.../.local/bin/uv`), because GUI apps may not inherit your shell's PATH.
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
- Eval case = prompt + expected tool(s) + checks on arguments/result. Example: "add a card about Go channels to my Go deck" should call `add_notes` with deck=`Go`, and should not call `add_notes` 3 times with 1 note each.
- Change one description and re-run. If the score moves, you are doing tool design with evidence rather than guessing.

What building the suite taught (details and numbers in `docs/evals.md`):
- **Isolate the eval session like a test, and assert the isolation.** `--strict-mcp-config` plus `--setting-sources project` keeps the real `anki` server, user plugins and `~/.claude/CLAUDE.md` out. Each run's `system/init` event is checked for exactly the expected servers, plugins and skills. That check failed every run the first time, because Claude Code had auto-updated two minutes earlier and added a bundled skill. Without the check, a changed environment would have looked like a model difference. Record the Claude Code version in every result.
- **Skill frontmatter overrides `--model`.** `model: haiku` in a skill switches the model back for the rest of the turn, so a Sonnet run of that skill is really a Haiku run. The runner copies the skills into a temp dir and rewrites `model:`. `effort: low` still applies, so the comparison is between models at low effort.
- **Fakes make agent evals safe and cheap to trust.** The server runs unchanged with `invoke` patched to FakeAnki, and `ANKI_CONNECT_URL` is set to a dead port first, so an unpatched path fails instead of reaching the real collection. For a second server the skill uses (Obsidian), a record-only stub with the real tool names and schemas turns "did it route to Obsidian?" into a checkable tool call, not a regex over the reply.
- **Store the raw stream and make scoring re-runnable.** A check that crashed on a run with no create call was fixed and re-applied to the stored streams with `--rescore`: no re-run, no extra usage.
- **Evals find documentation gaps that unit tests can't.** The flashcards skill never named the Obsidian create tool, so Haiku reached for `Write` with a path outside the vault. `mixed` decks accept `add_notes` by design, so when Haiku ignored the skill's routing table it put a technical card in `DDIA`, a parent deck made up only of Yanki subdecks.
- **Pydantic ignores unknown keys by default, and models make them up.** The model copied `text_field` from the deck profile into `add_notes`' `audio`, which has no such field, and it was dropped silently. For agent-facing inputs, `extra="forbid"` with a fix-it message is the safer default.
- **Making one input optional changes what the model leaves out.** Once `note_type` could come from the deck profile, Haiku treated the whole profile as automatic and dropped `tags` too (7/120 runs). Filling tags from the profile as well fixed it. Default the whole profile, or none of it.
- **Score what the server did, not only what the model sent.** After those defaults, the checks read the "(from deck profile)" notices in the result. Reading only the call arguments scored 0/30 on runs that were correct.
- **Haiku vs Sonnet (5 runs each):**
  - Mechanical cases: equal.
  - Haiku lost on judgment: an ambiguous gender, and "preview" not treated as a dry run.
  - Haiku saved less than its price suggests: 0.68× Sonnet's cost and 1.6× the time, because it took more turns, mostly loading deferred MCP tools one at a time.

## 6b. Distribution: making it work for any agent

- **The server is already portable.** MCP is a standard, so Claude Desktop, Cursor, VS Code/Copilot, Codex and Gemini CLI can all run it. The work is in making it *easy to install*, not in porting it.
- `uvx --from git+https://github.com/<user>/<repo> <script>` installs and runs straight from GitHub, with no clone or venv. It relies on `[project.scripts]` in `pyproject.toml`. Next step: publish to PyPI so it's just `uvx anki-mcp`.
- Config formats differ slightly: most clients use `{"mcpServers": {...}}`, VS Code uses `{"servers": {...}}`, and Codex uses TOML.
- **AGENTS.md** is the cross-agent instruction file (Codex, Cursor, Copilot, …). Claude Code reads CLAUDE.md, which can import it with `@AGENTS.md`, so there's one source of truth for all agents.
- Skills (SKILL.md) are less portable than servers. Put *capabilities* in the server and *personal policy* in the skill, so other people get the useful part.

- **`getDeckConfig` returns the preset, not the deck's effective limits.** A "This deck" new-cards/day override set in deck options (newer Anki) doesn't show up: the preset said 10/day while the deck really ran at 150. Don't quote study-time estimates from it without asking the user.
- **Similar-name hints for a *new* deck must compare last segments only.** `_similar_decks` on the full name flags every parent and sibling (`Languages`, `Languages::English` for `Languages::Pimsleur Spanish 2`); `create_deck` matches the leaf against existing leaves instead.

## 6c. Observability (OpenTelemetry)

- **MCP Python SDK 2.x already emits traces.** `OpenTelemetryMiddleware` is on by default and opens a SERVER span per request (`tools/call <tool>`, with `gen_ai.tool.name`), and it continues W3C trace context from `_meta`. It uses only `opentelemetry-api`, so it does nothing until someone installs an SDK provider. Our spans nest under it.
- **Write instrumentation against the API, and configure the SDK only in `main()`.** Then the instrumentation costs nothing in tests and default installs, and the SDK/exporters can be an optional extra. `get_tracer`/`get_meter` at import time return proxies that pick up the real provider later.
- **Tool errors don't reach middleware as exceptions.** An `AnkiError` becomes a `CallToolResult(isError=True)` (as a wire dict by the time middleware sees it), so outcome detection has to inspect the result, like the SDK's own middleware does.
- **Add user middleware with `MCPServer(middleware=[...])`.** It runs inside the SDK's OTel middleware, so `trace.get_current_span()` there is the `tools/call` span, and deck names can be added to it.
- **stdio servers get SIGTERMed, and SIGTERM skips atexit**, so the batched spans/logs and the last metrics were lost. `setup()` installs a SIGTERM handler that shuts the providers down (which flushes them). Metrics export every 10s rather than the default 60s.
- **Tests:** the global providers can be set only once per process, so `test_telemetry.py` installs in-memory ones once, with *delta* metric temporality so each test sees only its own points. The real OTLP wiring is tested in a subprocess against a fake HTTP receiver, which also asserts that stdout stays empty.

## 7. Glossary

- **Host**: the app running the model (Claude Code). **Client**: the host's connection to one server. **Server**: your program.
- **JSON-RPC**: the wire protocol MCP uses. **Structured output**: tools can return typed JSON alongside text.
- **Agent loop**: model → tool call → result → model… until done. Your tool results are fed back into that loop.

## edge TTS engine (2026-10-22)
- `edge:<voice>` uses the unofficial `edge-tts` package (Edge Read Aloud endpoint; online, no key, MP3 out, no afconvert).
  Male Spanish: `es-MX-JorgeNeural`, `es-ES-AlvaroNeural`, `es-AR-TomasNeural`, `es-CO-GonzaloNeural`, `es-US-AlonsoNeural`.
  Unofficial, so it can break; errors tell the user to retry or add the note without audio. The official route is
  Azure AI Speech (free F0 tier, ~500k chars/month, needs a key) if this ever stops working.
- `random:a,b` voices are resolved per text by hash (not `random()`), so re-running audio or `replace` checks give the same voice and filename.

## Semantic index (Phase 8.1–8.2, 2026-11-23)
- **Eval before model choice, and the baseline must be fair.** My first keyword baseline searched the full note text, which contains the English side, so it "won" the cross-language set (0.87 recall@5). Handicapped to the foreign side like the embeddings, it scored 0.03 vs MiniLM 0.80. On near-duplicates (`cumbre` vs `la cumbre`) plain substring matching still wins (0.97 vs 0.55), so the duplicate warning should be lexical first, embeddings second.
- fastembed has no multilingual-e5-small or bge-m3. Its multilingual options: paraphrase-multilingual-MiniLM-L12-v2 (0.22 GB, chosen), -mpnet-base (1 GB), multilingual-e5-large (2.2 GB), potion-multilingual-128M (fast but weak: 0.52 cross). MiniLM embeds all ~10.5k notes in about 105 s on the Mac.
- **Redis TAG queries: escape only ASCII punctuation and spaces.** A backslash before a non-ASCII character (the 🇪🇸 in a deck name) is `SEARCH_SYNTAX Syntax error`. Only a live check found this; FakeAnki-style unit tests can't. Use `SEPARATOR "|"` on TAG fields because the default `,` would split deck names containing commas.
- `redis:8` ships the search module (`FT.*`), vectorset, ReJSON and timeseries. `FT._LIST` returns index names as bytes.
- Anki bumps a note's `mod` when fields or tags change, not when a card changes deck, so incremental indexing misses moves; `--full` fixes it. `notesModTime` is the cheap way to get every note's mod in one call.
- Blank notes store nothing in Redis, so they are re-read on every run (counted as `skipped_empty`) but never embedded.
- Colima (not Docker Desktop) provides Docker on this Mac and doesn't autostart after a reboot: `colima start` first.
- **Write the index metadata when the index is created, not at the end of the run.** The first real run died midway (a missing card broke `getDecks`); with no model recorded, the next run read "no model" as "model changed" and wiped the 3,600 notes already embedded. `ensure_index` now records the model immediately and only rebuilds when a recorded model differs. `getDecks` also fails a whole batch on one missing card, so `_card_decks` retries one card at a time.

## Near-duplicate warnings (Phase 8.4, 2026-11-23)
- **Lexical first, semantic second.** The same word apart from article/case/accents is found with Anki's own search (`findNotes "nc:<word>"`, then an exact comparison of the normalised first field), so it works with no index. Embeddings add the paraphrases. Plain-text search is accent-sensitive in Anki (`rapido` does not find `rápido`); `nc:` ignores accents. FakeAnki implements `nc:`.
- The semantic threshold 0.8 (`rag.SIMILAR_THRESHOLD`) is a starting value from spot checks (a paraphrase scored 0.82; related-but-different notes 0.5-0.75), not tuned on a labelled set. Retune it if the warnings are noisy or miss real near-duplicates.
- **A unit test must never reach the real Redis or load the model.** Once `add_notes` called the index, the existing tests silently searched the user's real index (two failed because real hits leaked into results). The shared `anki` fixture now replaces `rag.get_store` / `rag.get_embedder` with a function that raises; tests that need an index patch them with fakes.
- The warning step runs on notes Anki already accepted, before they are added, so a note never matches itself. First call after server start adds about 3 s (model load); later calls are fast.

## move_cards (Phase 3, 2026-11-23)
- AnkiConnect `changeDeck` moves *cards*, keeps scheduling and history, and creates the target deck if it's missing, so the tool checks that the deck exists first (a typo would otherwise make a new deck). It does not bump the note's modified time, which is why the incremental indexer can't see a move: `move_cards` updates the index's deck tag itself (`RedisStore.set_decks`, only for notes already indexed, only when the note's first card moved, since that card names the note's deck in the index). `--full` still repairs any drift.
- A search that matches only some cards of a note moves only those; the result reports `notes_left_partly_behind`. The 200-card cap is the guard against a too-broad query; there is no per-card undo, so the result lists the source decks (`from`).
- `cardsInfo` gives `modelName`, which is how Yanki cards are recognised and skipped (the same prefix rule `delete_notes` uses).

## convert_notes (Phase 3, 2026-11-23)
- AnkiConnect's `updateNoteModel` converts one note: it changes the note type, then sets **every** field of the new type from the `fields` you pass and **replaces the tags** (omitted tags are cleared). So the tool computes the new field values itself from `field_map`, and passes the old tags plus `mcp-converted`. The installed add-on (2055492159) maps card templates by name; a source with more than one template and a target whose template names aren't all among them raises "Explicit conversion mappings are required" on the first note, after earlier notes in the batch may already have changed. The tool checks template names up front (`modelTemplates`) and refuses, so a batch can't half-convert for that reason.
- Changing a note type is a schema change, so the next sync needs "Upload to AnkiWeb". This server's `sync` refuses full syncs on purpose; the result's `next` tells the user to do it by hand on the desktop first, then the phone.
- The backup is `exportPackage` with scheduling for every deck holding an affected card, into `<backup dir>/<timestamp>[-n]/`, and the conversion refuses to start if any export fails or isn't a readable zip (reusing `export_deck`). Timestamps collide when two runs start in the same second (found by a test), so the folder gets a `-n` suffix. Anki also makes its own automatic backups.
- The trial rule keys on the history file: more than 20 notes are refused until some notes of that exact source -> target pair were converted before. Unmapped source fields with content must be listed in `drop_fields`.
- Fields are copied verbatim, so converting Cloze notes to a non-cloze type leaves `{{c1::…}}` in the text. A real cloze-to-vocabulary redesign needs text rewriting first (`update_notes`), then conversion.

## create_note_type (2026-11-23)
- AnkiConnect `createModel(modelName, inOrderFields, cardTemplates=[{Name, Front, Back}], css, isCloze)` makes a new note type and fails on a duplicate name. It cannot edit an existing type here (`updateModelTemplates`/`updateModelStyling` exist but are not wrapped, on purpose: editing a type that has notes is a riskier schema change).
- Whether adding a note type forces a full sync is documented nowhere I could find; forum reports say it does (one-way sync prompt on desktop, then a choice on AnkiDroid). The tool assumes yes and uses the same confirmation as `convert_notes`.
- A standard Anki note type can't hold both `{{cloze:}}` and ordinary templates. The user's "cloze" meant a hint, so English Vocab shows the gapped sentence (field `Gap`) as a hint on the Recall card, whose answer is typed with `{{type:Word}}`.
- The placeholder checker handles `{{#F}}…{{/F}}`, `{{^F}}`, and filters (`type:`, `cloze:`, `hint:`); `FrontSide`, `Tags`, `Deck`, `Card` etc. are always allowed. Anki's dark mode adds the class `nightMode` to the card, which the CSS keys on.

## suspend_cards (2026-11-23)
- Suspending leaves interval, ease, due date and history alone; unsuspending restores the card's queue, so review cards that came due meanwhile are overdue at once. `limit` lets a big backlog come back in slices.
- `is:new` in the query is how "hide only what I haven't started" works; learned cards keep their reviews. AnkiConnect `suspend(cards)` took 7,354 cards in about 5 s.
- The tool tags suspended notes `mcp-suspended` so the undo touches only its own work, not cards the user suspended by hand. Yanki notes are not tagged (Yanki owns their tags and may overwrite or flag the change), so for them undo is "any suspended Yanki card in the search". Unknown: whether a Yanki sync resets suspension. Cockroach Labs (22 Yanki cards) was suspended first as the test; check it after the next Yanki sync.
- Avoid parentheses in queries built by tools: FakeAnki's search parser doesn't do them. `suspend_cards` finds the cards and filters with separate `is:suspended` searches instead of appending terms.
- `_` is a single-character wildcard in Anki search: use `_deck_term` (escapes it) for decks like `Norwegian_art`.

## sync does not confirm or refuse (2026-11-23)
- The installed AnkiConnect (2055492159, new "services" build) runs `sync` asynchronously: the call returns as soon as the request is accepted. When AnkiWeb requires a full sync it does not raise: desktop Anki shows "Anki requires a full sync. Choose the authoritative collection" with Upload / Download / Cancel buttons and waits for the user (gui.py, `state('waitingForUser')`), and later AnkiConnect calls queue behind that dialog. The old "Sync status N not one of [0, 1]" error (and `_FULL_SYNC_REASONS`) only applies to older builds. So `synced: True` means "requested", and a repeated call can re-raise the dialog. The tool now says so.
- To see whether a sync really finished, read a copy of `~/Library/Application Support/Anki2/<profile>/collection.anki2` (copy first, open read-only): table `col` has `scm` (schema last modified, ms), `ls` (last successful sync, ms) and `mod`. `scm > ls` means the local schema changed since the last sync and a full sync is expected. `notetypes.mtime_secs` and `decks.mtime_secs` show what changed recently.

## Yanki and parallel syncs (2026-11-23)
- A Yanki sync of a new vault folder (`Fly`, 199 markdown notes) produced 398 Anki notes: every note twice, ids about a second apart. That pattern means two syncs ran at once (an automatic one plus a manual press, or two triggers). Sync once and wait for it to finish. The user deleted the folder in Obsidian and the deck in Anki; Yanki notes went back to 491 with no duplicates.
- Suspension of Yanki cards survives a Yanki sync (Cockroach Labs stayed 22/22 suspended), so `suspend_cards` is safe on Yanki decks.
- A full-sync dialog on a *second* computer is expected when that computer's collection is older than AnkiWeb's: Download there is right if it has no unsynced work. The dialog on the main computer was a false alarm (the user had pressed Sync on the other machine).

## English redesign (2026-11-23)
- Created `English Vocab` (examples/note_types/english-vocab.json) and converted all 179 `Languages::English` notes (178 Cloze, 1 Cloze+) with `convert_notes` mapping Word<-Text, Audio<-Back Extra, then rewrote the fields. Each old card became the Recognition card (history kept); Recall cards are new (197 new cards incl. 18 that were already new).
- `convert_notes` only copies fields, it can't split text. Second step was parsing the old `(pos) definition. Example with {{c1::word}}` text with a script (scratchpad, not in repo): strip HTML, pull IPA out of the cloze, split definition from example at the first sentence containing `{{`. 13 notes had extra dictionary text and 10+ needed manual fixes; run a flagging pass (lowercase starts, odd IPA chars, double spaces) before writing.
- IPA: free dictionaryapi.dev (curl works; Python urllib got blocked) covered most words; it mixes IPA styles (ɹ, ɛ, d͡ʒ, (r)), so normalise. Some entries are junk (`/joke/`, `/-də-/`): check every IPA that is ASCII-only or has brackets.
- Inflected clozes (`dissented`, `withered`...) were changed to the base verb in Word; the gap keeps any suffix that sat outside the cloze.
- Field rewrite of 173 notes went through AnkiConnect `updateNoteFields` directly (pasting them through `update_notes` risked typos); it only touched empty Definition fields of English Vocab notes.

## Restyling an existing note type (2026-11-23)
- AnkiConnect `updateModelTemplates` + `updateModelStyling` change the look and card layout of an existing note type. Checked `col.scm` (schema-modified time) before and after: unchanged, so no full sync. Read the collection with its `-wal` file copied too, otherwise a copy looks stale.
- Styling belongs to the note type, shared by every deck using it. "Basic (and reversed card)" is used by Pimsleur 1 and 2, SpanishRandom and Latin; restyling it changed all four. Old templates and CSS were saved to `~/.config/anki-mcp/backups/basic-reversed-style-*.json`.
- Script used: `examples/note_types/basic-reversed-index-style.py` (Index look: white card, one red dot, light type; dark mode via `.nightMode`). Not yet a server tool.

## Duplicate check scope (2026-11-23)
`add_notes` warned about near-duplicates across the whole collection, which is noise for a catch-all deck like SpanishRandom. Deck profiles now have `similar_scope` (`all` default, `deck` = only that deck and subdecks). It filters both the lexical (`"deck:X" "nc:word"`) and the semantic (`find_similar(deck=...)`) check.

## Index restyle on phones (2026-11-24)
- AnkiDroid showed horizontal and vertical scrolling and a card sitting too low. Fixes in the CSS: `*{box-sizing:border-box}` (the `min-height:100vh` plus padding otherwise overflows), zero `html, body` margin, `overflow-wrap:anywhere`, smaller paddings and gaps. If AnkiDroid still centres vertically, that is its own reviewing setting; CSS can't override it.
- `{{Subdeck}}` gives the last part of the deck name (`{{Deck}}` the full path), so one shared note type can show each deck's name in the tag line.
- A restyle only reaches the phone after desktop sync and then phone sync. Run `sync` right after the script; AnkiDroid may need the reviewer reopened.
