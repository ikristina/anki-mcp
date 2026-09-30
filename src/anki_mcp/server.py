"""Anki MCP server: lets an agent browse decks, search notes, find weak cards, and add notes.

Design rules (see LEARNINGS.md):
- Results are compact and paginated: a collection can hold tens of thousands of notes.
- Errors say what went wrong AND what to do next, because the reader is a model.
- Writes support dry_run and tag everything they create, so they are easy to audit.
"""

import base64
import difflib
import html
import re
from collections import Counter
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from anki_mcp import profiles, telemetry
from anki_mcp.client import AnkiError, invoke
from anki_mcp.profiles import DeckProfile
from anki_mcp.telemetry import log, notes_written
from anki_mcp.tts import synthesize, validate_voice

mcp = MCPServer(
    "anki",
    instructions="Access to the user's local Anki flashcard collection. Call list_decks first to learn exact deck "
    "names. Before adding notes to a deck, call get_deck_profile and follow the user's saved conventions. Search "
    "results are previews; use get_notes for full content. When creating several cards, send them in one add_notes "
    "call, and use dry_run=True first if unsure about deck or field names.",
    middleware=[telemetry.ToolMetrics()],
)
READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)

PREVIEW_CHARS = 120
ADDED_TAG = "mcp-added"
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"<(?:br|/?div|/?p|/?li)\b[^>]*>", re.IGNORECASE)


def _plain(value: str, limit: int | None = PREVIEW_CHARS) -> str:
    """Strip HTML so previews are readable and cheap in tokens; also the text sent to TTS.

    Block tags become line breaks; inline tags (<b>, <i>, <span>) vanish without adding spaces,
    so 'Marcum <b>excitamus</b>.' reads 'Marcum excitamus.' rather than 'Marcum excitamus .'.
    """
    text = html.unescape(_TAG_RE.sub("", _BLOCK_TAG_RE.sub("\n", value)))
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\s*\n\s*", "\n", text).strip()
    if limit and len(text) > limit:
        return text[:limit].rstrip() + "…"
    return text


_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)\}\}", re.DOTALL)
_IPA_RE = re.compile(r"\(?\s*/[^/\n]+/\s*\)?")


def _cloze_text(value: str) -> str:
    """The words inside {{c1::...}} deletions, for TTS: hints ('::hint') and IPA ('/ˈvɪr.əl/', '(/skɔːrn/)') dropped.

    '{{c1::curtail}}ed' gives 'curtail'; several deletions are joined with ', ' (repeats spoken once).
    """
    words: list[str] = []
    for body in _CLOZE_RE.findall(value):
        word = _IPA_RE.sub(" ", _plain(body.split("::")[0], None)).strip()
        word = re.sub(r"\s+", " ", word)
        if word and word not in words:
            words.append(word)
    return ", ".join(words)


_ALT_RE = re.compile(r"<span\b[^>]*\b(?:class=\"[^\"]*\balt\b[^\"]*\"|part=\"alt\")[^>]*>.*?</span>", re.IGNORECASE | re.DOTALL)


def _speech_text(value: str, cloze_only: bool) -> str:
    """Text to voice: no [sound:] tags, and no alternative answers (<span class="alt">, used by Memrise-style templates)."""
    value = _ALT_RE.sub("", _SOUND_RE.sub("", value))
    return _cloze_text(value) if cloze_only else _plain(value, None)


def _fields(note: dict, limit: int | None) -> dict[str, str]:
    ordered = sorted(note["fields"].items(), key=lambda kv: kv[1]["order"])
    return {name: _plain(f["value"], limit) for name, f in ordered}


def _raw_fields(note: dict) -> dict[str, str]:
    ordered = sorted(note["fields"].items(), key=lambda kv: kv[1]["order"])
    return {name: f["value"] for name, f in ordered}


def _decks_for_notes(notes: list[dict]) -> dict[int, str]:
    """Map note id -> deck name (via each note's first card)."""
    first_card = {n["noteId"]: n["cards"][0] for n in notes if n.get("cards")}
    if not first_card:
        return {}
    card_to_deck = {c: deck for deck, cards in invoke("getDecks", cards=list(first_card.values())).items() for c in cards}
    return {nid: card_to_deck.get(cid, "?") for nid, cid in first_card.items()}


def _deck_term(deck: str, children: bool = False) -> str:
    """Anki search term for this deck (which includes its subdecks), or with children=True for only its subdecks.

    '_' and '*' are wildcards in Anki search, so the name is escaped.
    """
    escaped = re.sub(r'([\\"*_])', r"\\\1", deck)
    return f'"deck:{escaped}::*"' if children else f'"deck:{escaped}"'


def _similar_decks(deck: str, decks) -> list[str]:
    """Substring matches first ('Golang' -> 'Leetcode::golang'), then typo matches on the last segment ('Spansh' -> Spanish)."""
    low = deck.lower()
    hits = sorted(d for d in decks if low in d.lower() or d.lower() in low)
    leaf = {d.split("::")[-1].lower(): d for d in decks}
    hits += [leaf[m] for m in difflib.get_close_matches(low.split("::")[-1], leaf, n=5, cutoff=0.6)]
    hits += difflib.get_close_matches(deck, decks, n=5, cutoff=0.6)
    return list(dict.fromkeys(hits))[:5]


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
def get_notes(
    note_ids: Annotated[list[int], Field(min_length=1, max_length=50)],
    raw: Annotated[
        bool,
        Field(description="True: field values exactly as stored (HTML, [sound:] tags). Use before update_notes."),
    ] = False,
) -> list[dict]:
    """Get the full content of specific notes (HTML stripped, not truncated). Get ids from search_notes."""
    notes = [n for n in invoke("notesInfo", notes=note_ids) if n.get("noteId")]
    decks = _decks_for_notes(notes)
    return [
        {
            "note_id": n["noteId"],
            "deck": decks.get(n["noteId"], "?"),
            "note_type": n["modelName"],
            "tags": n["tags"],
            "fields": _raw_fields(n) if raw else _fields(n, None),
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
        query = f"{_deck_term(deck)} {query}"
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


_SOUND_RE = re.compile(r"\[sound:([^\]]+)\]")
_CATEGORICAL_MAX = 15  # a field with at most this many distinct short values is treated as a code list


def _audio_source(filename: str) -> str:
    """Guess which tool made an audio file from its name: hypertts-…, google-…, anki-mcp-…"""
    if filename.startswith("anki-mcp-"):
        return "anki-mcp"
    head = re.split(r"[-_]", filename, maxsplit=1)[0]
    return head if head != filename and head.isalpha() else "other"


def _field_stats(values: list[str]) -> dict:
    stats: dict = {"fill_rate": round(sum(bool(v.strip()) for v in values) / len(values), 2)}
    sounds = [s for v in values for s in _SOUND_RE.findall(v)]
    texts = [_plain(_SOUND_RE.sub("", v), None) for v in values]
    texts = [t for t in texts if t]
    if sounds:
        stats["audio"] = {
            "notes_with_audio": sum(bool(_SOUND_RE.search(v)) for v in values),
            "sources": dict(Counter(_audio_source(s) for s in sounds).most_common(3)),
            "shares_field_with_text": bool(texts),
        }
    if texts:
        stats["avg_chars"] = round(sum(map(len, texts)) / len(texts))
        counts = Counter(texts)
        if len(texts) >= 5 and len(counts) <= _CATEGORICAL_MAX and stats["avg_chars"] <= 15:
            stats["values"] = dict(counts.most_common())
        else:
            stats["example"] = texts[-1][:80]
    return stats


@mcp.tool(annotations=READ_ONLY)
def describe_deck(
    deck: Annotated[str, Field(description="Exact deck name from list_decks.")],
    sample_size: Annotated[int, Field(ge=20, le=2000, description="Most recent notes to analyze.")] = 500,
) -> dict:
    """Work out a deck's format from its existing notes, so new notes can match it. Call before adding notes to a
    deck you haven't described yet in this conversation.

    Reports, per note type: fields in order with fill rate, which field holds audio ([sound:...]) and which tool made it,
    the allowed values of short code fields (e.g. WordType, Gender), and which fields each card template shows on the
    front/back. Also: tag patterns, subdecks, source (anki/yanki/mixed) and a few recent sample notes.
    Only notes directly in this deck are analyzed, not subdecks.
    """
    decks = set(invoke("deckNames"))
    if deck not in decks:
        raise AnkiError(f"Deck '{deck}' does not exist. Similar: {_similar_decks(deck, decks) or 'none'}. Use list_decks.")
    subdecks = sorted(d for d in decks if d.startswith(deck + "::"))
    own_ids = invoke("findNotes", query=f"{_deck_term(deck)} -{_deck_term(deck, children=True)}")
    own_cards = _own_card_counts([deck]).get(deck, 0)
    result: dict = {
        "deck": deck,
        "source": _source(deck, own_cards, _yanki_counts()),
        "notes": len(own_ids),
        "subdecks": subdecks[:30],
    }
    if saved := profiles.load().get(deck):
        result["profile"] = saved.model_dump(exclude_defaults=True)
    if not own_ids:
        result["hint"] = "No notes directly in this deck. Describe one of its subdecks instead." if subdecks else "Empty deck. Use list_note_types to pick a note type."
        return result

    analyzed = sorted(own_ids)[-sample_size:]  # note ids are creation timestamps: keep the most recent
    notes = invoke("notesInfo", notes=analyzed)
    result["analyzed_notes"] = len(notes)

    by_type: dict[str, list[dict]] = {}
    for n in notes:
        by_type.setdefault(n["modelName"], []).append(n)
    result["note_types"] = []
    for name, group in sorted(by_type.items(), key=lambda kv: -len(kv[1])):
        order = sorted(group[0]["fields"], key=lambda f: group[0]["fields"][f]["order"])
        templates = invoke("modelFieldsOnTemplates", modelName=name)
        result["note_types"].append(
            {
                "note_type": name,
                "notes": len(group),
                "fields": {f: _field_stats([n["fields"][f]["value"] for n in group]) for f in order},
                "templates": {t: {"front": sides[0], "back": sides[1]} for t, sides in templates.items()},
            }
        )

    tags = Counter(t for n in notes for t in n["tags"])
    patterns = Counter("::".join(t.split("::")[:-1]) + "::*" for t in tags.elements() if "::" in t)
    result["tags"] = {"most_common": dict(tags.most_common(8)), "hierarchies": dict(patterns.most_common(5))}
    result["samples"] = [{"note_type": n["modelName"], "fields": _fields(n, 80), "tags": n["tags"]} for n in notes[-3:]]
    return result


MAX_TEMPLATE_DETAIL = 5


@mcp.tool(annotations=READ_ONLY)
def list_note_types(
    name: Annotated[
        str | None,
        Field(description="Case-insensitive substring of the note type name, e.g. 'memrise'. Omit to list all."),
    ] = None,
) -> list[dict]:
    """List the collection's note types (models) with their fields in order and how many notes use each.

    Use it to pick a note type for add_notes or set_deck_profile when a deck is empty or you need a type the deck
    doesn't use yet, e.g. one just imported with a shared deck. When at most 5 types match, each also shows which
    fields its card templates put on the front/back. To see how a deck actually fills the fields, use describe_deck.
    """
    names = sorted(invoke("modelNames"), key=str.lower)
    if name:
        matches = [n for n in names if name.lower() in n.lower()]
        if not matches:
            close = difflib.get_close_matches(name, names, n=5, cutoff=0.5)
            raise AnkiError(f"No note type matches '{name}'. Similar: {close or 'none'}. Omit name to list all.")
        names = matches
    result = []
    for n in names:
        escaped = re.sub(r'([\\"*_])', r"\\\1", n)
        entry = {"note_type": n, "fields": invoke("modelFieldNames", modelName=n),
                 "notes": len(invoke("findNotes", query=f'"note:{escaped}"'))}
        if len(names) <= MAX_TEMPLATE_DETAIL:
            templates = invoke("modelFieldsOnTemplates", modelName=n)
            entry["templates"] = {t: {"front": sides[0], "back": sides[1]} for t, sides in templates.items()}
        result.append(entry)
    return result


# Keys models tend to invent, with what to use instead. Unknown keys used to be dropped silently (pydantic's default):
# an eval run passed audio.text_field (copied from the deck profile), which does nothing here.
_KEY_HINTS = {
    "text_field": "put the text to speak in 'text' (the plain text of the profile's text_field)",
    "notetype": "use 'note_type'", "model": "use 'note_type'", "modelName": "use 'note_type'",
    "deckName": "use 'deck'", "tag": "use 'tags' (a list)",
}


class _AgentInput(BaseModel):
    """Rejects unknown keys with a fix-it message instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _no_unknown_keys(cls, data):
        if isinstance(data, dict) and (unknown := [k for k in data if k not in cls.model_fields]):
            hints = "; ".join(f"'{k}': {_KEY_HINTS[k]}" for k in unknown if k in _KEY_HINTS)
            raise ValueError(f"Unknown key(s) {unknown} in {cls.__name__}. Allowed: {list(cls.model_fields)}."
                             + (f" {hints}." if hints else ""))
        return data


class NoteInput(_AgentInput):
    deck: str = Field(description="Exact existing deck name, e.g. 'Go' or 'DDIA::04_Transactions'.")
    fields: dict[str, str] = Field(
        description="Field name -> content. For note_type 'Basic' use {'Front': ..., 'Back': ...}; "
        "for 'Cloze' use {'Text': 'The {{c1::answer}} ...', 'Back Extra': ...}. "
        "Plain text newlines become line breaks; HTML is allowed."
    )
    note_type: str = Field(default="Basic", description="Anki note type (model) name.")
    tags: list[str] = Field(default_factory=list, description="Tags without spaces, e.g. ['leetcode', 'heap'].")
    audio: "AudioSpec | None" = Field(default=None, description="Generate pronunciation audio (free Google Translate voice).")


class AudioSpec(_AgentInput):
    field: str = Field(description="Field that receives the [sound:...] tag, e.g. 'Audio' (Spanish) or 'Sound' (French).")
    voice: str = Field(
        description="Google voice as language[-REGION] ('es-MX', 'fr', 'de', 'pt-BR', 'no'), "
        "or an offline engine: 'espeak:<lang>' (e.g. 'espeak:la' for Latin), 'macos:<Voice>' (e.g. 'macos:Alice') "
        "or 'piper:<voice>' (a local neural voice, e.g. 'piper:la_LA-vox-medium'). Use the deck profile's voice."
    )
    text: str | None = Field(
        default=None,
        description="Plain text to speak. Default: the note type's first field. There is no 'text_field' key: when the "
        "deck profile's audio.text_field isn't the first field, pass that field's plain text here.",
    )


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
            text = spec.text or _speech_text(notes[i].fields[field_cache[notes[i].note_type][0]], False)
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
    Unknown keys in a note or its audio (e.g. 'text_field', 'notetype') are rejected, not ignored.
    """
    decks = set(invoke("deckNames"))
    yanki = _yanki_counts()
    own = _own_card_counts(sorted({n.deck for n in notes} & decks))
    field_cache: dict[str, list[str]] = {}
    results: list[dict] = [{} for _ in notes]
    candidates: list[tuple[int, dict]] = []

    for i, note in enumerate(notes):
        if note.deck not in decks:
            near = _similar_decks(note.deck, decks)
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
        if note.audio:
            try:
                validate_voice(note.audio.voice)
            except AnkiError as e:
                results[i] = {"index": i, "status": "error", "error": str(e)}
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
    if added := Counter(notes[i].deck for i, r in enumerate(results) if r["status"] == "added"):
        for deck, n in added.items():
            notes_written.add(n, {"operation": "add", "anki.deck": deck})
        log.info("added %d notes: %s", summary["added"], dict(added))
    return {"dry_run": dry_run, "summary": summary, "results": results}


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=True))
def add_audio(
    query: Annotated[str, Field(description="Anki search for the notes to voice, e.g. '\"deck:Languages::Latin\"'.")],
    text_field: Annotated[str, Field(description="Field whose text is spoken, e.g. 'Front' (Latin) or 'Word' (Spanish/French).")],
    audio_field: Annotated[str, Field(description="Field that receives the [sound:...] tag, e.g. 'Audio' or 'Sound'.")],
    voice: Annotated[
        str,
        Field(
            description="Latin: 'espeak:la' (Google's 'la' is NOT Latin and is rejected). Spanish: 'es-MX'. French: 'fr'. "
            "Other: Google 'de', 'pt-BR', …, or offline 'espeak:<lang>' / 'macos:<Voice>' / 'piper:<voice>'. "
            "Prefer the deck profile's voice."
        ),
    ],
    limit: Annotated[int, Field(ge=1, le=50, description="Max notes to voice in this call; call again for the rest.")] = 20,
    dry_run: Annotated[bool, Field(description="Default True: report what would change. Set False to write.")] = True,
    cloze_only: Annotated[
        bool,
        Field(
            description="Speak only the {{c1::...}} words of text_field, not the whole field. Drops cloze hints and "
            "IPA transcriptions ('/ˈvɪr.əl/', '(/skɔːrn/)'). Notes without a cloze are skipped. For Cloze decks."
        ),
    ] = False,
) -> dict:
    """Add pronunciation audio to EXISTING notes that don't have it yet (use add_notes' `audio` option for new notes).

    Skips notes whose audio_field already holds a [sound:...] tag, so it is safe to re-run until 'remaining' is 0.
    Cloze notes: set cloze_only=True so only the hidden word is spoken (audio_field can be 'Back Extra').
    Only fills one field per note (no schema change, no full sync, review history untouched). Yanki notes are skipped.
    Workflow: dry run → voice ~5 notes and let the user listen in Anki → continue in batches.
    """
    validate_voice(voice)
    ids = invoke("findNotes", query=query)
    notes = invoke("notesInfo", notes=ids) if ids else []
    counts = Counter()
    missing_fields: dict[str, list[str]] = {}
    todo: list[tuple[dict, str]] = []
    for n in notes:
        fields = n["fields"]
        if n["modelName"].startswith("Yanki"):
            counts["skipped_yanki"] += 1
        elif text_field not in fields or audio_field not in fields:
            counts[f"skipped_missing_field ({n['modelName']})"] += 1
            missing_fields.setdefault(n["modelName"], list(fields))
        elif _SOUND_RE.search(fields[audio_field]["value"]):
            counts["already_has_audio"] += 1
        elif not (text := _speech_text(fields[text_field]["value"], cloze_only)):
            counts["skipped_no_cloze" if cloze_only else "skipped_empty_text"] += 1
        else:
            if _SOUND_RE.search(fields[text_field]["value"]):
                counts["warning_text_field_also_has_sound"] += 1
            todo.append((n, text))

    batch = todo[:limit]
    result: dict = {"matched": len(notes), **counts, "eligible": len(todo), "dry_run": dry_run}
    if not notes:
        result["hint"] = "No notes match the query. Check the deck name with list_decks (quote names with spaces)."
        return result
    if missing_fields:
        result["available_fields"] = missing_fields
    if dry_run:
        result["would_voice"] = [{"note_id": n["noteId"], "text": t[:80]} for n, t in batch]
        result["remaining_after"] = len(todo) - len(batch)
        return result

    done, failed = [], []
    for n, text in batch:
        try:
            filename, data = synthesize(text, voice)
            invoke("storeMediaFile", filename=filename, data=base64.b64encode(data).decode())
            current = n["fields"][audio_field]["value"]
            invoke("updateNoteFields", note={"id": n["noteId"], "fields": {audio_field: f"{current}[sound:{filename}]"}})
            done.append({"note_id": n["noteId"], "text": text[:80]})
        except AnkiError as e:
            failed.append({"note_id": n["noteId"], "text": text[:80], "error": str(e)})
            if "not installed" in str(e) or "Unknown TTS" in str(e) or "no real" in str(e):
                break  # the voice itself is unusable; don't repeat the same error for every note
    result.update(voiced=done, failed=failed, remaining=len(todo) - len(done))
    notes_written.add(len(done), {"operation": "audio", "tts.voice": voice})
    log.info("voiced %d notes with %s (%d failed, %d remaining)", len(done), voice, len(failed), result["remaining"])
    result["note"] = "If a note is open in Anki's browser/editor, reopen it to see the change."
    return result


EDITED_TAG = "mcp-edited"
DIFF_CHARS = 300


class NoteUpdate(BaseModel):
    note_id: int = Field(description="Note id from search_notes / get_notes.")
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Field name -> complete new value (raw HTML, as get_notes(raw=True) shows it). Only the fields "
        "listed change; to edit part of a field, copy its raw value and change just that part.",
    )
    add_tags: list[str] = Field(default_factory=list, description="Tags to add (existing tags are kept).")


@mcp.tool(annotations=ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False))
def update_notes(
    updates: Annotated[list[NoteUpdate], Field(min_length=1, max_length=50)],
    dry_run: Annotated[bool, Field(description="Default True: show old -> new per field. Set False to write.")] = True,
) -> dict:
    """Edit fields of EXISTING notes and/or add tags, e.g. tidy notes the user typed on their phone.

    Read the notes with get_notes(raw=True) first, so formatting and [sound:] tags aren't lost. Refused per note:
    Yanki notes (Obsidian would overwrite them), unknown fields, and edits that would remove a [sound:] tag
    (audio is only added, via add_audio). Changed notes get the tag 'mcp-edited'. Review history is untouched.
    Dry run by default: show the user the old -> new diff, then call again with dry_run=False.
    """
    found = {n["noteId"]: n for n in invoke("notesInfo", notes=[u.note_id for u in updates]) if n.get("noteId")}
    results, apply = [], []
    for u in updates:
        n = found.get(u.note_id)
        if n is None:
            results.append({"note_id": u.note_id, "status": "error", "error": "Note not found. Get ids from search_notes."})
            continue
        if n["modelName"].startswith("Yanki"):
            results.append({"note_id": u.note_id, "status": "error", "error": "Yanki note: edit its markdown in the "
                            "Obsidian vault instead, or the next Yanki sync overwrites the change."})
            continue
        unknown = sorted(set(u.fields) - set(n["fields"]))
        if unknown:
            results.append({"note_id": u.note_id, "status": "error",
                            "error": f"Unknown fields {unknown} for '{n['modelName']}'. Valid fields: {list(n['fields'])}."})
            continue
        new = {f: _to_html(v) for f, v in u.fields.items() if _to_html(v) != n["fields"][f]["value"]}
        lost = [f for f, v in new.items() if set(_SOUND_RE.findall(n["fields"][f]["value"])) - set(_SOUND_RE.findall(v))]
        if lost:
            results.append({"note_id": u.note_id, "status": "error", "error": f"Would remove audio from {lost}. "
                            "Keep the [sound:...] tag in the new value (read it with get_notes(raw=True))."})
            continue
        tags = [t for t in u.add_tags if t not in n["tags"]]
        if not new and not tags:
            results.append({"note_id": u.note_id, "status": "unchanged"})
            continue
        entry = {"note_id": u.note_id, "status": "would_update" if dry_run else "updated",
                 "changes": {f: {"old": n["fields"][f]["value"][:DIFF_CHARS], "new": v[:DIFF_CHARS]} for f, v in new.items()}}
        if tags:
            entry["add_tags"] = tags
        results.append(entry)
        apply.append((u.note_id, new, tags))

    if not dry_run:
        for nid, new, tags in apply:
            if new:
                invoke("updateNoteFields", note={"id": nid, "fields": new})
            invoke("addTags", notes=[nid], tags=" ".join([*tags, EDITED_TAG]))
        notes_written.add(len(apply), {"operation": "update"})
        log.info("updated %d notes", len(apply))
    summary = Counter(r["status"] for r in results)
    out = {"dry_run": dry_run, "summary": dict(summary), "results": results}
    if not dry_run and apply:
        out["note"] = "If a note is open in Anki's browser/editor, reopen it to see the change."
    return out


_SYNC_STATUS_RE = re.compile(r"Sync status (\d+) not one of")
# SyncCollectionResponse.ChangesRequired in Anki's sync.proto; AnkiConnect refuses everything but 0 and 1 (normal sync).
_FULL_SYNC_REASONS = {
    2: "the collection changed on both sides in a way that can't be merged, usually a note type or field edit",
    3: "this computer's collection is empty, so it would download everything from AnkiWeb",
    4: "AnkiWeb is empty, so this computer would upload everything",
}


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=True))
def sync() -> dict:
    """Sync the desktop collection with AnkiWeb, like pressing Sync in Anki.

    Call it before looking for notes the user added on another device (phone, iPad): the desktop doesn't see them
    until it syncs. Call it again after writing, so the other devices get the changes. Media (audio files) transfers
    in the background and can take a minute after this returns; the user should then sync their phone.

    Only normal (merging) syncs run. If AnkiWeb needs a full sync, which overwrites one side, nothing is changed and
    the error explains why: only the user can choose which side to keep, in desktop Anki.
    """
    try:
        invoke("sync", timeout=120)
    except AnkiError as e:
        msg = str(e)
        if "auth not configured" in msg:
            raise AnkiError("Desktop Anki is not logged in to AnkiWeb. Ask the user to click Sync in desktop Anki and log in once.") from e
        if m := _SYNC_STATUS_RE.search(msg):
            reason = _FULL_SYNC_REASONS.get(int(m.group(1)), f"sync status {m.group(1)}")
            raise AnkiError(
                f"Not synced: AnkiWeb needs a full sync ({reason}). A full sync overwrites one side, so it was not started "
                "and nothing changed. The user must click Sync in desktop Anki and choose which side to keep. Until then, "
                "writes here stay on this computer and won't reach the phone, so tell the user before making any."
            ) from e
        raise
    log.info("synced with AnkiWeb")
    return {"synced": True, "media": "transfers in the background; wait about a minute, then sync the phone"}


@mcp.tool(annotations=READ_ONLY)
def get_deck_profile(
    deck: Annotated[str | None, Field(description="Exact deck name. Omit to list every saved profile.")] = None,
) -> dict:
    """Read the user's saved conventions for a deck: note type, audio field + voice, how to fill each field, tags,
    free-form rules. Call this BEFORE adding notes to a deck, and follow it. A profile is the user's decision;
    describe_deck's inference only fills gaps it doesn't cover.

    No profile yet: call describe_deck, propose a profile to the user, and save it with set_deck_profile once they confirm.
    """
    saved = profiles.load()
    if deck is None:
        return {"file": str(profiles.path()), "profiles": {d: p.model_dump(exclude_defaults=True) for d, p in saved.items()}}
    if deck in saved:
        return {"deck": deck, "profile": saved[deck].model_dump(exclude_defaults=True)}
    return {
        "deck": deck,
        "profile": None,
        "saved_decks": sorted(saved),
        "hint": "No profile saved. Run describe_deck, propose a profile to the user, then set_deck_profile after they confirm.",
    }


@mcp.tool(annotations=ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False))
def set_deck_profile(
    deck: Annotated[str, Field(description="Exact deck name from list_decks.")],
    profile: DeckProfile,
    replace: Annotated[bool, Field(description="True: overwrite the whole profile. False (default): only the keys you pass change.")] = False,
) -> dict:
    """Save conventions for a deck so every future session (and agent) follows them. Only call after the user has
    confirmed the values. Checked against Anki: the deck and note type must exist, audio/field names must belong to
    the note type, and the voice must be usable. Writes a local JSON file, never the Anki collection.
    """
    decks = set(invoke("deckNames"))
    if deck not in decks:
        raise AnkiError(f"Deck '{deck}' does not exist. Similar: {_similar_decks(deck, decks) or 'none'}. Use list_decks.")
    saved = profiles.load()
    old = saved.get(deck)
    merged = profile if replace or old is None else old.model_copy(update=profile.model_dump(exclude_unset=True))
    merged = DeckProfile.model_validate(merged.model_dump())  # re-validate nested dicts from the merge

    if merged.note_type:
        try:
            names = invoke("modelFieldNames", modelName=merged.note_type)
        except AnkiError:
            raise AnkiError(f"Note type '{merged.note_type}' does not exist. Use describe_deck to see the deck's note types.")
        referenced = set(merged.fields)
        if merged.audio:
            referenced |= {merged.audio.field} | ({merged.audio.text_field} if merged.audio.text_field else set())
        if unknown := sorted(referenced - set(names)):
            raise AnkiError(f"Fields {unknown} are not in note type '{merged.note_type}'. Valid fields: {names}.")
    elif merged.audio or merged.fields:
        raise AnkiError("Set note_type too, so field names can be checked.")
    if merged.audio:
        validate_voice(merged.audio.voice)

    saved[deck] = merged
    file = profiles.save(saved)
    return {"deck": deck, "saved": True, "file": str(file), "profile": merged.model_dump(exclude_defaults=True)}


def main():
    telemetry.setup()
    mcp.run()


if __name__ == "__main__":
    main()
