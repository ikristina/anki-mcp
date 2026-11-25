import pytest

from anki_mcp import server
from anki_mcp.client import AnkiError
from anki_mcp.server import (AudioSpec, NoteInput, NoteUpdate, add_audio, add_notes, create_deck, delete_notes, describe_deck, export_deck, get_notes, get_weak_cards, list_decks,
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


def test_add_audio_replace_revoices_with_given_text_and_converges(anki, monkeypatch):
    monkeypatch.setattr(server, "validate_voice", lambda v: None)
    add_audio(LATIN_Q, "Front", "Audio", "espeak:la", dry_run=False)
    nid = _note_id(anki, "Languages::Latin", "Socius")
    plan = add_audio(LATIN_Q, "Front", "Audio", "piper:la", texts={nid: "Socius"}, replace=True, limit=1)
    assert plan["eligible"] == 3 and plan["would_voice"][0]["replaces"]
    r = add_audio(LATIN_Q, "Front", "Audio", "piper:la", texts={nid: "Sŏcius"}, replace=True, dry_run=False)
    assert (len(r["voiced"]), r["remaining"]) == (3, 0)
    audio = anki.notes[nid]["fields"]["Audio"]
    assert audio == f"[sound:{server.audio_stem('Sŏcius', 'piper:la')}.mp3]"  # old tag gone, override text spoken
    again = add_audio(LATIN_Q, "Front", "Audio", "piper:la", texts={nid: "Sŏcius"}, replace=True)
    assert again["eligible"] == 0 and again["already_has_audio"] == 3


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


# --- delete_notes ------------------------------------------------------------------------------------------------

def test_delete_notes_dry_run_lists_without_deleting(anki):
    nid = _note_id(anki, "Languages::Latin", "Socius")
    r = delete_notes([nid])
    assert r["dry_run"] and r["summary"] == {"would_delete": 1}
    assert r["results"][0]["deck"] == "Languages::Latin" and r["results"][0]["fields"]["Front"] == "Socius"
    assert nid in anki.notes and "deleteNotes" not in anki.calls


def test_delete_notes_deletes_only_approved_ids(anki):
    nid = _note_id(anki, "Languages::Latin", "Socius")
    other = _note_id(anki, "Languages::Latin", "Puella rosam amat.")
    r = delete_notes([nid, nid], dry_run=False)  # a repeated id is deleted once
    assert r["summary"] == {"deleted": 1} and "sync" in r["note"]
    assert nid not in anki.notes and other in anki.notes


def test_delete_notes_refusals(anki):
    yanki = _note_id(anki, "Go", "Channels?")
    r = delete_notes([yanki, 999999], dry_run=False)
    assert "Obsidian" in r["results"][0]["error"] and "not found" in r["results"][1]["error"]
    assert yanki in anki.notes and "deleteNotes" not in anki.calls


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


def test_create_deck_creates_missing_parents_and_is_idempotent(anki):
    assert create_deck("Hobbies::Chess", dry_run=True) == {"deck": "Hobbies::Chess", "created": False, "dry_run": True, "new_parents": ["Hobbies"]}
    assert "Hobbies::Chess" not in anki.decks
    assert create_deck("Hobbies::Chess")["created"] is True
    assert {"Hobbies", "Hobbies::Chess"} <= set(anki.decks)
    assert create_deck("Hobbies::Chess") == {"deck": "Hobbies::Chess", "created": False, "exists": True}


def test_create_deck_points_out_similar_names(anki):
    assert "Languages::Spanish" in create_deck("Languages::Spanish 2", dry_run=True)["similar_existing"]


@pytest.mark.parametrize("name", ["", "Languages::", "::Spanish", "Languages:: Spanish", 'Say "hi"'])
def test_create_deck_rejects_malformed_names(anki, name):
    with pytest.raises(AnkiError, match="Invalid deck name"):
        create_deck(name)
    assert "createDeck" not in anki.calls


def test_export_deck_writes_apkg_without_scheduling_by_default(anki, tmp_path):
    out = export_deck("Languages::Latin", path=str(tmp_path / "latin.apkg"))
    assert out == {"deck": "Languages::Latin", "path": str(tmp_path / "latin.apkg"), "notes": 3, "media_files": 1,
                   "bytes": (tmp_path / "latin.apkg").stat().st_size, "include_scheduling": False}
    assert anki.exported["includeSched"] is False


def test_export_deck_defaults_to_desktop_and_never_overwrites_silently(anki, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("ANKI_MCP_EXPORT_DIR", raising=False)
    (tmp_path / "Desktop").mkdir()
    assert export_deck("Languages::Latin", include_scheduling=True)["path"] == str(tmp_path / "Desktop" / "Latin.apkg")
    assert anki.exported["includeSched"] is True
    with pytest.raises(AnkiError, match="already exists"):
        export_deck("Languages::Latin")
    assert export_deck("Languages::Latin", overwrite=True)["notes"] == 3


def test_export_deck_default_folder_comes_from_env(anki, tmp_path, monkeypatch):
    monkeypatch.setenv("ANKI_MCP_EXPORT_DIR", str(tmp_path / "drive"))
    (tmp_path / "drive").mkdir()
    assert export_deck("Languages::Latin")["path"] == str(tmp_path / "drive" / "Latin.apkg")


@pytest.mark.parametrize("deck, path, error", [
    ("Languages::Latn", None, "does not exist. Similar: .*Languages::Latin"),
    ("Languages::Latin", "latin.apkg", "must be absolute"),
    ("Languages::Latin", "/tmp/latin.zip", "end in .apkg"),
    ("Languages::Latin", "/no/such/folder/latin.apkg", "does not exist. Pick an existing folder"),
])
def test_export_deck_checks_deck_and_path_before_exporting(anki, deck, path, error):
    with pytest.raises(AnkiError, match=error):
        export_deck(deck, path=path)
    assert "exportPackage" not in anki.calls


# --- move_cards ----------------------------------------------------------------------------------------------------

def _deck_of(anki, word):
    return next(n["deck"] for n in anki.notes.values() if n["fields"].get("Word") == word)


def test_move_cards_is_a_dry_run_by_default(anki):
    out = server.move_cards('"deck:Languages::Spanish" cumbre', "Default")
    assert out["dry_run"] is True and out["would_move"] == 1 and "moved" not in out
    assert out["from"] == {"Languages::Spanish": 1} and out["sample"][0]["fields"]["Word"] == "la cumbre" and "Audio" not in out["sample"][0]["fields"]
    assert _deck_of(anki, "la cumbre") == "Languages::Spanish" and "changeDeck" not in anki.calls


def test_move_cards_moves_keeps_the_note_mod_time_and_says_how_to_undo(anki):
    nid = next(i for i, n in anki.notes.items() if n["fields"].get("Word") == "el perro")
    mod = anki.notes[nid]["mod"]
    out = server.move_cards('"deck:Languages::Spanish" note:Spanish', "Default", dry_run=False)
    assert out["moved"] == 6 and out["from"] == {"Languages::Spanish": 6} and "from" in out["note"]
    assert _deck_of(anki, "el perro") == "Default" and anki.notes[nid]["mod"] == mod


def test_move_cards_leaves_cards_already_in_the_target_alone(anki):
    out = server.move_cards('"deck:Languages::Latin"', "Languages::Latin", dry_run=False)
    assert out["moved"] == 0 and out["already_in_target"] == 3 and "changeDeck" not in anki.calls


def test_move_cards_skips_yanki_cards_and_refuses_a_yanki_target(anki):
    out = server.move_cards('"deck:Go"', "Default", dry_run=False)
    assert out["moved"] == 0 and out["skipped_yanki"] == 1
    assert next(n for n in anki.notes.values() if n["fields"].get("Front") == "Channels?")["deck"] == "Go"
    with pytest.raises(AnkiError, match="Yanki"):
        server.move_cards("deck:Default", "Go")


def test_move_cards_errors_say_what_to_do(anki):
    with pytest.raises(AnkiError, match="Similar: .*'Languages::Spanish'"):
        server.move_cards("deck:Default", "Languages::Spansh")
    with pytest.raises(AnkiError, match="No cards match"):
        server.move_cards("zzzznothing", "Default")
    with pytest.raises(AnkiError, match="empty one would match every card"):
        server.move_cards("  ", "Default")
    for i in range(201):
        anki.add("Default", "Basic", {"Front": f"bulk {i}", "Back": "x"})
    with pytest.raises(AnkiError, match="limit is 200"):
        server.move_cards('"deck:Default"', "Languages")


def test_move_cards_counts_notes_whose_other_cards_stay_behind(anki):
    real = anki.a_notesInfo
    anki.a_notesInfo = lambda notes: [{**n, "cards": n["cards"] + [n["cards"][0] + 1]} for n in real(notes)]
    out = server.move_cards('"deck:Languages::Spanish" cumbre', "Default")
    assert out["notes_left_partly_behind"] == 1


# --- convert_notes -------------------------------------------------------------------------------------------------

import json as _json  # noqa: E402

BASIC_TO_SPANISH = {"Word": "Front", "Meaning": "Back"}


def _basics(anki, n, deck="Default", tags=("t1",)):
    return [anki.add(deck, "Basic", {"Front": f"palabra {i}", "Back": f"word {i}"}, tags=tags) for i in range(n)]


def test_convert_dry_run_plans_and_changes_nothing(anki, tmp_path):
    ids = _basics(anki, 3)
    out = server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH)
    assert out["dry_run"] and out["notes"] == 3 and out["source_note_type"] == "Basic" and out["would_lose"] == {}
    assert out["mapping"] == {"Word": "Front", "Meaning": "Back", "WordType": None, "Gender": None, "Audio": None}
    assert out["decks_to_back_up"] == ["Default"] and out["needs_trial_first"] is False
    assert out["sample"][0]["before"] == {"Front": "palabra 0", "Back": "word 0"} and out["sample"][0]["after"]["Word"] == "palabra 0"
    assert all(anki.notes[i]["model"] == "Basic" for i in ids) and "updateNoteModel" not in anki.calls
    assert not (tmp_path / "backups").exists()


def test_convert_refuses_without_confirmation_of_synced_devices(anki):
    _basics(anki, 2)
    with pytest.raises(AnkiError, match="every device is fully synced"):
        server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH, dry_run=False)
    assert "updateNoteModel" not in anki.calls


def test_convert_backs_up_converts_keeps_tags_and_logs(anki, tmp_path):
    ids = _basics(anki, 2, tags=("keep-me",))
    out = server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH, dry_run=False, confirm_all_devices_synced=True)
    assert out["converted"] == 2 and out["failed"] == []
    n = anki.notes[ids[0]]
    assert n["model"] == "Spanish" and n["fields"]["Word"] == "palabra 0" and n["fields"]["Meaning"] == "word 0" and n["fields"]["Gender"] == ""
    assert sorted(n["tags"]) == ["keep-me", "mcp-converted"]
    assert len(out["backups"]) == 1 and out["backups"][0].endswith("Default.apkg") and anki.exported["includeSched"] is True
    assert (tmp_path / "backups").is_dir() and "Upload to AnkiWeb" in out["next"]
    rec = _json.loads((tmp_path / "convert_history.jsonl").read_text())
    assert rec["converted"] == ids and rec["source"] == "Basic" and rec["target"] == "Spanish" and rec["backups"] == out["backups"]


def test_convert_unmapped_content_needs_drop_fields(anki):
    _basics(anki, 2)
    kw = dict(dry_run=False, confirm_all_devices_synced=True)
    assert server.convert_notes('"deck:Default"', "Spanish", {"Word": "Front"})["would_lose"] == {"Back": 2}
    with pytest.raises(AnkiError, match=r"\['Back'\].*drop_fields"):
        server.convert_notes('"deck:Default"', "Spanish", {"Word": "Front"}, **kw)
    assert server.convert_notes('"deck:Default"', "Spanish", {"Word": "Front"}, drop_fields=["Back"], **kw)["converted"] == 2


def test_convert_big_batches_need_a_trial_first(anki):
    ids = _basics(anki, 25)
    kw = dict(dry_run=False, confirm_all_devices_synced=True)
    assert server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH)["needs_trial_first"] is True
    with pytest.raises(AnkiError, match="trial"):
        server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH, **kw)
    assert server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH, limit=5, **kw)["converted"] == 5
    rest = server.convert_notes('"deck:Default" note:Basic', "Spanish", BASIC_TO_SPANISH, **kw)  # trial done: the rest may go
    assert rest["converted"] == 20 and all(anki.notes[i]["model"] == "Spanish" for i in ids)


def test_convert_stops_at_the_first_error_and_logs_what_was_done(anki, tmp_path):
    ids = _basics(anki, 4)
    anki.fail_convert_ids = {ids[2]}
    out = server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH, dry_run=False, confirm_all_devices_synced=True)
    assert out["converted"] == 2 and out["failed"][0]["note_id"] == ids[2] and "Stopped at the first error" in out["next"]
    assert anki.notes[ids[3]]["model"] == "Basic"
    assert _json.loads((tmp_path / "convert_history.jsonl").read_text())["converted"] == ids[:2]


def test_convert_aborts_before_changing_anything_when_the_backup_fails(anki):
    ids = _basics(anki, 2)
    anki.a_exportPackage = lambda deck, path, includeSched=False: False
    with pytest.raises(AnkiError, match="did not export"):
        server.convert_notes('"deck:Default"', "Spanish", BASIC_TO_SPANISH, dry_run=False, confirm_all_devices_synced=True)
    assert all(anki.notes[i]["model"] == "Basic" for i in ids) and "updateNoteModel" not in anki.calls


def test_convert_input_errors_say_what_to_do(anki):
    _basics(anki, 1)
    with pytest.raises(AnkiError, match="Yanki"):
        server.convert_notes('"deck:Go"', "Basic", {"Front": "Front"})
    with pytest.raises(AnkiError, match="several note types"):
        server.convert_notes('"deck:Languages"', "Spanish", BASIC_TO_SPANISH)
    with pytest.raises(AnkiError, match="already have"):
        server.convert_notes('"deck:Default"', "Basic", {"Front": "Front"})
    with pytest.raises(AnkiError, match="does not exist"):
        server.convert_notes('"deck:Default"', "Nope", BASIC_TO_SPANISH)
    with pytest.raises(AnkiError, match=r"Unknown target fields \['Wrod'\]"):
        server.convert_notes('"deck:Default"', "Spanish", {"Wrod": "Front"})
    with pytest.raises(AnkiError, match=r"unknown source fields \['Question'\]"):
        server.convert_notes('"deck:Default"', "Spanish", {"Word": "Question"})
    with pytest.raises(AnkiError, match="No notes match"):
        server.convert_notes("zzzznothing", "Spanish", BASIC_TO_SPANISH)


def test_convert_refuses_templates_that_do_not_line_up(anki):
    with pytest.raises(AnkiError, match="templates don't line up"):
        server.convert_notes('"deck:Languages::Latin"', "Spanish", {"Word": "Front", "Meaning": "Back"})


# --- create_note_type ----------------------------------------------------------------------------------------------

from pathlib import Path as _Path  # noqa: E402

ENGLISH_VOCAB = str(_Path(__file__).resolve().parent.parent / "examples" / "note_types" / "english-vocab.json")
SIMPLE = dict(name="Mini Vocab", fields=["Word", "Meaning"],
              templates=[server.CardTemplate(name="Card 1", front="{{Word}}", back="{{FrontSide}}<hr id=answer>{{Meaning}}")])


def test_the_shipped_english_vocab_definition_is_valid(anki):
    out = server.create_note_type(definition_file=ENGLISH_VOCAB)
    assert out["dry_run"] and out["name"] == "English Vocab" and out["fields"][0] == "Word"
    assert [t["name"] for t in out["templates"]] == ["Recognition", "Recall"]
    assert out["templates"][1]["front_uses"] == ["Definition", "Gap", "Word"] and "full sync" in out["next"]
    assert anki.created_model is None


def test_create_note_type_dry_run_creates_nothing(anki):
    out = server.create_note_type(**SIMPLE)
    assert out["dry_run"] and anki.created_model is None and "createModel" not in anki.calls


def test_create_note_type_needs_confirmation_of_synced_devices(anki):
    with pytest.raises(AnkiError, match="every device is fully synced"):
        server.create_note_type(**SIMPLE, dry_run=False)
    assert anki.created_model is None


def test_create_note_type_creates_it_and_says_to_do_the_full_sync(anki):
    out = server.create_note_type(**SIMPLE, css=".card{}", dry_run=False, confirm_all_devices_synced=True)
    assert out["created"] == "Mini Vocab" and "Upload to AnkiWeb" in out["next"] and "Adding a note type is probably" in out["next"]
    assert anki.created_model["fields"] == ["Word", "Meaning"] and anki.created_model["css"] == ".card{}"
    assert anki.created_model["templates"][0] == {"Name": "Card 1", "Front": "{{Word}}", "Back": "{{FrontSide}}<hr id=answer>{{Meaning}}"}
    assert invoke_names(anki) and "Mini Vocab" in anki.a_modelNames()


def invoke_names(anki):
    return anki.a_modelFieldNames("Mini Vocab") == ["Word", "Meaning"]


def test_create_note_type_checks_placeholders_and_names(anki):
    bad = server.CardTemplate(name="Card 1", front="{{Wrod}}", back="{{Meaning}}")
    with pytest.raises(AnkiError, match="front uses none of the fields"):
        server.create_note_type(name="X", fields=["Word", "Meaning"], templates=[bad])
    bad = server.CardTemplate(name="Card 1", front="{{Word}}", back="{{Meening}}")
    with pytest.raises(AnkiError, match=r"unknown placeholders \['Meening'\]"):
        server.create_note_type(name="X", fields=["Word", "Meaning"], templates=[bad])
    with pytest.raises(AnkiError, match="already exists"):
        server.create_note_type(**{**SIMPLE, "name": "Basic"})
    with pytest.raises(AnkiError, match="Field names must be unique"):
        server.create_note_type(**{**SIMPLE, "fields": ["Word", "Word"]})
    with pytest.raises(AnkiError, match="needs a name, fields"):
        server.create_note_type(name="X")
    with pytest.raises(AnkiError, match="Cannot read note type definition"):
        server.create_note_type(definition_file="/nonexistent/x.json")


def test_template_refs_understand_anki_syntax():
    refs = server._template_refs("{{#IPA}}{{IPA}}{{/IPA}} {{type:Word}} {{cloze:Text}} {{FrontSide}} {{^Image}}x{{/Image}} {{hint:Gap}}")
    assert refs == {"IPA", "Word", "Text", "FrontSide", "Image", "Gap"}


# --- suspend_cards -------------------------------------------------------------------------------------------------

def _suspended(anki):
    return {n["fields"].get("Word") or n["fields"].get("Front") for n in anki.notes.values() if n["suspended"]}


def test_suspend_cards_dry_run_changes_nothing(anki):
    out = server.suspend_cards('"deck:Languages::Spanish" is:new')
    assert out["dry_run"] and out["would_change"] == 6 and out["by_deck"] == {"Languages::Spanish": 6}
    assert _suspended(anki) == set() and "suspend" not in anki.calls


def test_suspend_only_unseen_keeps_seen_cards_studyable(anki):
    seen = anki.add("Languages::Spanish", "Spanish", {"Word": "estudiado", "Meaning": "studied"}, lapses=2)
    out = server.suspend_cards('"deck:Languages::Spanish" is:new', dry_run=False)
    assert out["changed"] == 6 and anki.notes[seen]["suspended"] is False and "Channels?" not in _suspended(anki)


def test_suspend_tags_notes_but_not_yanki_and_skips_already_suspended(anki):
    out = server.suspend_cards('"deck:Go"', dry_run=False)
    go = next(n for n in anki.notes.values() if n["fields"].get("Front") == "Channels?")
    assert out["changed"] == 1 and out["yanki_cards"] == 1 and go["suspended"] and "mcp-suspended" not in go["tags"]
    server.suspend_cards('"deck:Languages::Latin"', dry_run=False)
    latin = next(n for n in anki.notes.values() if n["fields"].get("Front") == "Socius")
    assert latin["suspended"] and "mcp-suspended" in latin["tags"] and "latin" in latin["tags"]
    again = server.suspend_cards('"deck:Languages::Latin"')
    assert again["would_change"] == 0 and again["skipped"] == 3 and again["skipped_because"] == "already suspended"


def test_unsuspend_restores_only_what_the_tool_suspended(anki):
    mine = server.suspend_cards('"deck:Languages::Latin"', dry_run=False)
    manual = next(n for n in anki.notes.values() if n["fields"].get("Word") == "el perro")
    manual["suspended"] = True  # the user suspended this one by hand
    out = server.suspend_cards('"deck:Languages::Latin"', suspend=False, dry_run=False)
    assert mine["changed"] == 3 and out["changed"] == 3 and _suspended(anki) == {"el perro"}
    assert all("mcp-suspended" not in n["tags"] for n in anki.notes.values())
    other = server.suspend_cards('"deck:Languages::Spanish"', suspend=False)
    assert other["would_change"] == 0 and "not suspended by this tool" in other["skipped_because"]


def test_unsuspend_limit_wakes_cards_gradually(anki):
    server.suspend_cards('"deck:Languages::Spanish"', dry_run=False)
    out = server.suspend_cards('"deck:Languages::Spanish"', suspend=False, limit=2, dry_run=False)
    assert out["changed"] == 2 and len(_suspended(anki)) == 4


def test_suspend_cards_errors_say_what_to_do(anki):
    with pytest.raises(AnkiError, match="empty one would match every card"):
        server.suspend_cards(" ")
    with pytest.raises(AnkiError, match="No cards match"):
        server.suspend_cards("zzzznothing")


# --- restyle_note_type ---------------------------------------------------------------------------------------------

from anki_mcp.server import ImageSpec, LookEdit, deck_options, restyle_note_type  # noqa: E402

BASIC_CSS = LookEdit(old="color: black", new="color: #17181a")


def test_list_note_types_full_shows_css_and_template_html(anki):
    basic = next(t for t in list_note_types("Basic", full=True) if t["note_type"] == "Basic")
    assert basic["css"] == ".card { color: black; }" and basic["template_html"]["Card 1"]["front"] == "{{Front}}"


def test_restyle_dry_run_shows_diff_and_decks_and_writes_nothing(anki):
    out = restyle_note_type("Basic", [BASIC_CSS, LookEdit(card="Card 1", side="front", new='<div class="q">{{Front}}</div>')])
    assert out["dry_run"] and "+.card { color: #17181a; }" in out["diff"]["css"] and "Card 1 front" in out["diff"]
    assert out["decks"] == {"Languages::_Pimsleur": 1}
    assert anki.css["Basic"] == ".card { color: black; }" and "updateModelStyling" not in anki.calls


def test_restyle_writes_after_a_backup(anki, tmp_path):
    out = restyle_note_type("Basic", [BASIC_CSS, LookEdit(card="Card 1", side="back", old="<hr id=answer>", new="<hr id=answer><b>A</b>")], dry_run=False)
    assert anki.css["Basic"] == ".card { color: #17181a; }" and "<b>A</b>" in anki.html["Basic"]["Card 1"]["Back"]
    saved = json.loads(Path(out["backup"]).read_text())
    assert saved["css"] == ".card { color: black; }" and saved["templates"]["Card 1"]["Back"] == "{{FrontSide}}<hr id=answer>{{Back}}"


@pytest.mark.parametrize("edit, msg", [
    (LookEdit(old="nope", new="x"), "occurs 0 times"),
    (LookEdit(card="Card 1", side="front", new="{{Nope}}"), "unknown placeholders"),
    (LookEdit(card="Card 1", side="front", new="no fields here"), "blank"),
    (LookEdit(card="Card 9", side="front", new="{{Front}}"), "no card 'Card 9'"),
    (LookEdit(card="Card 1", new="{{Front}}"), "side='front' or 'back'"),
    (LookEdit(old="color: black", new="color: black"), "change nothing"),
])
def test_restyle_refuses_bad_edits_before_writing(anki, edit, msg):
    with pytest.raises(AnkiError, match=msg):
        restyle_note_type("Basic", [edit], dry_run=False)
    assert "updateModelStyling" not in anki.calls and "updateModelTemplates" not in anki.calls


def test_restyle_refuses_yanki_note_types(anki):
    with pytest.raises(AnkiError, match="Yanki"):
        restyle_note_type("Yanki - Basic", [BASIC_CSS])


# --- full_sync_pending -------------------------------------------------------------------------------------------

import json  # noqa: E402
import sqlite3  # noqa: E402
from pathlib import Path  # noqa: E402


def _collection(monkeypatch, tmp_path, scm, ls):
    db = tmp_path / f"collection-{scm}-{ls}.anki2"
    con = sqlite3.connect(db)
    con.execute("create table col (scm integer, ls integer)")
    con.execute("insert into col values (?, ?)", (scm, ls))
    con.commit()
    con.close()
    monkeypatch.setenv("ANKI_MCP_COLLECTION", str(db))


def test_full_sync_pending_reads_the_collection(anki, monkeypatch, tmp_path):
    assert server._full_sync_pending() is None  # the fixture points at a missing file
    _collection(monkeypatch, tmp_path, scm=2, ls=1)
    assert server._full_sync_pending() is True
    _collection(monkeypatch, tmp_path, scm=1, ls=2)
    assert server._full_sync_pending() is False


def test_sync_refuses_when_the_schema_changed_since_last_sync(anki, monkeypatch, tmp_path):
    _collection(monkeypatch, tmp_path, scm=2, ls=1)
    with pytest.raises(AnkiError, match="full sync"):
        sync()
    assert "sync" not in anki.calls


# --- deck_options ------------------------------------------------------------------------------------------------

def test_deck_options_reads_and_lists_decks_sharing_the_preset(anki):
    out = deck_options("Languages::Spanish")
    assert out["preset"] == "Default" and out["new_per_day"] == 20 and "Go" in out["preset_shared_with"] and "dry_run" not in out


def test_deck_options_shared_preset_needs_agreement(anki):
    assert "shared with" in deck_options("Languages::Spanish", new_per_day=5)["next"]
    with pytest.raises(AnkiError, match="change_shared_preset=True"):
        deck_options("Languages::Spanish", new_per_day=5, dry_run=False)
    assert anki.configs[1]["new"]["perDay"] == 20
    deck_options("Languages::Spanish", new_per_day=5, dry_run=False, change_shared_preset=True)
    assert anki.configs[1]["new"]["perDay"] == 5


def test_deck_options_changes_an_own_preset(anki):
    out = deck_options("Languages::Latin", reviews_per_day=50, dry_run=False)
    assert out["changed"] == {"reviews_per_day": {"old": 100, "new": 50}} and anki.configs[2]["rev"]["perDay"] == 50
    assert anki.configs[2]["new"]["perDay"] == 10


# --- images ------------------------------------------------------------------------------------------------------

def _png(tmp_path):
    p = tmp_path / "cat.png"
    p.write_bytes(b"\x89PNG fake")
    return str(p)


def test_add_notes_attaches_an_image(anki, tmp_path):
    note = NoteInput(deck="Default", fields={"Front": "gato", "Back": "cat"}, image=ImageSpec(field="Back", path=_png(tmp_path)))
    nid = add_notes([note])["results"][0]["note_id"]
    back = anki.notes[nid]["fields"]["Back"]
    name = back.split('src="')[1].split('"')[0]
    assert back.startswith("cat<img src=") and name.startswith("anki-mcp-") and name in anki.media


@pytest.mark.parametrize("path, msg", [("/tmp/x.pdf", "absolute path to a"), ("/nonexistent/x.png", "does not exist")])
def test_add_notes_rejects_bad_images(anki, path, msg):
    note = NoteInput(deck="Default", fields={"Front": "q", "Back": "a"}, image=ImageSpec(field="Back", path=path))
    assert msg in add_notes([note], dry_run=True)["results"][0]["error"]


def test_update_notes_appends_an_image_once(anki, tmp_path):
    nid = next(i for i, n in anki.notes.items() if n["fields"].get("Front") == "Socius")
    upd = NoteUpdate(note_id=nid, image=ImageSpec(field="Back", path=_png(tmp_path)))
    assert update_notes([upd])["results"][0]["status"] == "would_update" and not anki.media
    update_notes([upd], dry_run=False)
    assert "<img src=" in anki.notes[nid]["fields"]["Back"] and len(anki.media) == 1
    assert update_notes([upd])["results"][0]["status"] == "unchanged"
