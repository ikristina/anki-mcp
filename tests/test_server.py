import pytest

from anki_mcp import server
from anki_mcp.client import AnkiError
from anki_mcp.server import (AudioSpec, NoteInput, NoteUpdate, add_audio, add_notes, describe_deck, get_notes, get_weak_cards, list_decks,
                             list_note_types, search_notes, sync, update_notes)

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
    yanki = {"Go": 11, "Languages::Latin": 2, "DDIA::03_Replication": 15, "DDIA::04_Transactions": 15}
    own = {"Go": 11, "Languages": 0, "Languages::Latin": 464, "Languages::Spanish": 100,
           "DDIA": 0, "DDIA::03_Replication": 15, "DDIA::04_Transactions": 15}
    assert server._source("Go", own, yanki) == "yanki"
    assert server._source("Languages::Latin", own, yanki) == "mixed"  # a few stray Yanki notes don't make it Yanki-owned
    assert server._source("Languages", own, yanki) == "mixed"  # parent of a Yanki subdeck, but Latin holds native cards
    assert server._source("Languages::Spanish", own, yanki) == "anki"
    assert server._source("DDIA", own, yanki) == "yanki"  # no own cards, every subdeck from Obsidian
    assert server._source("DDIA", {**own, "DDIA": 1}, yanki) == "mixed"  # a native card of its own: add_notes stays open
    assert server._source("DDIA", {**own, "DDIA::Notes": 3}, yanki) == "mixed"  # a native subdeck


def test_audio_source_from_filename():
    assert server._audio_source("hypertts-3fc4.mp3") == "hypertts"
    assert server._audio_source("anki-mcp-abc.m4a") == "anki-mcp"
    assert server._audio_source("recording.mp3") == "other"


# --- read tools --------------------------------------------------------------------------------------------------

def test_list_decks_reports_full_names_and_sources(anki):
    decks = {d["deck"]: d for d in list_decks()}
    assert decks["DDIA::04_Transactions"]["source"] == "yanki"  # full path, not getDeckStats' leaf name
    assert decks["DDIA"]["source"] == "yanki"  # only Yanki subdecks
    assert decks["Languages::Spanish"]["source"] == "anki"
    assert decks["Languages::Spanish"]["own_cards"] == 6


def test_search_notes_paginates_and_escapes(anki):
    page = search_notes('"deck:Languages::Spanish"', limit=2, offset=0)
    assert (page["total"], page["returned"]) == (6, 2)
    assert page["notes"][0]["deck"] == "Languages::Spanish"


def test_search_notes_tag_and_text_terms(anki):
    # Models write these in search_notes (duplicate checks, "cards I tagged latin").
    assert search_notes("tag:latin")["total"] == 3
    assert search_notes("tag:Spanish::Duolingo")["total"] == 6  # parent tag matches children
    assert search_notes('"deck:Languages::Spanish" cumbre')["total"] == 1
    assert search_notes("-tag:latin CUMBRE")["total"] == 1


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


# --- list_note_types ---------------------------------------------------------------------------------------------

def test_list_note_types_all_are_compact(anki, monkeypatch):
    monkeypatch.setattr(server, "MAX_TEMPLATE_DETAIL", 3)  # the fake has only 5 types; the real collection ~50
    types = {t["note_type"]: t for t in list_note_types()}
    assert set(types) == {"Basic", "Basic (and reversed card)", "Spanish", "Yanki - Basic", "Cloze"}
    assert types["Spanish"] == {"note_type": "Spanish", "fields": ["Word", "Meaning", "WordType", "Gender", "Audio"], "notes": 6}
    assert types["Cloze"]["notes"] == 0


def test_list_note_types_filter_adds_templates(anki):
    [basic_rev] = [t for t in list_note_types("REVERSED")]
    assert basic_rev["notes"] == 3
    assert basic_rev["templates"]["Card 2"] == {"front": ["Back"], "back": ["Front", "Audio"]}
    # 'Basic' alone must not count the 'Basic (and reversed card)' notes
    assert next(t for t in list_note_types("basic") if t["note_type"] == "Basic")["notes"] == 1


def test_list_note_types_no_match_suggests(anki):
    with pytest.raises(AnkiError, match="Spanish"):
        list_note_types("Spansh")


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


@pytest.mark.parametrize("text, expected", [
    ("They fell silent as the {{c1::plangent}} bell pealed.", "plangent"),
    ("The show was {{c1::curtail}}ed.", "curtail"),
    ("healthy and {{c1::virile /ˈvɪr.əl/}}.", "virile"),
    ("remove {{c1::extraneous (/ɪkˈstreɪ.ni.əs/)}} details", "extraneous"),
    ("I {{c1::<b>scorn</b> /skɔːrn/}} it", "scorn"),
    ("{{c1::fell::adj}} and {{c2::chinwag}} and {{c3::fell}}", "fell, chinwag"),
    ("(adj.) no cloze here, and/or / slashes", ""),
])
def test_cloze_text_keeps_only_the_hidden_word(text, expected):
    assert server._cloze_text(text) == expected


def test_add_audio_cloze_only_speaks_the_cloze_word(anki):
    anki.add("Languages::English", "Cloze", {"Text": "(adj.) strong. Healthy and {{c1::virile /ˈvɪr.əl/}}."})
    anki.add("Languages::English", "Cloze", {"Text": "no deletion"})
    r = add_audio('"deck:Languages::English"', "Text", "Back Extra", "en-US", cloze_only=True)
    assert [x["text"] for x in r["would_voice"]] == ["virile"]
    assert r["skipped_no_cloze"] == 1


def test_add_audio_skips_yanki_notes(anki):
    r = add_audio('"deck:Go"', "Front", "Back", "es-MX")
    assert r["skipped_yanki"] == 1 and r["eligible"] == 0


def test_add_audio_does_not_speak_alternative_answers(anki):
    anki.add("Languages::Spanish", "Basic (and reversed card)",
             {"Front": 'Hay que irnos a dormir.<span class="alt">Vámonos a dormir.|Hay que dormirnos.</span>', "Back": "Let's go to sleep."})
    r = add_audio('"deck:Languages::Spanish" "note:Basic (and reversed card)"', "Front", "Audio", "es-MX")
    assert [x["text"] for x in r["would_voice"]] == ["Hay que irnos a dormir."]


# --- get_notes / update_notes ------------------------------------------------------------------------------------

def _note_id(anki, deck, first_value):
    return next(nid for nid, n in anki.notes.items() if n["deck"] == deck and next(iter(n["fields"].values())) == first_value)


def test_get_notes_raw_keeps_html_and_sound(anki):
    nid = _note_id(anki, "Languages::Latin", "Marcum <b>excitamus</b>.")
    assert get_notes([nid])[0]["fields"]["Front"] == "Marcum excitamus."
    assert get_notes([nid], raw=True)[0]["fields"]["Front"] == "Marcum <b>excitamus</b>."


def test_update_notes_dry_run_shows_diff_without_writing(anki):
    nid = _note_id(anki, "Languages::Latin", "Socius")
    r = update_notes([NoteUpdate(note_id=nid, fields={"Back": "Companion, ally"}, add_tags=["checked"])])
    assert r["dry_run"] and r["summary"] == {"would_update": 1}
    assert r["results"][0]["changes"] == {"Back": {"old": "Companion", "new": "Companion, ally"}}
    assert r["results"][0]["add_tags"] == ["checked"]
    assert anki.notes[nid]["fields"]["Back"] == "Companion" and "checked" not in anki.notes[nid]["tags"]


def test_update_notes_writes_fields_and_tags(anki):
    nid = _note_id(anki, "Languages::Latin", "Socius")
    r = update_notes([NoteUpdate(note_id=nid, fields={"Back": "Companion", "Front": "Socius, -i"}, add_tags=["checked"])], dry_run=False)
    assert r["summary"] == {"updated": 1}
    assert list(r["results"][0]["changes"]) == ["Front"]  # unchanged Back isn't rewritten
    assert anki.notes[nid]["fields"]["Front"] == "Socius, -i"
    assert anki.notes[nid]["tags"] == ["latin", "checked", "mcp-edited"]
    assert update_notes([NoteUpdate(note_id=nid, fields={"Front": "Socius, -i"})], dry_run=False)["summary"] == {"unchanged": 1}


def test_update_notes_refusals(anki):
    with_audio = _note_id(anki, "Languages::Latin", "Puella rosam amat.")
    yanki = _note_id(anki, "Go", "Channels?")
    r = update_notes([
        NoteUpdate(note_id=with_audio, fields={"Audio": ""}),
        NoteUpdate(note_id=yanki, fields={"Back": "x"}),
        NoteUpdate(note_id=with_audio, fields={"Sound": "x"}),
        NoteUpdate(note_id=999999, fields={"Front": "x"}),
        NoteUpdate(note_id=with_audio, fields={"Audio": "[sound:anki-mcp-x.m4a]", "Back": "The girl loves the rose!"}),
    ], dry_run=False)
    errors = [x.get("error", "") for x in r["results"]]
    assert "Would remove audio" in errors[0]
    assert "Obsidian" in errors[1]
    assert "Valid fields" in errors[2]
    assert "not found" in errors[3]
    assert r["results"][4]["status"] == "updated"  # keeping the [sound:] tag is fine
    assert anki.notes[yanki]["fields"]["Back"] == "Pipes."


# --- sync --------------------------------------------------------------------------------------------------------

def test_sync_normal(anki):
    assert sync()["synced"] is True
    assert anki.calls == ["sync"]


@pytest.mark.parametrize("status, reason", [(2, "can't be merged"), (3, "download everything"), (4, "upload everything")])
def test_sync_refuses_full_sync_with_a_readable_reason(anki, status, reason):
    anki.sync_error = f"Sync status {status} not one of [0, 1] - see SyncCollectionResponse.ChangesRequired"
    with pytest.raises(AnkiError, match=reason) as e:
        sync()
    assert "choose which side to keep" in str(e.value)


def test_sync_without_ankiweb_login_says_what_to_do(anki):
    anki.sync_error = "sync: auth not configured"
    with pytest.raises(AnkiError, match="log in"):
        sync()


@pytest.mark.parametrize("note, message", [
    ({**SPANISH, "fields": {"Word": "la ventana"}, "audio": {"field": "Audio", "voice": "es-MX", "text_field": "Word"}},
     r"Unknown key\(s\) \['text_field'\] in AudioSpec.*put the text to speak in 'text'"),
    ({"deck": "Default", "notetype": "Basic", "fields": {"Front": "q"}}, r"\['notetype'\] in NoteInput.*use 'note_type'"),
])
def test_add_notes_rejects_unknown_keys_with_a_fix(anki, note, message):
    # Pydantic used to drop these silently: an eval run passed audio.text_field copied from the deck profile.
    with pytest.raises(ValueError, match=message):
        NoteInput.model_validate(note)


def test_unknown_key_error_reaches_the_model(anki):
    import asyncio

    from mcp import Client

    async def run():
        async with Client(server.mcp) as c:
            return await c.call_tool("add_notes", {"notes": [{"deck": "Default", "fields": {"Front": "q"}, "tag": ["x"]}]})

    result = asyncio.run(run())
    assert result.is_error and "use 'tags' (a list)" in result.content[0].text
    assert anki.calls == []  # rejected before touching Anki


def _spanish_profile():
    from anki_mcp import profiles
    profiles.save({"Languages::Spanish": profiles.DeckProfile(note_type="Spanish")})


def test_omitted_note_type_comes_from_the_deck_profile(anki):
    _spanish_profile()
    out = add_notes([NoteInput(deck="Languages::Spanish", fields={"Word": "la ventana", "Meaning": "window"})], dry_run=True)
    assert out["results"][0] == {"index": 0, "status": "valid", "note_type": "Spanish (from deck profile)"}


def test_note_type_differing_from_profile_warns_but_is_used(anki):
    _spanish_profile()
    out = add_notes([NoteInput(deck="Languages::Spanish", note_type="Basic", fields={"Front": "q", "Back": "a"})], dry_run=True)
    assert out["results"][0]["status"] == "valid"
    assert "uses note type 'Spanish', not 'Basic'" in out["results"][0]["warning"]


def test_no_profile_means_basic_and_no_notice(anki):
    out = add_notes([NoteInput(deck="Default", fields={"Front": "q", "Back": "a"})], dry_run=True)
    assert out["results"][0] == {"index": 0, "status": "valid"}


def test_omitted_tags_come_from_the_deck_profile(anki):
    from anki_mcp import profiles
    profiles.save({"Languages::Spanish": profiles.DeckProfile(note_type="Spanish", tags=["Spanish::Added"])})
    out = add_notes([NoteInput(deck="Languages::Spanish", fields={"Word": "la ventana", "Meaning": "window"}),
                     NoteInput(deck="Languages::Spanish", fields={"Word": "el mapa", "Meaning": "map"},
                               tags=["Spanish::Duolingo::26_Places"])])
    assert out["results"][0]["tags"] == "['Spanish::Added'] (from deck profile)" and "tags" not in out["results"][1]
    tags = {n["fields"].get("Word"): n["tags"] for n in anki.notes.values()}
    assert tags["la ventana"] == ["Spanish::Added", "mcp-added"]
    assert tags["el mapa"] == ["Spanish::Duolingo::26_Places", "mcp-added"]


def test_add_notes_refuses_a_parent_made_only_of_yanki_subdecks(anki):
    # An eval run put a Kafka card in DDIA this way: outside every chapter subdeck and outside the vault.
    out = add_notes([NoteInput(deck="DDIA", fields={"Front": "Kafka consumer groups?", "Back": "One consumer per partition."})])
    assert out["summary"]["error"] == 1 and "Obsidian vault" in out["results"][0]["error"]
    assert "addNotes" not in anki.calls
    assert describe_deck("DDIA")["source"] == "yanki"
