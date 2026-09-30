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

**Status: done** (`get_deck_profile`, `set_deck_profile`; `describe_deck` includes the saved profile). Not done yet:
the cached computed part (`describe_deck` is fast enough so far) and exposing profiles as MCP resources.

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

**Done:** audio backfill for existing notes (`add_audio`). Converting existing notes also enables **audio backfill**, e.g. adding `espeak:la` audio to the 233 Latin notes.
That's a regular field update, so no full sync is needed.

## Phase 4: Remote MCP (Streamable HTTP + auth), still on AnkiConnect

**Stopgap, done:** the `sync` tool plus Claude Code Remote Control already give phone access with the Mac running,
with no transport or auth work. See [docs/remote-from-phone.md](docs/remote-from-phone.md).

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

**Status: eval suite built (2026-09-30).** `evals/` holds 14 cases over `language-cards` and `flashcards`, run via `claude -p`
against FakeAnki and an Obsidian stub, with Haiku/Sonnet/Opus results in [docs/evals.md](docs/evals.md). Fixes for its
findings are in (`ef0be23`, `4b51524`, `37b82c0`, `7ee8c05`, `dbb29e2`) with re-run columns there. Next: a case for
every new workflow; Haiku's open misses (preview without a dry run, routing a new topic into an existing deck).

- Unit tests with a fake AnkiConnect, so CI runs without Anki.
- Eval suite: prompts → expected tool calls/arguments via `claude -p --output-format stream-json`. For example, "add these 5
  Spanish words" should produce one `add_notes` call with the right fields and gender codes, and a Yanki deck should be routed to Obsidian.
- Compare models (Haiku vs Sonnet) on the language-cards evals to back up the `model: haiku` choice with data.

## Phase 8: Retrieval (semantic search over the collection)

Read-only and low risk, so it can start at any time. It is also the learning project for RAG: embeddings, a vector
store, keeping an index fresh, hybrid search, reranking, and above all **evaluation**.

**Problem:**
- Anki's duplicate check is exact first-field text per deck and note type, so `cumbre` vs `la cumbre`, or a second card
  on "Raft leader election" worded differently, slip through.
- `search_notes` is keyword-only: "cards about consensus" misses notes that only say "Paxos", and "cat" doesn't find
  "el gato".

In MCP the server does **retrieval only**. The agent is the generator, so no LLM calls happen in the server.

**Decisions:**
- **Store: Redis 8** (its query engine includes vector search), in Docker with a volume and RDB persistence, next to
  otel-lgtm. It was chosen to learn Redis vector search, and because it does vector, full-text and tag filters
  (deck, note type) in one query, so hybrid search needs no hand-written merging.
  - At 14k notes × 384 dims × 4 bytes ≈ 21 MB, size isn't a factor.
  - The index is **derived data**: it can always be rebuilt from Anki, so persistence only saves re-embedding time.
  - Keep storage behind a small interface (`upsert`, `delete`, `search`), so that sqlite-vec (one file, no server) can
    be added later and compared on the same evals.
- **Everything stays optional:** a `rag` extra (embedding library + Redis client), and nothing changes without it.
  **Redis being down must never break existing tools.** The retrieval tools raise `AnkiError` with a fix-it
  (`docker start anki-redis`), and `add_notes` skips the similarity warning and says so in a hint.
- **Local, free embeddings**, like the TTS engines. The model must be **multilingual**, because the cards mix
  Spanish, French, Norwegian, Latin and English. The model is chosen by eval (8.1), not upfront. Candidates:
  multilingual-e5-small, paraphrase-multilingual-MiniLM, bge-m3. Also compare fastembed (ONNX, light) vs
  sentence-transformers (pulls in torch).
- **Text to embed:** plain text of the note's fields with HTML, `[sound:…]` tags, `<span class="alt">` alternatives and
  cloze markup stripped (reuse `_plain` / `_cloze_text`). Yanki notes are included: search is read-only.

**Redis layout:**
- Key: `anki:note:<note_id>`.
- Hash fields: `vector` (FLOAT32 bytes), `text`, `deck` (TAG), `note_type` (TAG), `mod` (NUMERIC, note modified time).
- Index: `idx:anki_notes`, HNSW, cosine distance, plus TEXT on `text` for hybrid.
- Key `anki:index:meta`: embedding model name, dimension, and last indexed time. **If the model changes, rebuild**:
  vectors from different models aren't comparable.

**Steps:**
1. **8.1 Eval set and model spike (no server code).**
   - Build 50–100 (query → expected note ids) pairs from the real collection:
     - paraphrased questions
     - cross-language queries ("cat" → "el gato")
     - known near-duplicate pairs (`cumbre` / `la cumbre`)
     - technical synonyms (consensus → Raft/Paxos)
   - Metrics: recall@5 and MRR. Baseline: Anki keyword search.
   - Pick the embedding model on these numbers.
2. **8.2 Indexer.**
   - A `scripts/index_notes.py` (or `anki-mcp-index` entry point) does the full build.
   - Incremental updates re-embed only notes changed since the last run. Candidates: Anki's `edited:N` search term, or
     `notesInfo`'s modified time; check which AnkiConnect exposes cheaply.
   - Also remove notes deleted in Anki (diff the id sets).
   - Add spans and a duration metric via `telemetry.operation`.
3. **8.3 `find_similar_notes(text | note_id, deck=None, k=5)`**: read-only tool returning note previews with similarity
   scores. Unit tests use a fake store, and `FakeAnki` gets whatever new search terms the indexer uses.
4. **8.4 Near-duplicate warning in `add_notes`.**
   - Dry-run results gain `similar: [...]` per note above a threshold tuned on the 8.1 pairs.
   - It warns and never blocks.
   - `language-cards` shows the warning to the user before adding.
5. **8.5 Hybrid search.**
   - `search_notes(semantic=True)` or a separate tool: vector + full-text in one Redis query, with an optional reranker.
   - Keep it only if the eval shows it beats keyword-only and vector-only.
6. **Maybe later:**
   - Weak-card helper: retrieve related notes to explain a card you keep failing.
   - RAG over the Obsidian vault to generate grounded cards: real chunking, and the Obsidian MCP server as the source.

**Open questions:**
- Where the eval set lives. It quotes real note contents; `examples/profiles.json` is already public, but notes are more
  personal. Maybe keep it in the repo with ids and short texts, or gitignore it.
- Whether indexing runs on server start (incremental, capped), on demand (tool), or only from the script.

## Later / maybe

- PyPI release under a distinct name (`anki-mcp` is taken).
- Better Latin voice if one appears (Google `la`, Microsoft, and Meta MMS were all rejected; see LEARNINGS.md).
