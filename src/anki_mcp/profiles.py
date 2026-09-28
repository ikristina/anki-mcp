"""Deck profiles: per-deck conventions that can't be inferred from the notes (voice, spoken field, style rules).

Stored as one human-editable JSON file, outside the collection, so saving a profile never touches Anki:
`$ANKI_MCP_PROFILES`, else `~/.config/anki-mcp/profiles.json`. Shape: {"<exact deck name>": DeckProfile, ...}.
"""

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from anki_mcp.client import AnkiError


class AudioProfile(BaseModel):
    field: str = Field(description="Field that receives the [sound:...] tag, e.g. 'Audio' or 'Sound'.")
    voice: str = Field(description="Voice string as in add_notes: 'es-MX', 'fr', 'espeak:la', 'macos:Alice', ...")
    text_field: str | None = Field(default=None, description="Field whose text is spoken. Default: the note type's first field.")


class DeckProfile(BaseModel):
    note_type: str | None = Field(default=None, description="Note type to use for new notes in this deck.")
    audio: AudioProfile | None = Field(default=None, description="How new notes in this deck get pronunciation audio.")
    fields: dict[str, str] = Field(
        default_factory=dict,
        description="Field name -> how to fill it, e.g. {'Word': 'target-language word, nouns with article', "
        "'WordType': 'one of N, V, Adj, Adv, Expr'}.",
    )
    tags: list[str] = Field(default_factory=list, description="Tags to put on new notes, e.g. ['latin'].")
    conventions: list[str] = Field(
        default_factory=list, description="Free-form rules, e.g. 'Tag duolingo if the sentence comes from Duolingo.'"
    )


def path() -> Path:
    return Path(os.environ.get("ANKI_MCP_PROFILES") or Path.home() / ".config" / "anki-mcp" / "profiles.json")


def load() -> dict[str, DeckProfile]:
    p = path()
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
        return {deck: DeckProfile.model_validate(v) for deck, v in raw.items()}
    except (json.JSONDecodeError, ValidationError, AttributeError) as e:
        raise AnkiError(f"Profile file {p} is invalid ({e}). Ask the user to fix or remove it.") from e


def save(profiles: dict[str, DeckProfile]) -> Path:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {deck: prof.model_dump(exclude_defaults=True) for deck, prof in sorted(profiles.items())}
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    tmp.replace(p)  # atomic: a crash never leaves a half-written file
    return p
