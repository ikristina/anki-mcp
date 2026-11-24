"""Indexer logic against FakeAnki with an in-memory store and a fake embedder (no Redis, no model download)."""

import numpy as np
import pytest

from anki_mcp import server
from anki_mcp.client import AnkiError

from anki_mcp import rag


class FakeEmbedder:
    model_name = "fake-model"

    def __init__(self):
        self.embedded: list[str] = []

    def embed(self, texts):
        self.embedded += texts
        out = np.zeros((len(texts), rag.DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, hash(t) % rag.DIM] = 1.0
        return out


class MemoryStore:
    def __init__(self):
        self.items: dict[int, dict] = {}
        self.meta: dict = {}
        self.built = False

    def ensure_index(self, model, dim):
        rebuilt = not self.built or (self.meta and self.meta["model"] != model)
        if rebuilt:
            self.items.clear()
        self.built = True
        return rebuilt

    def clear(self):
        self.items.clear()
        self.built = False
        self.meta = {}

    def mods(self):
        return {i: it["mod"] for i, it in self.items.items()}

    def upsert(self, items):
        for it in items:
            self.items[it["id"]] = it

    def delete(self, ids):
        for i in ids:
            self.items.pop(i, None)

    def finish(self, model, dim):
        self.meta = {"model": model, "dim": dim}

    def info(self):
        return {"notes": len(self.items), "model": self.meta.get("model"), "indexed_at": 1_700_000_000 if self.meta else None}

    def search(self, vector, k=5, deck=None):
        rows = [it for it in self.items.values() if not deck or it["deck"] == deck or it["deck"].startswith(deck + "::")]
        rows.sort(key=lambda it: -float(it["vector"] @ vector))
        return [{"note_id": it["id"], "similarity": round(float(it["vector"] @ vector), 4), "deck": it["deck"],
                 "note_type": it["note_type"], "text": it["text"]} for it in rows[:k]]


def run(anki, store, emb, **kw):
    return rag.sync_index(store, emb, call=anki, **kw)


def test_first_run_indexes_every_note_with_deck_and_type(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    res = run(anki, store, emb)
    assert res["indexed"] == len(anki.notes) and res["unchanged"] == 0 and res["removed"] == 0
    perro = next(it for it in store.items.values() if it["text"].startswith("el perro"))
    assert perro["deck"] == "Languages::Spanish" and perro["note_type"] == "Spanish"
    assert store.meta == {"model": "fake-model", "dim": rag.DIM}


def test_second_run_embeds_nothing(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    run(anki, store, emb)
    emb.embedded.clear()
    res = run(anki, store, emb)
    assert res["indexed"] == 0 and res["unchanged"] == len(anki.notes) and emb.embedded == []


def test_only_changed_notes_are_re_embedded_and_deleted_ones_removed(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    run(anki, store, emb)
    emb.embedded.clear()
    nid = next(i for i, n in anki.notes.items() if n["fields"].get("Word") == "correr")
    anki.a_updateNoteFields({"id": nid, "fields": {"Meaning": "to jog"}})
    victim = next(i for i, n in anki.notes.items() if n["fields"].get("Word") == "la casa")
    anki.a_deleteNotes([victim])
    new = anki.add("Languages::Spanish", "Spanish", {"Word": "el gato", "Meaning": "cat"})
    res = run(anki, store, emb)
    assert (res["indexed"], res["removed"]) == (2, 1)
    assert len(emb.embedded) == 2 and any("to jog" in t for t in emb.embedded)
    assert victim not in store.items and new in store.items


def test_blank_notes_are_skipped_and_removed_if_they_become_blank(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    nid = anki.add("Languages::Spanish", "Spanish", {"Word": "", "Meaning": ""})
    res = run(anki, store, emb)
    assert res["skipped_empty"] == 1 and nid not in store.items
    other = next(i for i, n in anki.notes.items() if n["fields"].get("Word") == "correr")
    anki.a_updateNoteFields({"id": other, "fields": {f: "" for f in anki.notes[other]["fields"]}})
    res = run(anki, store, emb)
    # a blank note stores nothing, so it is re-read (cheap, never embedded) on every run and counted again
    assert other not in store.items and res["skipped_empty"] == 2


def test_limit_caps_work_and_reports_remaining(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    res = run(anki, store, emb, limit=3)
    assert (res["indexed"], res["remaining"]) == (3, len(anki.notes) - 3)
    res = run(anki, store, emb)
    assert res["indexed"] == len(anki.notes) - 3 and res["remaining"] == 0


def test_full_rebuilds_everything(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    run(anki, store, emb)
    assert run(anki, store, emb, full=True)["indexed"] == len(anki.notes)


def test_model_change_rebuilds(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    run(anki, store, emb)
    emb2 = FakeEmbedder()
    emb2.model_name = "other-model"
    assert run(anki, store, emb2)["indexed"] == len(anki.notes) and store.meta["model"] == "other-model"


def test_note_text_strips_html_sound_alternatives_and_cloze():
    note = {"fields": {
        "Text": {"order": 0, "value": "An {{c1::availability zone}} is <b>a</b> {{c2::datacenter::hint}}"},
        "Back": {"order": 1, "value": 'hola<span class="alt">hello</span> [sound:x.mp3]'},
    }}
    assert rag.note_text(note) == "An availability zone is a datacenter | hola"


def test_escape_tag_handles_deck_names():
    assert rag._escape_tag("Languages::🇪🇸 Spanish") == r"Languages\:\:🇪🇸\ Spanish"


def test_a_missing_card_does_not_abort_the_run(anki):
    store, emb = MemoryStore(), FakeEmbedder()
    nid = next(iter(anki.notes))
    real = anki.a_getDecks

    def flaky(cards):
        if any(c == nid * 10 for c in cards):
            if len(cards) > 1:
                raise rag.AnkiError("AnkiConnect error on 'getDecks': Card was not found")
            raise rag.AnkiError("AnkiConnect error on 'getDecks': Card was not found")
        return real(cards)

    anki.a_getDecks = flaky
    res = run(anki, store, emb)
    assert res["indexed"] == len(anki.notes) and store.items[nid]["deck"] == "?"
    assert store.items[nid + 1]["deck"] != "?"


@pytest.fixture
def indexed(anki, monkeypatch):
    store, emb = MemoryStore(), FakeEmbedder()
    run(anki, store, emb)
    monkeypatch.setattr(rag, "get_store", lambda: store)
    monkeypatch.setattr(rag, "get_embedder", lambda: emb)
    return store


def test_find_similar_notes_by_text_ranks_best_first_with_index_info(anki, indexed):
    text = next(it["text"] for it in indexed.items.values() if it["text"].startswith("el perro"))
    out = server.find_similar_notes(text=text, limit=3)
    assert out["results"][0]["similarity"] == 1.0 and out["results"][0]["text"].startswith("el perro")
    assert len(out["results"]) == 3 and out["index"]["notes"] == len(anki.notes) and out["index"]["indexed_at"]


def test_find_similar_notes_by_note_id_leaves_that_note_out(anki, indexed):
    nid = next(i for i, n in anki.notes.items() if n["fields"].get("Word") == "el perro")
    out = server.find_similar_notes(note_id=nid, limit=20)
    assert nid not in [r["note_id"] for r in out["results"]] and len(out["results"]) == len(anki.notes) - 1


def test_find_similar_notes_deck_filter_includes_subdecks(anki, indexed):
    out = server.find_similar_notes(text="anything", deck="Languages", limit=20)
    assert out["results"] and all(r["deck"].startswith("Languages") for r in out["results"])
    assert not server.find_similar_notes(text="anything", deck="Nonexistent")["results"]


def test_find_similar_notes_rejects_bad_arguments_with_a_fix(anki, indexed):
    for kw in ({}, {"text": "x", "note_id": 1000}):
        with pytest.raises(AnkiError, match="exactly one"):
            server.find_similar_notes(**kw)
    with pytest.raises(AnkiError, match="No note with id 424242"):
        server.find_similar_notes(note_id=424242)


def test_find_similar_notes_with_an_empty_index_says_how_to_build_it(anki, monkeypatch):
    monkeypatch.setattr(rag, "get_store", lambda: MemoryStore())
    monkeypatch.setattr(rag, "get_embedder", lambda: FakeEmbedder())
    with pytest.raises(AnkiError, match="anki-mcp-index"):
        server.find_similar_notes(text="x")


# --- 8.4: near-duplicate warnings in add_notes -------------------------------------------------------------------

from anki_mcp.server import NoteInput, add_notes  # noqa: E402

SPANISH = {"deck": "Languages::Spanish", "note_type": "Spanish"}


def _add(*words, **kw):
    notes = [NoteInput(**SPANISH, fields={"Word": w, "Meaning": m}) for w, m in words]
    return add_notes(notes, dry_run=True, **kw)


def test_core_text_ignores_article_case_accents_labels_and_html():
    for v in ("cumbre", "la cumbre", "La Cumbre", "<b>la cumbre</b> (peak)", "[sound:x.mp3]la cumbre"):
        assert rag.core_text(v) == "cumbre", v
    assert rag.core_text("rápido") == rag.core_text("Rapido") == "rapido"
    assert rag.core_text("el gato (Spain: gato)") == "gato"


def test_same_word_without_article_is_flagged_even_without_the_semantic_index(anki):
    out = _add(("cumbre", "summit"))
    hit = out["results"][0]
    assert hit["status"] == "valid"  # a warning, never a block
    assert hit["similar"][0]["text"].startswith("la cumbre") and "same word" in hit["similar"][0]["match"]
    assert hit["similar"][0]["deck"] == "Languages::Spanish"
    assert out["similar_check"].startswith("semantic check skipped")  # the guard in conftest: no index in unit tests


def test_accents_and_case_are_ignored(anki):
    assert "similar" in _add(("RAPIDO", "quick"))["results"][0]


def test_unrelated_word_gets_no_warning(anki):
    assert "similar" not in _add(("zapato", "shoe"))["results"][0]


def test_same_text_in_another_deck_says_same_text(anki):
    other = add_notes([NoteInput(deck="Default", note_type="Basic", fields={"Front": "Socius", "Back": "x"})], dry_run=True)
    assert other["results"][0]["similar"][0]["match"] == "same text"


def test_allow_duplicates_skips_the_check(anki):
    out = _add(("cumbre", "summit"), allow_duplicates=True)
    assert "similar" not in out["results"][0] and "similar_check" not in out


class MeaningEmbedder(FakeEmbedder):
    """Notes with the same second text part (the meaning) get the same vector: stands in for 'close in meaning'."""

    def embed(self, texts):
        out = np.zeros((len(texts), rag.DIM), dtype=np.float32)
        for i, t in enumerate(texts):
            out[i, hash(t.split(" | ")[1]) % rag.DIM] = 1.0
        return out


def test_similar_meaning_is_flagged_from_the_semantic_index(anki, monkeypatch):
    store, emb = MemoryStore(), MeaningEmbedder()
    run(anki, store, emb)
    monkeypatch.setattr(rag, "get_store", lambda: store)
    monkeypatch.setattr(rag, "get_embedder", lambda: emb)
    notes = [NoteInput(**SPANISH, fields={"Word": "el pico", "Meaning": "peak", "WordType": "N", "Gender": "m"}),
             NoteInput(**SPANISH, fields={"Word": "zapato", "Meaning": "shoe", "WordType": "N", "Gender": "m"})]
    out = add_notes(notes, dry_run=True)
    sim = out["results"][0]["similar"]
    assert sim[0]["text"].startswith("la cumbre") and sim[0]["match"].startswith("similar meaning (1.00)")
    assert "similar" not in out["results"][1] and "similar_check" not in out


def test_a_broken_index_never_breaks_add_notes(anki, monkeypatch):
    def boom():
        raise RuntimeError("Redis is on fire\nstack trace")

    monkeypatch.setattr(rag, "get_store", boom)
    out = _add(("zapato", "shoe"))
    assert out["results"][0]["status"] == "valid" and out["similar_check"] == "semantic check skipped: Redis is on fire"
