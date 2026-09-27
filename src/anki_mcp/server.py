"""Anki MCP server: lets an agent browse decks, search notes, find weak cards, and add notes.

Design rules (see LEARNINGS.md):
- Results are compact and paginated: a collection can hold tens of thousands of notes.
- Errors say what went wrong AND what to do next, because the reader is a model.
- Writes support dry_run and tag everything they create, so they are easy to audit.
"""

import base64
import html
import re
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from anki_mcp.client import AnkiError, invoke
from anki_mcp.tts import synthesize

mcp = MCPServer(
    "anki",
    instructions="Access to the user's local Anki flashcard collection. Call list_decks first to learn exact deck "
    "names. Search results are previews; use get_notes for full content. When creating several cards, send them "
    "in one add_notes call, and use dry_run=True first if unsure about deck or field names.",
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

PREVIEW_CHARS = 120
ADDED_TAG = "mcp-added"
_TAG_RE = re.compile(r"<[^>]+>")


def _plain(value: str, limit: int | None = PREVIEW_CHARS) -> str:
    """Strip HTML so previews are readable and cheap in tokens."""
    text = html.unescape(_TAG_RE.sub(" ", value.replace("<br>", "\n")))
    text = re.sub(r"[ \t]+", " ", text).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


def _fields(note: dict, limit: int | None) -> dict[str, str]:
    ordered = sorted(note["fields"].items(), key=lambda kv: kv[1]["order"])
    return {name: _plain(f["value"], limit) for name, f in ordered}


def _decks_for_notes(notes: list[dict]) -> dict[int, str]:
    """Map note id -> deck name (via each note's first card)."""
    first_card = {n["noteId"]: n["cards"][0] for n in notes if n.get("cards")}
    if not first_card:
        return {}
    card_to_deck = {c: deck for deck, cards in invoke("getDecks", cards=list(first_card.values())).items() for c in cards}
    return {nid: card_to_deck.get(cid, "?") for nid, cid in first_card.items()}


def _yanki_counts() -> dict[str, int]:
    """Deck -> number of its own cards synced from Obsidian by the Yanki plugin (note types 'Yanki - ...')."""
    cards = invoke("findCards", query='"note:Yanki*"')
    return {deck: len(ids) for deck, ids in invoke("getDecks", cards=cards).items()} if cards else {}


def _source(deck: str, own_cards: int, yanki: dict[str, int]) -> str:
    """'yanki' if every own card comes from Yanki, 'mixed' if some do (or only subdecks do), else 'anki'."""
    n = yanki.get(deck, 0)
    if n and n == own_cards:
        return "yanki"
    if n or any(y.startswith(deck + "::") for y in yanki):
        return "mixed"
    return "anki"


def _own_card_counts(decks: list[str]) -> dict[str, int]:
    names = {str(i): n for n, i in invoke("deckNamesAndIds").items()}
    stats = invoke("getDeckStats", decks=decks)  # keyed by id; its "name" is only the leaf
    return {names[i]: s["total_in_deck"] for i, s in stats.items()}


@mcp.tool(annotations=READ_ONLY)
def list_decks() -> list[dict]:
    """List all Anki decks with card counts (new / learning / due for review / own cards) and source.

    source='yanki': generated from markdown in the user's Obsidian vault by the Yanki plugin. Read and quiz
    freely, but new cards must be written in Obsidian, not added here.
    source='anki': native deck, accepts add_notes.
    source='mixed': holds both kinds (or its subdecks do). add_notes works; prefer Obsidian for technical topics.
    Subdecks use '::' as the separator, e.g. 'DDIA::04_Transactions'. Counts include subdecks except 'own_cards'.
    """
    names = {str(i): n for n, i in invoke("deckNamesAndIds").items()}
    stats = invoke("getDeckStats", decks=list(names.values()))  # keyed by id; its "name" is only the leaf
    yanki = _yanki_counts()
    return sorted(
        (
            {
                "deck": names[deck_id],
                "source": _source(names[deck_id], s["total_in_deck"], yanki),
                "new": s["new_count"],
                "learning": s["learn_count"],
                "due": s["review_count"],
                "own_cards": s["total_in_deck"],
            }
            for deck_id, s in stats.items()
        ),
        key=lambda d: d["deck"],
    )


@mcp.tool(annotations=READ_ONLY)
def search_notes(
    query: Annotated[
        str,
        Field(
            description="Anki search syntax. Examples: 'deck:Go', '\"deck:DDIA::04_Transactions\" isolation', "
            "'tag:leetcode', 'front:*heap*', 'added:7' (last 7 days), 'is:due'. Quote deck names containing spaces."
        ),
    ],
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    offset: Annotated[int, Field(ge=0)] = 0,
) -> dict:
    """Search notes and return compact previews (HTML stripped, fields truncated).

    Returns total match count so you can paginate with offset. Use get_notes for full field content.
    """
    ids = invoke("findNotes", query=query)
    page = ids[offset : offset + limit]
    notes = invoke("notesInfo", notes=page) if page else []
    decks = _decks_for_notes(notes)
    return {
        "total": len(ids),
        "offset": offset,
        "returned": len(notes),
        "notes": [
            {
                "note_id": n["noteId"],
                "deck": decks.get(n["noteId"], "?"),
                "note_type": n["modelName"],
                "tags": n["tags"],
                "fields": _fields(n, PREVIEW_CHARS),
            }
            for n in notes
        ],
    }


@mcp.tool(annotations=READ_ONLY)
def get_notes(note_ids: Annotated[list[int], Field(min_length=1, max_length=50)]) -> list[dict]:
    """Get the full content of specific notes (HTML stripped, not truncated). Get ids from search_notes."""
    notes = [n for n in invoke("notesInfo", notes=note_ids) if n.get("noteId")]
    decks = _decks_for_notes(notes)
    return [
        {
            "note_id": n["noteId"],
            "deck": decks.get(n["noteId"], "?"),
            "note_type": n["modelName"],
            "tags": n["tags"],
            "fields": _fields(n, None),
        }
        for n in notes
    ]


@mcp.tool(annotations=READ_ONLY)
def get_weak_cards(
    deck: Annotated[str | None, Field(description="Exact deck name (includes subdecks). Omit for all decks.")] = None,
    limit: Annotated[int, Field(ge=1, le=50)] = 15,
) -> list[dict]:
    """Find the cards the user struggles with most: highest lapse count (times forgotten), then lowest ease.

    Good for 'quiz me on my weak spots' or deciding which topics need rewritten/extra cards.
    Suspended cards are excluded.
    """
    query = "prop:lapses>0 -is:suspended"
    if deck:
        query = f'"deck:{deck}" {query}'
    card_ids = invoke("findCards", query=query)[:3000]
    if not card_ids:
        return []
    cards = invoke("cardsInfo", cards=card_ids)
    cards.sort(key=lambda c: (-c["lapses"], c["factor"]))
    return [
        {
            "card_id": c["cardId"],
            "note_id": c["note"],
            "deck": c["deckName"],
            "lapses": c["lapses"],
            "ease": c["factor"] / 1000,
            "interval_days": c["interval"],
            "fields": _fields(c, PREVIEW_CHARS),
        }
        for c in cards[:limit]
    ]


class NoteInput(BaseModel):
    deck: str = Field(description="Exact existing deck name, e.g. 'Go' or 'DDIA::04_Transactions'.")
    fields: dict[str, str] = Field(
        description="Field name -> content. For note_type 'Basic' use {'Front': ..., 'Back': ...}; "
        "for 'Cloze' use {'Text': 'The {{c1::answer}} ...', 'Back Extra': ...}. "
        "Plain text newlines become line breaks; HTML is allowed."
    )
    note_type: str = Field(default="Basic", description="Anki note type (model) name.")
    tags: list[str] = Field(default_factory=list, description="Tags without spaces, e.g. ['leetcode', 'heap'].")
    audio: "AudioSpec | None" = Field(default=None, description="Generate pronunciation audio (free Google Translate voice).")


class AudioSpec(BaseModel):
    field: str = Field(description="Field that receives the [sound:...] tag, e.g. 'Audio' (Spanish) or 'Sound' (French).")
    voice: str = Field(
        description="Google voice as language[-REGION] ('es-MX', 'fr', 'de', 'pt-BR', 'no'), "
        "or an offline engine: 'espeak:<lang>' (e.g. 'espeak:la' for Latin) or 'macos:<Voice>' (e.g. 'macos:Alice')."
    )
    text: str | None = Field(default=None, description="Text to speak. Default: the note type's first field (plain text).")


NoteInput.model_rebuild()


def _to_html(value: str) -> str:
    if "\n" in value and not _TAG_RE.search(value):
        return value.replace("\n", "<br>")
    return value


def _attach_audio(addable, notes, field_cache, results):
    """Synthesize audio for notes that asked for it; AnkiConnect stores the file and fills the field.

    A note whose audio fails is reported as an error and not added, so no card is silently missing sound.
    """
    kept = []
    for i, payload in addable:
        spec = notes[i].audio
        if spec:
            text = spec.text or _plain(notes[i].fields[field_cache[notes[i].note_type][0]], None)
            try:
                filename, mp3 = synthesize(text, spec.voice)
            except AnkiError as e:
                results[i] = {"index": i, "status": "error", "error": str(e)}
                continue
            payload["audio"] = [{"data": base64.b64encode(mp3).decode(), "filename": filename, "fields": [spec.field]}]
        kept.append((i, payload))
    return kept


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=False, open_world_hint=True))
def add_notes(
    notes: Annotated[list[NoteInput], Field(min_length=1, max_length=50)],
    dry_run: Annotated[bool, Field(description="Validate only; add nothing. Use to preview a batch.")] = False,
    allow_duplicates: bool = False,
) -> dict:
    """Add one or more notes. Every note gets tagged 'mcp-added' so the user can review them in Anki.

    Only for decks with source='anki' (see list_decks); Yanki decks are rejected.
    Each note is validated first (deck exists, note type exists, field names match, not a duplicate
    of an existing first field in the same deck). Invalid notes are reported and skipped;
    valid ones are still added. Prefer one batched call over many single-note calls.
    Set `audio` on a note to generate pronunciation (needs internet; skipped in dry_run).
    """
    decks = set(invoke("deckNames"))
    yanki = _yanki_counts()
    own = _own_card_counts(sorted({n.deck for n in notes} & decks))
    field_cache: dict[str, list[str]] = {}
    results: list[dict] = [{} for _ in notes]
    candidates: list[tuple[int, dict]] = []

    for i, note in enumerate(notes):
        if note.deck not in decks:
            near = [d for d in decks if note.deck.lower() in d.lower() or d.lower() in note.deck.lower()][:5]
            results[i] = {"index": i, "status": "error", "error": f"Deck '{note.deck}' does not exist. Similar: {near or 'none'}. Use list_decks."}
            continue
        if _source(note.deck, own.get(note.deck, 0), yanki) == "yanki":
            results[i] = {
                "index": i,
                "status": "error",
                "error": f"Deck '{note.deck}' is synced from the user's Obsidian vault by Yanki; cards added here "
                "would live outside their notes. Write this card as Yanki markdown in the Obsidian vault instead "
                "(it syncs to Anki), or pick a deck with source='anki'.",
            }
            continue
        if note.note_type not in field_cache:
            try:
                field_cache[note.note_type] = invoke("modelFieldNames", modelName=note.note_type)
            except AnkiError:
                field_cache[note.note_type] = []
        expected = field_cache[note.note_type]
        if not expected:
            results[i] = {"index": i, "status": "error", "error": f"Note type '{note.note_type}' does not exist. Try 'Basic' or 'Cloze'."}
            continue
        unknown = set(note.fields) - set(expected)
        if unknown:
            results[i] = {"index": i, "status": "error", "error": f"Unknown fields {sorted(unknown)} for '{note.note_type}'. Valid fields: {expected}."}
            continue
        if note.audio and note.audio.field not in expected:
            results[i] = {"index": i, "status": "error", "error": f"Audio field '{note.audio.field}' not in '{note.note_type}'. Valid fields: {expected}."}
            continue
        if note.audio and not (note.audio.text or _plain(note.fields.get(expected[0], ""), None)):
            results[i] = {"index": i, "status": "error", "error": f"No text to speak: set audio.text or fill '{expected[0]}'."}
            continue
        candidates.append(
            (
                i,
                {
                    "deckName": note.deck,
                    "modelName": note.note_type,
                    "fields": {k: _to_html(v) for k, v in note.fields.items()},
                    "tags": sorted(set(note.tags) | {ADDED_TAG}),
                    "options": {"allowDuplicate": allow_duplicates, "duplicateScope": "deck"},
                },
            )
        )

    if candidates:
        checks = invoke("canAddNotesWithErrorDetail", notes=[p for _, p in candidates])
        addable = []
        for (i, payload), check in zip(candidates, checks):
            if check["canAdd"]:
                addable.append((i, payload))
            else:
                results[i] = {"index": i, "status": "error", "error": check.get("error", "cannot add")}

        if dry_run:
            for i, _ in addable:
                results[i] = {"index": i, "status": "valid"}
        elif addable := _attach_audio(addable, notes, field_cache, results):
            ids = invoke("addNotes", notes=[p for _, p in addable])
            for (i, _), nid in zip(addable, ids):
                results[i] = {"index": i, "status": "added", "note_id": nid} if nid else {"index": i, "status": "error", "error": "Anki rejected the note."}

    summary = {s: sum(r["status"] == s for r in results) for s in ("added", "valid", "error")}
    return {"dry_run": dry_run, "summary": summary, "results": results}


def main():
    mcp.run()


if __name__ == "__main__":
    main()
