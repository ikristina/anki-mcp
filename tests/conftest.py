"""In-memory stand-in for AnkiConnect, so tests run anywhere (CI included) without Anki or network.

It implements only the actions the server uses, and a small subset of Anki search syntax. An unknown action or
search term raises, so a test fails loudly if the server starts relying on something the fake doesn't model.
"""

import fnmatch
import re
import shlex

import pytest

from anki_mcp import server
from anki_mcp.client import AnkiError

MODELS = {
    "Basic": ["Front", "Back"],
    "Basic (and reversed card)": ["Front", "Back", "Audio"],
    "Spanish": ["Word", "Meaning", "WordType", "Gender", "Audio"],
    "Yanki - Basic": ["Front", "Back", "YankiNamespace"],
}
TEMPLATES = {
    "Basic": {"Card 1": [["Front"], ["Back"]]},
    "Basic (and reversed card)": {"Card 1": [["Front", "Audio"], ["Back"]], "Card 2": [["Back"], ["Front", "Audio"]]},
    "Spanish": {"Read": [["Word", "Audio"], ["Meaning"]], "Speak": [["Meaning"], ["Word", "Audio"]]},
    "Yanki - Basic": {"Card 1": [["Front"], ["Back"]]},
}


class FakeAnki:
    def __init__(self):
        self.decks = ["Default", "Go", "DDIA", "DDIA::04_Transactions", "Languages", "Languages::Spanish",
                      "Languages::Latin", "Languages::_Pimsleur"]
        self.notes: dict[int, dict] = {}
        self.media: dict[str, str] = {}
        self.calls: list[str] = []
        self._next_id = 1000
        for word, meaning, wt, g in [("la cumbre", "peak", "N", "f"), ("el perro", "dog", "N", "m"),
                                     ("correr", "to run", "V", ""), ("rápido", "fast", "Adj", ""),
                                     ("la casa", "house", "N", "f"), ("de nada", "you're welcome", "Expr", "")]:
            self.add("Languages::Spanish", "Spanish",
                     {"Word": word, "Meaning": meaning, "WordType": wt, "Gender": g, "Audio": "[sound:hypertts-abc.mp3]"},
                     tags=["Spanish::Duolingo::01_Basics"])
        self.add("Languages::Latin", "Basic (and reversed card)",
                 {"Front": "Puella rosam amat.", "Back": "The girl loves the rose.", "Audio": "[sound:anki-mcp-x.m4a]"}, tags=["latin"])
        self.add("Languages::Latin", "Basic (and reversed card)", {"Front": "Socius", "Back": "Companion", "Audio": ""}, tags=["latin"])
        self.add("Languages::Latin", "Basic (and reversed card)", {"Front": "Marcum <b>excitamus</b>.", "Back": "We wake Marcus.", "Audio": ""}, tags=["latin"])
        self.add("Go", "Yanki - Basic", {"Front": "Channels?", "Back": "Pipes.", "YankiNamespace": "ns"}, lapses=5)
        self.add("DDIA::04_Transactions", "Yanki - Basic", {"Front": "Isolation?", "Back": "I in ACID", "YankiNamespace": "ns"}, lapses=2)
        self.add("Languages::_Pimsleur", "Basic", {"Front": "Hola", "Back": "Hi"})

    # --- helpers -------------------------------------------------------------------------------------------------
    def add(self, deck, model, fields, tags=(), lapses=0):
        nid = self._next_id
        self._next_id += 1
        values = {f: fields.get(f, "") for f in MODELS[model]}
        self.notes[nid] = {"deck": deck, "model": model, "fields": values, "tags": list(tags), "lapses": lapses}
        return nid

    def _info(self, nid):
        n = self.notes[nid]
        return {"noteId": nid, "modelName": n["model"], "tags": n["tags"], "cards": [nid * 10],
                "fields": {f: {"value": v, "order": i} for i, (f, v) in enumerate(n["fields"].items())}}

    def _matches(self, n, term):
        neg = term.startswith("-")
        term = term.lstrip("-")
        if term.startswith("deck:"):
            raw = term[5:]
            wildcard = raw.endswith("::*") and not raw.endswith("\\*")
            name = re.sub(r"\\(.)", r"\1", raw[:-3] if wildcard else raw)
            ok = n["deck"].startswith(name + "::") if wildcard else (n["deck"] == name or n["deck"].startswith(name + "::"))
        elif term.startswith("note:"):
            ok = fnmatch.fnmatchcase(n["model"], term[5:])
        elif term == "prop:lapses>0":
            ok = n["lapses"] > 0
        elif term == "is:suspended":
            ok = False
        else:
            raise AssertionError(f"FakeAnki doesn't understand search term {term!r}")
        return ok != neg

    def _find(self, query):
        terms = shlex.split(query, posix=False)
        terms = [t.replace('"', "") for t in terms]
        return [nid for nid, n in self.notes.items() if all(self._matches(n, t) for t in terms)]

    def _check(self, note):
        if note["deckName"] not in self.decks:
            return "deck was not found"
        first = MODELS[note["modelName"]][0]
        dup = any(n["deck"] == note["deckName"] and n["fields"][first] == note["fields"].get(first) for n in self.notes.values())
        if dup and not note.get("options", {}).get("allowDuplicate"):
            return "cannot create note because it is a duplicate"
        return None

    # --- AnkiConnect actions -------------------------------------------------------------------------------------
    def __call__(self, action, **p):
        self.calls.append(action)
        handler = getattr(self, "a_" + action, None)
        if handler is None:
            raise AssertionError(f"FakeAnki doesn't implement action {action!r}")
        return handler(**p)

    def a_deckNames(self):
        return list(self.decks)

    def a_deckNamesAndIds(self):
        return {d: i + 1 for i, d in enumerate(self.decks)}

    def a_getDeckStats(self, decks):
        ids = self.a_deckNamesAndIds()
        return {str(ids[d]): {"name": d.split("::")[-1], "new_count": 0, "learn_count": 0, "review_count": 0,
                              "total_in_deck": sum(n["deck"] == d for n in self.notes.values())} for d in decks}

    def a_findNotes(self, query):
        return self._find(query)

    def a_findCards(self, query):
        return [nid * 10 for nid in self._find(query)]

    def a_getDecks(self, cards):
        out: dict[str, list[int]] = {}
        for c in cards:
            out.setdefault(self.notes[c // 10]["deck"], []).append(c)
        return out

    def a_notesInfo(self, notes):
        return [self._info(nid) for nid in notes if nid in self.notes]

    def a_cardsInfo(self, cards):
        out = []
        for c in cards:
            n = self.notes[c // 10]
            out.append({"cardId": c, "note": c // 10, "deckName": n["deck"], "lapses": n["lapses"], "factor": 2500,
                        "interval": 3, "fields": self._info(c // 10)["fields"]})
        return out

    def a_modelFieldNames(self, modelName):
        if modelName not in MODELS:
            raise AnkiError(f"model was not found: {modelName}")
        return MODELS[modelName]

    def a_modelFieldsOnTemplates(self, modelName):
        return TEMPLATES[modelName]

    def a_canAddNotesWithErrorDetail(self, notes):
        return [{"canAdd": not (e := self._check(n)), **({"error": e} if e else {})} for n in notes]

    def a_addNotes(self, notes):
        ids = []
        for note in notes:
            fields = dict(note["fields"])
            for a in note.get("audio", []):
                self.media[a["filename"]] = a["data"]
                for f in a["fields"]:
                    fields[f] = fields.get(f, "") + f"[sound:{a['filename']}]"
            ids.append(None if self._check(note) else self.add(note["deckName"], note["modelName"], fields, note["tags"]))
        return ids

    def a_storeMediaFile(self, filename, data):
        self.media[filename] = data
        return filename

    def a_updateNoteFields(self, note):
        self.notes[note["id"]]["fields"].update(note["fields"])
        return None


@pytest.fixture
def anki(monkeypatch):
    fake = FakeAnki()
    monkeypatch.setattr(server, "invoke", fake)
    # Deterministic, offline audio: no gTTS network calls, no eSpeak/afconvert binaries needed.
    monkeypatch.setattr(server, "synthesize", lambda text, voice: (f"anki-mcp-{abs(hash((text, voice)))}.mp3", b"ID3fake"))
    return fake
