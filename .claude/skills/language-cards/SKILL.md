---
name: language-cards
description: Add new foreign-language words or expressions to the user's Anki language decks, with pronunciation audio. Use when the user wants to add, save or learn vocabulary, a phrase or an expression in Spanish, French, Latin, Norwegian or another language, or pastes a list of words to turn into cards. Invocable as /language-cards <words...>.
model: haiku
effort: low
allowed-tools: mcp__anki__get_deck_profile, mcp__anki__set_deck_profile, mcp__anki__list_decks, mcp__anki__describe_deck, mcp__anki__list_note_types, mcp__anki__search_notes, mcp__anki__add_notes, mcp__anki__add_audio
---

# Language cards with audio

Arguments (if invoked as a command): `$ARGUMENTS`. These are the words or expressions to add, and may name the language.

## 1. Deck conventions come from deck profiles

Don't guess a deck's format. Call `mcp__anki__get_deck_profile` (no argument) to see every saved profile, then use the
one for the target deck: `note_type`, `audio` (field, voice, text_field), how to fill each field in `fields`, `tags`, and
the free-form `conventions`. Follow them exactly. `mcp-added` is tagged automatically.

No profile for the deck (or no deck for the language yet):
1. `mcp__anki__list_decks` to find the deck, then `mcp__anki__describe_deck` to learn its note type, fields, audio field,
   code values (e.g. WordType) and tag patterns.
   Empty deck (describe_deck says "Empty deck.")? Call `mcp__anki__list_note_types` (filter by name if the user named
   one) and ask which note type to use; its template front/back fields show which field to voice.
2. Propose a profile to the user (note type, audio field + voice, one line per field, tags). Ask which voice they want.
   For Latin, only `espeak:la` works: Google's `la` is not Latin.
3. After they confirm, save it with `mcp__anki__set_deck_profile`, then continue.

If the user corrects a convention along the way ("nouns need the article"), offer to save it to the profile.

## 2. Workflow

1. Normalize each entry per the profile's `fields` (e.g. fix spelling and accents, articles on nouns, WordType/Gender). If a word is
   ambiguous (several meanings or genders), ask; don't guess.
2. Call `mcp__anki__add_notes` **once** for the batch with `dry_run: true`. Duplicates show up as errors, so drop
   them and tell the user which words they already have.
3. Show the user a compact table of what will be added (one column per filled field).
4. Call `add_notes` again without dry_run, with `audio: {"field": <profile audio.field>, "voice": <profile audio.voice>}`
   on each note. Set `audio.text` to the plain text of the profile's `audio.text_field` when it isn't the first field,
   or when the spoken text should differ (e.g. drop a parenthetical `(fam.)`).
5. Report which notes were added and any that failed. An audio failure means the note was *not* added, so offer to retry.

## 3. Adding audio to existing notes

Use `mcp__anki__add_audio` (not add_notes) with `text_field`, `audio_field` and `voice` from the deck's profile.
Dry run first (the default), then `dry_run: false` in batches of up to 50 until `remaining` is 0. On a first run for a deck,
voice ~5 notes and let the user listen before doing the rest.

## 4. Notes

- Audio is free: Google Translate voices via gTTS, the same service the user's HyperTTS presets use. It needs internet.
- Norwegian voice code is `no` (not `nb`).
- Never add to Yanki-owned decks (see the `flashcards` skill).
