import pytest

from anki_mcp import server
from anki_mcp.client import AnkiError
from anki_mcp.server import AudioSpec, NoteInput, add_audio, add_notes, describe_deck, get_weak_cards, list_decks, search_notes

SPANISH = {"deck": "Languages::Spanish", "note_type": "Spanish"}
LATIN_Q = '"deck:Languages::Latin"'


# --- pure helpers ------------------------------------------------------------------------------------------------

def test_plain_strips_html_entities_and_truncates():
    assert server._plain("<b>la</b>&nbsp;casa<br>grande") == "la casa\ngrande"
    assert server._plain("x" * 200, 10) == "x" * 10 + "…"


def test_deck_term_escapes_anki_wildcards():
    assert server._deck_term("Languages::_Pimsleur") == r'"deck:Languages::\_Pimsleur"'
    assert server._deck_term("DDIA", children=True) == '"deck:DDIA::*"'


@pytest.mark.parametrize("typo, expected", [("Spansh", "Languages::Spanish"), ("latin", "Languages::Latin"), ("Golang", "Go")])
def test_similar_decks_handles_typos_and_substrings(typo, expected):
    assert expected in server._similar_decks(typo, ["Go", "Languages::Spanish", "Languages::Latin", "Kubernetes"])


def test_source_labels():
    yanki = {"Go": 11, "Languages::Latin": 2}
    assert server._source("Go", 11, yanki) == "yanki"
    assert server._source("Languages::Latin", 464, yanki) == "mixed"  # a few stray Yanki notes don't make it Yanki-owned
    assert server._source("Languages", 0, yanki) == "mixed"  # parent of a Yanki subdeck
    assert server._source("Languages::Spanish", 100, yanki) == "anki"


def test_audio_source_from_filename():
    assert server._audio_source("hypertts-3fc4.mp3") == "hypertts"
    assert server._audio_source("anki-mcp-abc.m4a") == "anki-mcp"
    assert server._audio_source("recording.mp3") == "other"


# --- read tools --------------------------------------------------------------------------------------------------

def test_list_decks_reports_full_names_and_sources(anki):
    decks = {d["deck"]: d for d in list_decks()}
    assert decks["DDIA::04_Transactions"]["source"] == "yanki"  # full path, not getDeckStats' leaf name
    assert decks["DDIA"]["source"] == "mixed"
    assert decks["Languages::Spanish"]["source"] == "anki"
    assert decks["Languages::Spanish"]["own_cards"] == 6


def test_search_notes_paginates_and_escapes(anki):
    page = search_notes('"deck:Languages::Spanish"', limit=2, offset=0)
    assert (page["total"], page["returned"]) == (6, 2)
    assert page["notes"][0]["deck"] == "Languages::Spanish"


def test_get_weak_cards_sorted_by_lapses(anki):
    cards = get_weak_cards(limit=5)
    assert [c["lapses"] for c in cards] == [5, 2]


def test_describe_deck_infers_format(anki):
    d = describe_deck("Languages::Spanish")
    nt = d["note_types"][0]
    assert nt["note_type"] == "Spanish"
    assert nt["fields"]["Audio"]["audio"]["sources"] == {"hypertts": 6}
    assert nt["fields"]["WordType"]["values"] == {"N": 3, "V": 1, "Adj": 1, "Expr": 1}
    assert nt["templates"]["Read"] == {"front": ["Word", "Audio"], "back": ["Meaning"]}
    assert d["tags"]["hierarchies"] == {"Spanish::Duolingo::*": 6}


def test_describe_deck_parent_only_and_typo(anki):
    assert describe_deck("DDIA")["hint"].startswith("No notes directly")
    with pytest.raises(AnkiError, match="Languages::Spanish"):
        describe_deck("Spansh")


def test_describe_deck_handles_underscore_deck(anki):
    assert describe_deck("Languages::_Pimsleur")["notes"] == 1


# --- add_notes ---------------------------------------------------------------------------------------------------

def statuses(result):
    return [r["status"] for r in result["results"]]


def test_add_notes_validation_errors_are_specific(anki):
    r = add_notes([
        NoteInput(deck="Go", fields={"Front": "q", "Back": "a"}),
        NoteInput(deck="Golang", fields={"Front": "q", "Back": "a"}),
        NoteInput(**SPANISH, fields={"Question": "x"}),
        NoteInput(**SPANISH, fields={"Word": "x"}, audio=AudioSpec(field="Sound", voice="es-MX")),
        NoteInput(**SPANISH, fields={"Word": "la cumbre"}),
        NoteInput(deck="Languages::Latin", note_type="Basic (and reversed card)", fields={"Front": "x", "Back": "y"},
                  audio=AudioSpec(field="Audio", voice="la")),
    ], dry_run=True)
    errors = [x["error"] for x in r["results"]]
    assert "Yanki" in errors[0]
    assert "Similar" in errors[1] and "Go" in errors[1]
    assert "Unknown fields ['Question']" in errors[2]
    assert "Audio field 'Sound'" in errors[3]
    assert "duplicate" in errors[4]
    assert "espeak:la" in errors[5]  # Google 'la' is rejected even in a dry run
    assert "addNotes" not in anki.calls


def test_add_notes_dry_run_writes_nothing(anki):
    before = len(anki.notes)
    r = add_notes([NoteInput(**SPANISH, fields={"Word": "el gato", "Meaning": "cat"})], dry_run=True)
    assert statuses(r) == ["valid"] and len(anki.notes) == before


def test_add_notes_adds_with_audio_and_tag(anki):
    r = add_notes([NoteInput(**SPANISH, fields={"Word": "el gato", "Meaning": "cat\nfeline"}, tags=["test"],
                             audio=AudioSpec(field="Audio", voice="es-MX"))])
    assert statuses(r) == ["added"]
    note = anki.notes[r["results"][0]["note_id"]]
    assert note["fields"]["Audio"].startswith("[sound:anki-mcp-")
    assert note["fields"]["Meaning"] == "cat<br>feline"
    assert set(note["tags"]) == {"test", "mcp-added"}


def test_add_notes_audio_failure_skips_note(anki, monkeypatch):
    def boom(text, voice):
        raise AnkiError("network down")
    monkeypatch.setattr(server, "synthesize", boom)
    before = len(anki.notes)
    r = add_notes([NoteInput(**SPANISH, fields={"Word": "el gato"}, audio=AudioSpec(field="Audio", voice="es-MX"))])
    assert statuses(r) == ["error"] and "network down" in r["results"][0]["error"]
    assert len(anki.notes) == before  # never a card silently missing its sound


# --- add_audio ---------------------------------------------------------------------------------------------------

def test_add_audio_dry_run_plans_without_writing(anki, monkeypatch):
    monkeypatch.setattr(server, "validate_voice", lambda v: None)  # CI has no eSpeak binary
    r = add_audio(LATIN_Q, "Front", "Audio", "espeak:la")
    assert (r["matched"], r["already_has_audio"], r["eligible"]) == (3, 1, 2)
    assert [x["text"] for x in r["would_voice"]] == ["Socius", "Marcum excitamus."]  # HTML stripped
    assert "updateNoteFields" not in anki.calls


def test_add_audio_writes_batch_and_is_rerunnable(anki, monkeypatch):
    monkeypatch.setattr(server, "validate_voice", lambda v: None)
    first = add_audio(LATIN_Q, "Front", "Audio", "espeak:la", limit=1, dry_run=False)
    assert (len(first["voiced"]), first["remaining"]) == (1, 1)
    second = add_audio(LATIN_Q, "Front", "Audio", "espeak:la", limit=5, dry_run=False)
    assert (len(second["voiced"]), second["remaining"]) == (1, 0)
    third = add_audio(LATIN_Q, "Front", "Audio", "espeak:la", dry_run=False)
    assert third["eligible"] == 0 and third["already_has_audio"] == 3
    assert len(anki.media) == 2


def test_add_audio_rejects_google_latin_and_explains_fields(anki):
    with pytest.raises(AnkiError, match="espeak:la"):
        add_audio(LATIN_Q, "Front", "Audio", "la")
    r = add_audio(LATIN_Q, "Word", "Audio", "es-MX")
    assert r["eligible"] == 0 and r["available_fields"] == {"Basic (and reversed card)": ["Front", "Back", "Audio"]}


def test_add_audio_skips_yanki_notes(anki):
    r = add_audio('"deck:Go"', "Front", "Back", "es-MX")
    assert r["skipped_yanki"] == 1 and r["eligible"] == 0
