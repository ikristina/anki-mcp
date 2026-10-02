# Agent evals: do the skills make the right tool calls?

The unit tests check the server and the smoke test checks it against the real collection. This suite checks the other half: given a prompt, does the **model** call the tools correctly? That means the right deck, fields, audio and batching, a dry run before writing, and Yanki decks routed to Obsidian. Prompts go in through `claude -p --output-format stream-json`, and the tool calls in the stream are scored.

## Run it

```bash
uv run python evals/run.py --case es-batch --models haiku              # one case, one run
uv run python evals/run.py --models haiku sonnet --repeats 5           # everything (140 sessions)
uv run python evals/run.py --models opus --repeats 5 --skill language-cards
uv run python evals/run.py --rescore evals/results/<file>.jsonl        # re-apply edited checks, no new runs
```

Each run is a real Claude Code session and counts against your plan usage. The full Haiku + Sonnet run took about 11 minutes with 4 jobs in parallel. Results (one JSON line per run, including the full stream) go to `evals/results/`, which is gitignored.

## How a run is isolated

Nothing can reach the real collection or vault:

- **The two MCP servers are fakes.** The `anki` server is `evals/fake_server.py`, the real server with `invoke` patched to the in-memory `FakeAnki` from `tests/conftest.py`. It sets `ANKI_CONNECT_URL` to a dead port first, so an unpatched path fails instead of reaching Anki. The `obsidian` server is `evals/fake_obsidian.py`, a record-only stub with the real server's tool names, arguments (`additionalProperties: false`), `{"ok": true, "data": …}` envelope and vault id `personal`. It is seeded with one Go card for the duplicate case.
- **Flags:**
  - `--strict-mcp-config --mcp-config <tmp>` loads only those two servers. The user-scope real `anki` server is not loaded.
  - `--setting-sources project` drops user settings, user plugins and `~/.claude/CLAUDE.md`.
  - Bash, Write and Edit are denied. MCP tools and `Skill` are allowed.
- **Fresh temp workdir per run.** It holds a copy of `.claude/skills/` with `model:` rewritten to the model under test. Without that, `language-cards`' `model: haiku` would switch Sonnet and Opus runs back to Haiku.
- **Guard:** each run's `system/init` event must show exactly the servers `anki` and `obsidian`, no non-built-in plugins, and no skills other than the two repo skills and Claude Code's bundled ones. Otherwise the run fails. This caught a real change: Claude Code auto-updated from 2.1.285 to 2.1.286 two minutes before the first full run and added a bundled `plugin-authoring` skill. Every run failed the guard until that skill was allowlisted. Each row now records `claude_code_version`, and the summary warns if it changes during a run.

**`effort: low` is pinned too.** `language-cards` sets both `model: haiku` and `effort: low`. The eval rewrites only `model`, so every language-cards run, Sonnet and Opus included, ran at low effort. The comparison below is of models at low effort, and the Haiku verdict is about Haiku at low effort.

## What was tested

- **Code under test:** `b46a0f7` (Piper TTS engine; its voice `Field` descriptions are part of what the model saw).
- **Skill fix before the baseline:** `b793bea` changed the flashcards skill's search tool from `obsidian_simple_search` to `obsidian_search_vault`, the real name.
- **Environment:** Claude Code 2.1.286, 2026-09-30, 5 runs per case per model.

| # | case | prompt (short) | main checks |
|---|---|---|---|
| 1 | es-batch | `/language-cards` 5 Spanish words, "no need to confirm" | dry run first; **1** real `add_notes` with all 5; deck/note type; `la ventana`, `el mapa` (m); Gender, WordType; audio `Audio`/`es-MX`; profile tag |
| 2 | es-dup | 3 words, one already in the deck (`cumbre`) | duplicate left out, user told |
| 3 | es-natural | "save these Spanish words", no slash command | skill triggers; deck, fields, audio |
| 4 | es-ambiguous | `cometa` (el = comet, la = kite) | asks (or adds both) |
| 5 | es-dry-only | "just show me, don't add" | a dry run happens; nothing written |
| 6 | la-profile | a Latin sentence | `Basic (and reversed card)`, `espeak:la`, plain `audio.text`, tag `latin`, no `[sound:` in Front |
| 7 | la-voice-trap | "use Google's Latin voice" | nothing added with `la`; explains eSpeak |
| 8 | yanki-go | a card for the Yanki `Go` deck | no `add_notes`; `list_decks` first; searched vault; one `obsidian_create_note` under `03 resources/Anki/Go/`, kebab-case ≤60, front/`---`/back, no `noteId`; Yanki sync reminder |
| 9 | yanki-subdeck | `DDIA::04_Transactions` | same, under `…/DDIA/04_Transactions/` |
| 10 | tech-no-deck | a Kafka card, no deck exists | new vault folder; no `add_notes`; "add the folder to Yanki" |
| 11 | yanki-dup | a Go card that already exists in the vault | search finds it; no create |
| 12 | anki-basic | a Default-deck card | `Basic` Front/Back via `add_notes`, not Obsidian |
| 13 | anki-batch | 3 Default-deck cards | **1** `add_notes` with all 3 |
| 14 | vocab-via-flashcards | "make flashcards for" Spanish words | handed to `language-cards`; audio |

## Results

| case | Haiku | Sonnet | Opus | after fixes: Haiku | after fixes: Sonnet | after P3: Haiku | after P3: Sonnet |
|---|---|---|---|---|---|---|---|
| es-batch | 5/5 | 5/5 | 5/5 | **2/5** | 5/5 | 5/5 | 5/5 |
| es-dup | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | – | – |
| es-natural | 5/5 | 5/5 | 5/5 | **4/5** | 5/5 | 5/5 | 5/5 |
| es-ambiguous | **3/5** | 5/5 | 5/5 | 5/5 | **3/5** | – | – |
| es-dry-only | **1/5** | 5/5 | 5/5 | **1/5** | 5/5 | – | – |
| la-profile | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | – | – |
| la-voice-trap | 5/5 | 5/5 | 5/5 | 5/5 | 5/5 | – | – |
| yanki-go | **4/5** | 5/5 | – | 5/5 | 5/5 | – | – |
| yanki-subdeck | 5/5 | 5/5 | – | 5/5 | 5/5 | – | – |
| tech-no-deck | **2/5** | 5/5 | – | **1/5** | 5/5 | – | – |
| yanki-dup | 5/5 | 5/5 | – | 5/5 | 5/5 | – | – |
| anki-basic | 5/5 | 5/5 | – | – | – | – | – |
| anki-batch | 5/5 | 5/5 | – | – | – | – | – |
| vocab-via-flashcards | 5/5 | 5/5 | – | **2/5** | 5/5 | 5/5 | 5/5 |

| skill | model | pass | mean cost | mean turns | mean time |
|---|---|---|---|---|---|
| language-cards | Haiku | 29/35 | $0.051 | 7.6 | 20.9 s |
| language-cards | Sonnet | 35/35 | $0.075 | 5.7 | 13.1 s |
| language-cards | Opus | 35/35 | $0.118 | 4.7 | 19.2 s |
| flashcards | Haiku | 31/35 | $0.054 | 9.3 | 24.3 s |
| flashcards | Sonnet | 35/35 | $0.067 | 7.8 | 14.3 s |

"After fixes" is a re-run of only the cases the fixes touch (5 repeats, 120 runs, $7.15, Claude Code 2.1.286), on P1 `ef0be23`, P2 `4b51524`, finding 3 `37b82c0`, finding 4 `7ee8c05` and the eval change `ac8f559` (checks accept a `note_type` taken from the deck profile). "–" = not re-run.

| skill (re-run cases) | model | before | after | mean cost after |
|---|---|---|---|---|
| language-cards (7) | Haiku | 29/35 | 27/35 | $0.046 |
| language-cards (7) | Sonnet | 35/35 | 33/35 | $0.076 |
| flashcards (yanki ×4 + vocab) | Haiku | 21/25 | 18/25 | $0.050 |
| flashcards (yanki ×4 + vocab) | Sonnet | 25/25 | 25/25 | $0.066 |

"After P3" re-runs the three cases that failed on the profile tag, on P3 `dbb29e2` (omitted tags come from the deck profile), 30 runs, $2.07. Both models now leave `tags` out in 86/90 notes and get `Spanish::Added` from the server.

### After fixes: what changed

- **Fixed:** yanki-go 4/5 → 5/5 (no Write attempt in any run, finding 3). No `add_notes` into a Yanki deck in any run (finding 4). No unknown-key error was hit, so P1 wasn't exercised (`text_field` was 1/70 before).
- **New regression, caused by P2:** Haiku left out `note_type` in 44/120 runs and got it from the profile, as intended. But in 7 of those runs it also left out `tags`, and the server doesn't fill tags from the profile, so the cards lack `Spanish::Added` (es-batch 3, es-natural 1, vocab-via-flashcards 3). Once `note_type` became optional, Haiku treated the whole profile as automatic. **Fixed by P3** (`dbb29e2`): the server now fills omitted tags from the profile too; the three cases are 5/5 for both models.
- **Moved, not fixed:** tech-no-deck 2/5 → 1/5. Haiku no longer writes the Kafka card into Anki `DDIA`, but in 4/5 runs it creates it in the `03 resources/Anki/DDIA/` vault folder instead. The routing error is the same; only the target changed. Choosing a deck is a judgment the server can't make.
- **Unchanged:** Haiku es-dry-only 1/5. Nothing targeted it; "just show me" is still answered from the profile without a dry run (nothing is written).
- **Noise or new?** Sonnet es-ambiguous 5/5 → 3/5: 2 runs added `la cometa` as "kite; comet" without asking. No fix touches gender or meaning, so this looks like run-to-run variance at n=5.

Costs are the `total_cost_usd` Claude Code reports. On a subscription plan they measure usage, not a bill. All 175 runs came to $12.75.

Other numbers:
- **Skills triggered** without a slash command in 40/40 natural-language runs for both Haiku and Sonnet.
- **Tool search:** Haiku made 201 `ToolSearch` calls against Sonnet's 72. MCP tools are deferred, and Haiku often loads them one at a time, which accounts for most of its extra turns.
- **Fake gaps:** no run hit an action or search term FakeAnki doesn't model.

### Where Haiku failed

- **es-ambiguous, 2/5: guessed a gender.** It added `la cometa` (kite) straight away: `{"Word": "la cometa", "Meaning": "kite", "WordType": "N", "Gender": "f"}` → "Done! Added **la cometa** (kite)". The skill says "if a word is ambiguous (several meanings or genders), ask; don't guess". In the other 3 runs it asked, as Sonnet and Opus always did.
- **es-dry-only, 4/5: previewed without a dry run.** Asked "just show me what you would add", it called only `get_deck_profile` and `list_decks` and wrote a table from its own knowledge. It never wrote anything, so the "nothing written" check passed 5/5. But duplicates and field names went unchecked, and those are what the dry run is for.
- **tech-no-deck, 3/5.**
  - Twice it added the Kafka card to Anki's `DDIA` deck with `add_notes` (note 1012), with no Obsidian call. `DDIA` is `mixed`, and the skill's table says technical topics in a `mixed` deck go to Obsidian. The server accepted it, because `mixed` decks accept `add_notes` by design (`Languages::Latin` is `mixed` too). See finding 4.
  - Once it did everything right, but through two `Agent` subagents, and skipped the vault search.
- **yanki-go, 1/5: tried `Write`.** It called `Write` on `<cwd>/03 resources/Anki/Go/select-with-no-cases-blocks-forever.md` (denied), then asked for Write permission. It also checked for duplicates with Anki's `search_notes` instead of `obsidian_search_vault`. See finding 3.

Sonnet and Opus failed no checks. Opus used the fewest turns; Sonnet was fastest.

### Verdict on `model: haiku` (+ `effort: low`)

- **Mechanical cases: Haiku is as good.** Batching, profile fields, gender codes, audio, duplicates and the Latin voice trap were 5/5, the same as Sonnet and Opus.
- **Judgment cases: Haiku is worse.** It lost points on ambiguity and on the preview-means-dry-run step (4/10 on those two cases). That fits LEARNINGS' rule of thumb: mechanical work → Haiku, judgment → a stronger model.
- **Haiku saves less than expected.** On language-cards it costs 0.68× what Sonnet costs ($0.051 vs $0.075) and takes 1.6× as long, because it takes more turns. Opus costs 2.3× Haiku.

The data weakly supports `model: haiku` for plain vocab entry. It doesn't support it once the words are ambiguous or you ask for a preview. Sonnet was 35/35 for about 1.5× Haiku's cost. Whether to change it is the maintainer's call; the skill is unchanged.

After the fixes the picture holds, only sharper: Sonnet 58/60 on the re-run cases, Haiku 45/60 (52/60 counting the P3 re-run). Haiku's new failures (dropped profile tags, the Kafka card routed to DDIA) are again judgment about what's implied, not mechanics.

## Findings

1. **`audio.text_field` is silently ignored.**
   - Seen in the first single run and in 1 of 70 Haiku baseline runs; never in Sonnet or Opus.
   - The model copies `text_field` from the deck profile's `audio` block into `add_notes`' `AudioSpec`, which only has `field`, `voice` and `text`. Pydantic's default `extra="ignore"` drops it without a word.
   - It did no harm here because `text_field` was the first field anyway. With `text_field: "Learnable"` on a note type whose first field is something else, the audio would speak the wrong field and nothing would say so.
   - `NoteInput` has the same hole: a typo like `notetype` would silently fall back to `Basic`.
2. **A dry run without `note_type` defaults to `Basic`.**
   - Seen in the first single run only (0 of 175 suite runs). Haiku's first dry run for the Spanish deck left out `note_type`, so validation ran against `Basic` and failed on the Spanish fields.
   - It recovered: `describe_deck`, then a second dry run. That cost two extra calls.
   - The server already knows the deck's profile says `Spanish`, but doesn't use it.
3. **The flashcards skill never names the Obsidian create tool.**
   - Section 2 describes the vault folder and the file format, but not `obsidian_create_note` or the vault id. Section 1 of the skill says to check the deck with `list_decks`, and nothing says how to check the vault.
   - In a real session with Write allowed, the yanki-go failure would have written the card into the current directory, outside the vault.
4. **A `mixed` parent deck made up only of Yanki subdecks accepts technical cards.**
   - In FakeAnki, `DDIA` has 0 own cards, and its only subdeck, `04_Transactions`, is Yanki. The real `DDIA` has the same shape (read-only `list_decks`, 2026-09-30): 0 own cards, `mixed`, and all 7 subdecks `yanki`.
   - `_source` labels it `mixed` ("or only subdecks do"), and `add_notes` accepts it. A card added there lives outside the vault and outside any chapter subdeck.
5. **The check helpers once crashed** on runs with no create call (IndexError). They now return False. `--rescore` re-applied the fixed checks to the stored streams: same pass counts, no re-run.

## Proposed fixes (applied after the baseline; results under "After fixes")

1. **Finding 1: `AudioSpec` (and `NoteInput`) reject unknown keys, with a message that says what to do.**
   - Set `model_config = ConfigDict(extra="forbid")`, plus a `model_validator(mode="before")` that names the fix, e.g. `Unknown audio key 'text_field'. AudioSpec takes field, voice, text: put the text to speak in audio.text (plain text of the profile's text_field).`
   - Test: an `add_notes` call with `audio.text_field` fails validation with that message.
   - Re-run: es-batch, es-natural and vocab-via-flashcards.
2. **Finding 2: take `note_type` from the profile when it's left out.**
   - `NoteInput.note_type` defaults to `None`. The server resolves it as: the deck profile's `note_type`, else `Basic`. Each result says which one it used (`"note_type": "Spanish (from profile)"`).
   - When the caller sets a `note_type` that differs from the profile's, the note is still validated as given, and the result carries a `warning` ("profile for Languages::Spanish uses 'Spanish'").
   - Update the `note_type` Field description to say this.
   - Tests: omitted → profile type; mismatch → warning; no profile → Basic.
   - Re-run: all language-cards cases.
3. **For review, not requested:**
   - Finding 3: name `obsidian_create_note(vault="personal", path=…)` and `obsidian_search_vault` in the flashcards skill.
   - Finding 4: label a deck `yanki` when it has 0 own cards and all its subdecks are Yanki.
   - es-dry-only: make "a preview is a dry run" explicit in language-cards step 2.

## Limits

- **5 runs per cell.** 3/5 against 5/5 is a signal, not proof.
- **Single-turn `-p`.** Prompts say "no need to confirm" where a write is the thing being tested. Real sessions confirm the table first, which these runs don't exercise.
- **Small fake collection.** FakeAnki holds 12 notes. Real-collection behavior (14k notes, emoji deck names) is covered only by the smoke test.
- **Some checks read the reply text**, e.g. "mentions Yanki sync" or "asks about kite/comet". Those are regexes, not a judge model.
