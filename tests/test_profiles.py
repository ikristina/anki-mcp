import json

import pytest

from anki_mcp import profiles
from anki_mcp.client import AnkiError
from anki_mcp.profiles import AudioProfile, DeckProfile
from anki_mcp.server import describe_deck, get_deck_profile, set_deck_profile

LATIN = "Languages::Latin"
LATIN_PROFILE = DeckProfile(
    note_type="Basic (and reversed card)",
    audio=AudioProfile(field="Audio", voice="espeak:la", text_field="Front"),
    fields={"Front": "Latin", "Back": "English"},
    tags=["latin"],
)


def test_missing_profile_points_to_describe_deck(anki):
    r = get_deck_profile(LATIN)
    assert r["profile"] is None and "describe_deck" in r["hint"]


def test_set_then_get_roundtrip_and_shown_in_describe(anki):
    set_deck_profile(LATIN, LATIN_PROFILE)
    assert get_deck_profile(LATIN)["profile"]["audio"] == {"field": "Audio", "voice": "espeak:la", "text_field": "Front"}
    assert get_deck_profile()["profiles"].keys() == {LATIN}
    assert describe_deck(LATIN)["profile"]["tags"] == ["latin"]
    # human-editable JSON, defaults omitted
    assert json.loads(profiles.path().read_text())[LATIN]["note_type"] == "Basic (and reversed card)"


def test_partial_update_merges_and_replace_overwrites(anki):
    set_deck_profile(LATIN, LATIN_PROFILE)
    set_deck_profile(LATIN, DeckProfile(conventions=["Tag duolingo for Duolingo sentences."]))
    p = get_deck_profile(LATIN)["profile"]
    assert p["audio"]["voice"] == "espeak:la" and p["conventions"] == ["Tag duolingo for Duolingo sentences."]
    set_deck_profile(LATIN, DeckProfile(tags=["x"]), replace=True)
    assert get_deck_profile(LATIN)["profile"] == {"tags": ["x"]}


@pytest.mark.parametrize(
    "deck, profile, match",
    [
        ("Latn", LATIN_PROFILE, "Languages::Latin"),
        (LATIN, DeckProfile(note_type="Nope"), "does not exist"),
        (LATIN, DeckProfile(note_type="Basic (and reversed card)", audio=AudioProfile(field="Sound", voice="fr")), "Valid fields"),
        (LATIN, DeckProfile(note_type="Basic (and reversed card)", audio=AudioProfile(field="Audio", voice="la")), "Latin"),
        (LATIN, DeckProfile(fields={"Front": "x"}), "note_type"),
    ],
)
def test_set_rejects_bad_profiles_and_saves_nothing(anki, deck, profile, match):
    with pytest.raises(AnkiError, match=match):
        set_deck_profile(deck, profile)
    assert not profiles.path().exists()


def test_invalid_file_is_reported(anki):
    profiles.path().write_text("{not json")
    with pytest.raises(AnkiError, match="invalid"):
        get_deck_profile(LATIN)
