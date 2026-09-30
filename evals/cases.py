"""Eval cases: a prompt plus named checks over the Run parsed from `claude -p` (see run.py).

The fake collection is tests/conftest.py's FakeAnki. Profiles are evals/profiles.json. The Obsidian stub is
evals/fake_obsidian.py (seeded with 03 resources/Anki/Go/nil-channel-send.md). A check is a function Run -> bool; it may
raise on unexpected shapes, which counts as a failure.
"""

import ast
import json
import re

GO_NOTE = "03 resources/Anki/Go/nil-channel-send.md"


# --- helpers -----------------------------------------------------------------------------------------------------

def adds(run, dry=None):
    """add_notes calls, optionally only dry (True) or real (False) ones. dry_run defaults to False in the server."""
    return [c for c in run.tool("add_notes", "anki") if dry is None or bool(c.input.get("dry_run", False)) == dry]


def added(run):
    """Notes that the server reports as actually added (status 'added'), across all real add_notes calls, with the
    effective note_type and tags filled in."""
    out = []
    for c in adds(run, dry=False):
        if c.is_error:
            continue
        results = json.loads(c.result)["results"]
        for r in results:
            if r.get("status") == "added":  # an omitted note_type is resolved by the server from the deck profile
                n = c.input["notes"][r["index"]]
                out.append({**n, "note_type": n.get("note_type") or r.get("note_type", "Basic").removesuffix(" (from deck profile)"),
                            "tags": n.get("tags") or ast.literal_eval(r.get("tags", "[]").removesuffix(" (from deck profile)"))})
    return out


def index(run, pred):
    return next((i for i, c in enumerate(run.calls) if pred(c)), None)


def by_word(notes, word, field="Word"):
    hits = [n for n in notes if word in n["fields"].get(field, "").lower()]
    assert len(hits) == 1, f"{len(hits)} notes with {word!r} in {field}"
    return hits[0]


def creates(run):
    return run.tool("obsidian_create_note", "obsidian")


def text_has(run, *patterns):
    return all(re.search(p, run.text, re.I) for p in patterns)


# --- shared check sets -------------------------------------------------------------------------------------------

def dry_run_first(run):
    first_dry = index(run, lambda c: c.name == "add_notes" and c.input.get("dry_run"))
    first_real = index(run, lambda c: c.name == "add_notes" and not c.input.get("dry_run"))
    return first_dry is not None and (first_real is None or first_dry < first_real)


def one_real_add(run):
    return len(adds(run, dry=False)) == 1


def spanish_notes_ok(n_expected):
    def deck_and_type(run):
        notes = added(run)
        return len(notes) == n_expected and all(
            n["deck"] == "Languages::Spanish" and n["note_type"] == "Spanish" for n in notes)

    def audio_es_mx(run):
        return all((n.get("audio") or {}).get("field") == "Audio" and n["audio"].get("voice") == "es-MX"
                   for n in added(run))

    def profile_tag(run):
        return all("Spanish::Added" in n.get("tags", []) for n in added(run))

    return [("deck + note type", deck_and_type), ("audio Audio/es-MX", audio_es_mx), ("tag Spanish::Added", profile_tag)]


def yanki_checks(prefix, deck=None):
    """Card routed to Obsidian under `prefix` (None: any new folder under 03 resources/Anki/), in Yanki format."""
    def no_add_notes(run):
        return not adds(run) if deck is None else not [c for c in adds(run)
                                                        if any(n.get("deck") == deck for n in c.input.get("notes", []))]

    def list_decks_first(run):
        ld = index(run, lambda c: c.name == "list_decks")
        write = index(run, lambda c: c.name in ("add_notes", "obsidian_create_note"))
        return ld is not None and (write is None or ld < write)

    def one_create(run):
        return len(creates(run)) == 1

    def vault_personal(run):
        return all(c.input.get("vault") == "personal" for c in creates(run))

    def path_ok(run):
        if not creates(run):
            return False
        path = creates(run)[0].input["path"]
        folder, _, filename = path.rpartition("/")
        if prefix:
            in_place = folder == prefix.rstrip("/")
        else:  # a new folder: not one of the existing Yanki decks
            in_place = re.fullmatch(r"03 resources/Anki/[^/]+(/[^/]+)*", folder) is not None and not re.match(
                r"03 resources/Anki/(Go|DDIA)(/|$)", folder)
        stem = filename.removesuffix(".md")
        return in_place and filename.endswith(".md") and len(stem) <= 60 and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", stem)

    def yanki_format(run):
        if not creates(run):
            return False
        content = creates(run)[0].input.get("content", "")
        parts = re.split(r"(?m)^---[ \t]*$", content)
        return (len(parts) >= 2 and parts[0].strip() != "" and "---".join(parts[1:]).strip() != ""
                and "noteid" not in content.lower())

    def searched_first(run):
        s = index(run, lambda c: c.name == "obsidian_search_vault")
        w = index(run, lambda c: c.name == "obsidian_create_note")
        return s is not None and w is not None and s < w

    def sync_reminder(run):
        return text_has(run, r"yanki[^.\n]{0,20}sync")

    checks = [("no add_notes" + (f" to {deck}" if deck else ""), no_add_notes), ("list_decks before writing", list_decks_first),
              ("one create_note", one_create), ("vault personal", vault_personal), ("path + filename", path_ok),
              ("front/---/back, no noteId", yanki_format), ("searched vault first", searched_first),
              ("tells user to run Yanki sync", sync_reminder)]
    if prefix is None:
        checks.append(("tells user to add folder to Yanki", lambda run: text_has(run, r"yanki", r"folder")))
    return checks


# --- cases -------------------------------------------------------------------------------------------------------

GO_AHEAD = " Add them, no need to confirm with me."

CASES = [
    # language-cards --------------------------------------------------------------------------------------------
    {"id": "es-batch", "skill": "language-cards",
     "prompt": "/language-cards Spanish: ventana (window), madrugada (early morning), mapa (map), bailar (to dance), "
               "sin embargo (however)." + GO_AHEAD,
     "checks": [
         ("dry run before real add", dry_run_first),
         ("exactly 1 real add_notes", one_real_add),
         *spanish_notes_ok(5),
         ("articles on nouns", lambda r: [by_word(added(r), w)["fields"]["Word"].strip().lower() for w in
                                          ("ventana", "madrugada", "mapa")] == ["la ventana", "la madrugada", "el mapa"]),
         ("gender codes", lambda r: [by_word(added(r), w)["fields"].get("Gender", "") for w in
                                     ("ventana", "madrugada", "mapa", "bailar", "sin embargo")] == ["f", "f", "m", "", ""]),
         ("WordType codes", lambda r: [by_word(added(r), w)["fields"].get("WordType") for w in ("ventana", "bailar")]
          == ["N", "V"] and by_word(added(r), "sin embargo")["fields"].get("WordType") in ("Expr", "Conj", "Adv")),
     ]},
    {"id": "es-dup", "skill": "language-cards",
     "prompt": "/language-cards Spanish: cumbre (peak), ventana (window), bailar (to dance)." + GO_AHEAD,
     "checks": [
         ("dry run before real add", dry_run_first),
         ("exactly 1 real add_notes", one_real_add),
         *spanish_notes_ok(2),
         ("duplicate left out", lambda r: not any("cumbre" in n["fields"].get("Word", "") for n in added(r))),
         ("tells user it exists", lambda r: text_has(r, r"cumbre", r"already|duplicate|exist")),
     ]},
    {"id": "es-natural", "skill": "language-cards",
     "prompt": "I just learned two Spanish words: ventana (window) and mapa (map). Save them to my Spanish deck." + GO_AHEAD,
     "checks": [
         ("language-cards skill used", lambda r: any(c.input.get("skill") == "language-cards" for c in r.tool("Skill"))),
         ("exactly 1 real add_notes", one_real_add),
         *spanish_notes_ok(2),
         ("articles on nouns", lambda r: sorted(n["fields"]["Word"].strip().lower() for n in added(r)) == ["el mapa", "la ventana"]),
     ]},
    {"id": "es-ambiguous", "skill": "language-cards",
     "prompt": "/language-cards Spanish: cometa",
     "checks": [
         # el cometa = comet, la cometa = kite. Asking is right; adding both genders as two notes is also fine.
         ("asks, or adds both genders", lambda r: (not adds(r, dry=False) and text_has(r, r"comet", r"kite")) or
          sorted(n["fields"]["Word"].strip().lower() for n in added(r)) == ["el cometa", "la cometa"]),
     ]},
    {"id": "es-dry-only", "skill": "language-cards",
     "prompt": "/language-cards Spanish: ventana (window), mapa (map), bailar (to dance). Just show me what you would "
               "add. Don't add anything yet.",
     "checks": [
         ("dry run done", lambda r: bool(adds(r, dry=True))),
         ("nothing written", lambda r: not adds(r, dry=False) and not [c for c in r.tool("add_audio", "anki")
                                                                        if c.input.get("dry_run") is False]),
     ]},
    {"id": "la-profile", "skill": "language-cards",
     "prompt": "/language-cards Latin: Puer canem videt. (The boy sees the dog.)" + GO_AHEAD.replace("them", "it"),
     "checks": [
         ("exactly 1 real add_notes", one_real_add),
         ("deck + note type", lambda r: [(n["deck"], n["note_type"]) for n in added(r)]
          == [("Languages::Latin", "Basic (and reversed card)")]),
         ("Front Latin, Back English", lambda r: "puer canem videt" in added(r)[0]["fields"]["Front"].lower()
          and "boy" in added(r)[0]["fields"].get("Back", "").lower()),
         ("voice espeak:la into Audio", lambda r: added(r)[0]["audio"]["voice"] == "espeak:la" and added(r)[0]["audio"]["field"] == "Audio"),
         ("audio.text plain Latin", lambda r: "puer canem videt" in (added(r)[0]["audio"].get("text") or "").lower()
          and "<" not in added(r)[0]["audio"]["text"]),
         ("tag latin", lambda r: "latin" in added(r)[0].get("tags", [])),
         ("no [sound: in Front", lambda r: "[sound:" not in added(r)[0]["fields"]["Front"]),
     ]},
    {"id": "la-voice-trap", "skill": "language-cards",
     "prompt": "/language-cards Latin: Canis currit. (The dog runs.) Use Google's Latin voice (voice 'la') for the "
               "audio." + GO_AHEAD.replace("them", "it"),
     "checks": [
         ("nothing added with Google la", lambda r: not [n for n in added(r)
                                                         if (n.get("audio") or {}).get("voice", "").split("-")[0] == "la"]),
         ("explains / uses espeak", lambda r: text_has(r, r"espeak")),
     ]},
    # flashcards -------------------------------------------------------------------------------------------------
    {"id": "yanki-go", "skill": "flashcards",
     "prompt": "Make a flashcard for my Go deck: what does `select {}` with no cases do? (It blocks forever.)",
     "checks": yanki_checks("03 resources/Anki/Go", deck="Go")},
    {"id": "yanki-subdeck", "skill": "flashcards",
     "prompt": "Add a flashcard to my DDIA::04_Transactions deck: what is write skew? (Two transactions read the same "
               "data, then each updates a different object based on what it read, so together they break an invariant "
               "that each alone would keep.)",
     "checks": yanki_checks("03 resources/Anki/DDIA/04_Transactions", deck="DDIA::04_Transactions")},
    {"id": "tech-no-deck", "skill": "flashcards",
     "prompt": "Make a flashcard about Kafka consumer groups: within one consumer group, each partition is consumed "
               "by exactly one consumer.",
     "checks": yanki_checks(None)},
    {"id": "yanki-dup", "skill": "flashcards",
     "prompt": "Make a flashcard for my Go deck: what happens when you send on a nil channel? (It blocks forever.)",
     "checks": [
         ("search found the seeded note", lambda r: any(GO_NOTE in c.result for c in r.tool("obsidian_search_vault", "obsidian"))),
         ("no create_note", lambda r: not creates(r)),
         ("no add_notes", lambda r: not adds(r)),
         ("tells user it exists", lambda r: text_has(r, r"already|exist")),
     ]},
    {"id": "anki-basic", "skill": "flashcards",
     "prompt": "Add a flashcard to my Default deck: What is the capital of Australia? Canberra.",
     "checks": [
         ("exactly 1 real add_notes", one_real_add),
         ("Basic in Default", lambda r: [(n["deck"], n.get("note_type", "Basic")) for n in added(r)] == [("Default", "Basic")]),
         ("Front/Back", lambda r: "australia" in added(r)[0]["fields"]["Front"].lower()
          and "canberra" in added(r)[0]["fields"]["Back"].lower()),
         ("not sent to Obsidian", lambda r: not creates(r)),
     ]},
    {"id": "anki-batch", "skill": "flashcards",
     "prompt": "Add these 3 cards to my Default deck: capital of Australia -> Canberra; capital of Canada -> Ottawa; "
               "capital of New Zealand -> Wellington.",
     "checks": [
         ("exactly 1 real add_notes", one_real_add),
         ("all 3 added to Default", lambda r: len(added(r)) == 3 and all(n["deck"] == "Default" for n in added(r))),
         ("not sent to Obsidian", lambda r: not creates(r)),
     ]},
    {"id": "vocab-via-flashcards", "skill": "flashcards",
     "prompt": "Make flashcards for these Spanish words: el río (river), la montaña (mountain)." + GO_AHEAD,
     "checks": [
         ("handed to language-cards", lambda r: any(c.input.get("skill") == "language-cards" for c in r.tool("Skill"))
          or (added(r) and all(n.get("audio") for n in added(r)))),
         ("exactly 1 real add_notes", one_real_add),
         *spanish_notes_ok(2),
     ]},
]
