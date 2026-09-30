---
name: flashcards
description: Create flashcards in the right place. Use whenever the user asks to make, add or save flashcards/Anki cards, or to turn notes, a solution, or a conversation into cards. Routes technical/interview-prep cards to the Obsidian vault (synced to Anki by Yanki) and everything else directly to Anki via the anki MCP server.
---

# Flashcards: pick the destination, then write

The user has two card pipelines. Pick one per card before writing anything.
Foreign-language vocabulary/expressions → follow the `language-cards` skill instead (it handles audio).

## 1. Route

Call `mcp__anki__list_decks` and look at the target deck's `source`:

| source | Where the card goes |
|---|---|
| `yanki` | Obsidian vault as markdown (section 2). `add_notes` will reject it. |
| `anki` | `mcp__anki__add_notes` (section 3) |
| `mixed` | Technical topic → Obsidian. Anything else (language vocab etc.) → `add_notes` |

No suitable deck yet?
- Technical / interview prep (system design, distributed systems, Go, Python, k8s, LeetCode patterns,
  company interview prep) → new folder in the vault (section 2) — and tell the user they must add that folder
  to Yanki's synced folders (Yanki settings), or it will never reach Anki.
- Anything else → ask the user which existing deck, don't invent one.

## 2. Obsidian (Yanki) cards

Write through the `obsidian` MCP server, vault id `personal` (`mcp__obsidian__obsidian_list_vaults` lists them). Never
use Write/Edit for cards: the vault is not the working directory, so a file written there never reaches Yanki.

1. `mcp__obsidian__obsidian_search_vault` (vault `personal`, `scope: "03 resources/Anki/<Deck>"`) to check that a card on the
   same question doesn't already exist. If one does, show it and stop.
2. `mcp__obsidian__obsidian_create_note` with `vault: "personal"`, `path: "03 resources/Anki/<Deck>/<file>.md"`
   (vault-relative, forward slashes) and the card as `content`. It never overwrites an existing file.

- Vault folder: `03 resources/Anki/<Deck>/` — the folder path under `03 resources/Anki/` becomes the deck name
  (`DDIA/04_Transactions` → `DDIA::04_Transactions`). Synced folders live in `.obsidian/plugins/yanki/data.json`.
- **One card per file.** Filename: kebab-case summary of the question, ≤ 60 chars, `.md`.
- Format — front, a `---` line, back. Do NOT write a `noteId` frontmatter; Yanki adds it on sync.

  ```markdown
  **How do you implement a semaphore in Go to limit concurrency to N goroutines?**

  ---

  Use a buffered channel of size N ...

  Tags: go/concurrency/patterns
  ```
- Don't edit the folder note (`<Deck>.md`); its Waypoint block is auto-generated.
- Auto-sync is off: finish by telling the user to run **Yanki: Sync** in Obsidian.

## 3. Direct Anki cards

- One `add_notes` call for the whole batch; run with `dry_run: true` first when deck or fields are uncertain.
- Default note type `Basic` (`Front`/`Back`). For other note types, check fields via `search_notes` on the deck.

## 4. Card quality (both paths)

- One fact or decision per card; the question must be answerable without seeing the source.
- Technical claims (numbers, defaults, complexity, "X does Y") must be verified — use the
  `verified-flashcards` skill's discipline when it is available.
- Show the user the cards (or file list) you created and where they went.
