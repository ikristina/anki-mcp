# Roadmap

Each phase is useful on its own and ships separately. Order goes from low risk to high risk: nothing that can damage
the collection comes before backups and a verified sync path.

Guiding rules for every phase:
- **The collection is irreplaceable.** Writes are dry-run first; destructive or schema-changing operations need a backup
  and an explicit user confirmation.
- **Capabilities in the server, personal policy in skills/profiles**, so the server stays useful to other people.
- Every new tool gets smoke-test checks, and every new workflow gets eval cases (Phase 7).

---

## Phase 1: Deck discovery (`describe_deck`)

**Problem:** the skills hardcode my decks, note types, field names and voices. Nobody else can use them, and they go
stale when my collection changes.

**Tool:** `describe_deck(deck)` works out the format from the data (read-only):
- note types used, with counts; field names in order
- per field: fill rate, typical length, whether it holds `[sound:…]` (i.e. it's an audio field), and the audio source
  (`hypertts-*`, `anki-mcp-*`, …)
- short categorical fields (e.g. `WordType`, `Gender`): top values, so new notes use the same codes
- tag patterns (e.g. `Spanish::Duolingo::<nn>_<Topic>`)
- 3 sample notes (plain text)
- `source`: anki / yanki / mixed

This is what I did by hand when designing the language skill. As a tool, any agent can do it for any deck.

## Phase 2: Deck profiles (memory)

**Problem:** some things can't be inferred and shouldn't be re-derived every session: which TTS voice a deck uses,
which field is spoken, conventions ("nouns include the article"), "never add audio here".

**Design:**
- `get_deck_profile(deck)` returns the **computed** part (from `describe_deck`, cached and refreshed when the note count
  changes) merged with **user-confirmed** overrides.
- `set_deck_profile(deck, {voice, audio_field, speak_field, conventions, …})` saves the overrides. Only after the user
  confirms them.
- Storage: a local JSON file (`~/.config/anki-mcp/profiles.json`). It's human-editable, and keeping it outside the
  collection means no schema changes. Later (headless, Phase 6) it could move into the collection, e.g. via note-type
  field descriptions (`modelFieldSetDescription`), which sync with Anki. **To verify:** whether editing field
  descriptions forces a full sync.
- Also expose profiles as MCP **resources** (`anki://deck/<name>/profile`), so a user can attach them to a chat.

**Result:** `language-cards` becomes generic ("read the deck profile, follow it"), and the table of my decks moves
out of the skill into my profile file.

## Phase 3: Reorganizing (move / convert)

Two different operations with very different risk:

| Operation | AnkiConnect | Review history | Sync impact | Risk |
|---|---|---|---|---|
| **Move cards to another deck** | `changeDeck` | kept | normal sync | low |
| **Convert note type** (e.g. `Basic` → `Spanish`, mapping Front→Word, Back→Meaning) | `updateNoteModel` | kept | **schema change → one-way full sync** (the "force push") | high |

- `move_cards(query, target_deck, dry_run=True)`: preview count and sample, then move.
- `convert_notes(query, target_note_type, field_map, dry_run=True)`: the preview shows before/after for a few notes and
  lists unmapped fields that would lose data. Refuses Yanki decks (Yanki would recreate or overwrite them).

**Safety protocol for conversions** (the tool enforces it, rather than trusting the model to remember):
1. Refuse unless the user confirms that **all devices are synced first**. Unsynced phone reviews are lost in a one-way sync.
2. **Back up** the affected decks with `exportPackage` (`.apkg`, including scheduling), plus a note that Anki keeps its own automatic backups.
3. Convert in a small batch first, then show the result for the user to check in Anki.
4. The user then syncs from **desktop** and chooses **Upload to AnkiWeb**. Other devices download once.
5. Log what was converted (note ids, mapping, backup path) in `~/.config/anki-mcp/history/` for recovery.

Converting existing notes also enables **audio backfill**, e.g. adding `espeak:la` audio to the 233 Latin notes.
That's a regular field update, so no full sync is needed.

## Phase 4: Remote MCP (Streamable HTTP + auth), still on AnkiConnect

- Add the **Streamable HTTP** transport. (The old SSE transport is deprecated in the MCP spec, so don't build it.)
- **Authentication is mandatory**: this server can read and write the collection. Use OAuth, which Claude's custom connectors
  expect, or at least a bearer token for personal use.
- Run on my Mac and reach it through Tailscale or a Cloudflare Tunnel. This teaches remote MCP with zero sync risk.
  Limitation: it only works while the Mac and Anki are running.

## Phase 5: Offline queue

- When AnkiConnect is unreachable, `add_notes` stores the validated notes (and generated audio) in a queue instead of failing.
- On the next call with Anki reachable (or via `flush_queue`), apply them and report the result.
- This covers most "add a word from my phone" use without touching the AnkiWeb protocol.

## Phase 6: Headless AnkiWeb backend (desktop-free)

**Phase 6.0, feasibility spike (one day), before any design work:**
- Try [`fastanki`](https://github.com/AnswerDotAI/fastanki) with a **throwaway AnkiWeb account**: log in, download,
  add one note with an audio file, sync, confirm on AnkiMobile/AnkiDroid that the card and the audio arrived.
- Check its maturity, media-sync support, and AnkiWeb's terms for unofficial clients.
- Go/no-go. If no-go, Phases 4–5 remain the mobile story.

**If go:**
- Backend interface: `AnkiConnectClient` (default) vs `AnkiWebClient`, chosen by `ANKI_BACKEND`.
- Sync **before and after** every write. **Never accept a full sync automatically**: stop and ask.
- Automatic backup before each write session; keep the last N.
- Credentials: log in once and store the **sync key** in the host's secret store, not `ANKIWEB_PASSWORD`.
- Linux-compatible audio: eSpeak via apt, and `ffmpeg` instead of macOS `afconvert`. `macos:*` voices are unavailable there.
- Dockerfile and a deployment guide (Fly.io / Railway / VPS).

## Phase 7: Evals and tests (continuous, start now)

- Unit tests with a fake AnkiConnect, so CI runs without Anki.
- Eval suite: prompts → expected tool calls/arguments via `claude -p --output-format stream-json`. For example, "add these 5
  Spanish words" should produce one `add_notes` call with the right fields and gender codes, and a Yanki deck should be routed to Obsidian.
- Compare models (Haiku vs Sonnet) on the language-cards evals to back up the `model: haiku` choice with data.

## Later / maybe

- PyPI release under a distinct name (`anki-mcp` is taken).
- Better Latin voice if one appears (Google `la`, Microsoft, and Meta MMS were all rejected; see LEARNINGS.md).
