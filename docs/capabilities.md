# What you can ask for

A plain-language map of what this server lets an agent do with your Anki collection, what it can't do yet, and what the design vocabulary means. **Whoever adds a capability updates this file in the same change.** Tool details are in the README table; this page is for "what's possible?".

Last updated: 2026-11-23 (English Vocab note type created, English deck converted).

## Things you can say

| You say | What happens | Tools |
|---|---|---|
| "Add these Spanish words/sentences" (or paste a screenshot) | Cards with audio go into the right deck, following that deck's saved conventions; near-duplicates are flagged | `add_notes`, `get_deck_profile`, `find_similar_notes` |
| "Do I already have something like X?" | Search by meaning ("consensus", "cat" finds *el gato*) or by exact Anki search | `find_similar_notes`, `search_notes` |
| "What am I forgetting most?" | Your most-lapsed cards, optionally per deck | `get_weak_cards` |
| "Fix / reword these cards", "add a tag" | Edits fields and tags, shows an old → new diff first | `update_notes` |
| "Voice the cards that have no audio" | Backfills pronunciation on existing notes | `add_audio` |
| "Make a new note type: these fields, these card types, this look" | Creates it from a design (see `examples/note_types/english-vocab.json`); needs a full sync afterwards | `create_note_type` |
| "Stop showing me new cards in deck X" / "pause deck X" | Suspends the deck's never-studied cards (`is:new`) or all its cards; undo restores them, gradually if you like | `suspend_cards` |
| "Make a new deck called …" | Creates an empty deck (and parents), warns about similar names | `create_deck` |
| "Move these cards to deck Y" | Moves cards by search, keeps history; dry run first | `move_cards` |
| "Turn these Basic notes into note type Z" | Changes the note type with a field map; **forces a one-way full sync**; backs up first | `convert_notes` |
| "Back up / share deck X" | Writes an `.apkg` (with or without your review history) | `export_deck` |
| "Delete these notes" | Only for ids you approved, dry run first | `delete_notes` |
| "Sync" | Normal sync with AnkiWeb (never a full one) | `sync` |
| "What does deck X look like?" | Note types, fields, audio, tag patterns, samples | `describe_deck`, `list_note_types` |
| "Remember how this deck works" | Saves conventions (voice, field rules, tags) for next time | `set_deck_profile` |

Safety defaults: writes are dry runs first, everything added is tagged `mcp-added` (edited: `mcp-edited`, converted: `mcp-converted`), decks synced from Obsidian (Yanki) are never written to.

## Worked example: the English deck

`Languages::English` was rebuilt as `English Vocab` (Word, Definition, Example, Gap, IPA, Image, Audio). Two cards per note: **Recognition** (word, IPA and audio -> meaning) and **Recall** (definition + gap sentence -> type the word). Image is empty; to add it later, edit the notes.

## Not possible yet

- **Editing an existing note type** (add a field, change a template or its styling). New types can be created (`create_note_type`); changing one that has notes is a riskier schema change and isn't wrapped. `create_deck` only makes the empty container; what its cards contain comes from the note type.
- **Deck options**: new cards per day, review limits, learning steps. Anki's deck-options screen only; AnkiConnect can't change them.
- **Image cards and occlusion**: adding images to notes isn't wired up (audio is).
- **Renaming or deleting decks**, merging decks.
- **Full syncs**: deliberately refused; you do those by hand in Anki.
- **Working when Anki is closed**: everything goes through the Anki desktop app (AnkiConnect).

## Designing a deck: the vocabulary

Pick these when asking for a new deck. Each one maps to note type fields and card templates.

**What a note holds** (fields): word, translation, definition, example sentence, pronunciation (IPA), synonyms, image, notes/mnemonic, audio.

**Card types** (what a review asks you):
- *Recognition*: see the word, recall the meaning.
- *Recall / production*: see the meaning, recall the word.
- *Typing*: same, but you type the answer and Anki marks it right or wrong.
- *Cloze*: a sentence with a gap to fill.
- *Listening*: hear the audio, recall the meaning or type what you heard.
- *Reversed*: both directions from one note (two cards).

**Audio**: always generated when adding, optional, or none. If cards depend on it (listening), adding must not skip it.

**Look**: dark or light, minimal or colourful, font size, how much shows on the front, and where the audio button sits. This is styling on the note type, shared by every card of that type.

## Where things live

- Saved deck conventions: `~/.config/anki-mcp/profiles.json`
- Backups before conversions: `~/.config/anki-mcp/backups/` (`ANKI_MCP_BACKUP_DIR`), log in `convert_history.jsonl`
- Semantic index: Redis container `anki-redis` (needs `colima start` after a reboot), rebuilt with `uv run --extra rag anki-mcp-index`
- Observability: Grafana container `otel-lgtm` (http://localhost:3000)
- Design notes and gotchas: `LEARNINGS.md`; plan: `ROADMAP.md`
