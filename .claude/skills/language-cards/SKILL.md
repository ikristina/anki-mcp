---
name: language-cards
description: Add new foreign-language words or expressions to the user's Anki language decks, with pronunciation audio. Use when the user wants to add, save or learn vocabulary, a phrase or an expression in Spanish, French, Latin, Norwegian or another language, or pastes a list of words to turn into cards. Invocable as /language-cards <words...>.
model: haiku
effort: low
allowed-tools: mcp__anki__list_decks, mcp__anki__search_notes, mcp__anki__add_notes
---

# Language cards with audio

Arguments (if invoked as a command): `$ARGUMENTS`. These are the words or expressions to add, and may name the language.

## 1. Deck conventions (from the user's collection; do not change them)

| Language | Deck | Note type | Audio field | Voice |
|---|---|---|---|---|
| Spanish | `Languages::🇪🇸 Spanish` | `Spanish` | `Audio` | `es-MX` |
| French | `Languages::🇫🇷 French` | `French` | `Sound` | `fr` |
| Latin | `Languages::Latin` | `Basic (and reversed card)` (Front = Latin, Back = English) | `Front` (no separate audio field; the sound tag goes next to the text) | `espeak:la` (user's choice; never Google `la`, which sounds like English) |

Latin: tag `latin`, plus `duolingo` if the sentence comes from Duolingo (as existing notes do). Set `audio.text` to the Latin
text itself, so the speech is never generated from HTML.

Spanish and French note fields:
- `Word`: the target-language word *with article for nouns* (`la cumbre`, `le copain`). Expressions go in as-is.
- `Meaning`: short English meaning.
- `WordType`: one of `N`, `V`, `Adj`, `Adv`, `Expr`, `Prep`, `Pron`, `Conj`, `Num` (French also uses `NPl`).
- `Gender`: `m`, `f` or `m/f` for nouns; empty otherwise.
- `SynonymAid` (optional): a synonym that helps disambiguate.
- Leave `Word_suffix`, `Word_hint`, `Meaning_suffix`, `Meaning_hint` empty.
- Tags: existing notes use `Spanish::Duolingo::<nn>_<Topic>` / `French::Duolingo::...`. Reuse a matching topic tag
  if the user names one; otherwise tag `Spanish::Added` / `French::Added`. `mcp-added` is added automatically.

Another language or deck: run `mcp__anki__list_decks` and `mcp__anki__search_notes` on the deck to learn its note type
and fields, then confirm the mapping with the user before adding.

## 2. Workflow

1. Normalize each entry: fix spelling and accents, add the article to nouns, and fill Meaning/WordType/Gender. If a word is
   ambiguous (several meanings or genders), ask; don't guess.
2. Call `mcp__anki__add_notes` **once** for the batch with `dry_run: true`. Duplicates show up as errors, so drop
   them and tell the user which words they already have.
3. Show the user a compact table of what will be added (Word · Meaning · Type · Gender).
4. Call `add_notes` again without dry_run, with `audio: {"field": <audio field>, "voice": <voice>}` on each note.
   Audio is spoken from `Word`; set `audio.text` only if the spoken text should differ (e.g. drop the parenthetical `(fam.)`).
5. Report which notes were added and any that failed. An audio failure means the note was *not* added, so offer to retry.

## 3. Notes

- Audio is free: Google Translate voices via gTTS, the same service the user's HyperTTS presets use. It needs internet.
- Norwegian voice code is `no` (not `nb`).
- Never add to Yanki-owned decks (see the `flashcards` skill).
