"""Phase 8.1: pick an embedding model for semantic search by measuring recall@5 and MRR on the real collection.

Read-only (AnkiConnect find/notesInfo only). Everything it writes goes to evals/retrieval/ (gitignored: it quotes real notes).
No LLM calls: the query sets are derived mechanically from the notes.

  uv run --extra rag python scripts/rag_eval.py build              # dump the corpus and build the query sets
  uv run --extra rag python scripts/rag_eval.py score [--models a,b]  # score keyword baseline + embedding models

Query sets:
  near_dup  a note's first field with the article dropped/added, case and accents changed ("la cumbre" -> "Cumbre").
            The question: does the new-card check find the existing note?
  cross     the English side of a bilingual note as the query, the foreign side only as the document.
            A stress test of multilingual alignment, harder than production (where both sides are indexed).
  manual    evals/retrieval/manual.json: [{"query": "...", "expect": "<substring of the expected note's text>"}],
            hand-written paraphrases and synonyms (e.g. "consensus" -> a Raft or Paxos card).
"""

import argparse
import html
import json
import random
import re
import sys
import time
import unicodedata
import urllib.request
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "evals" / "retrieval"
URL = "http://127.0.0.1:8765"
TAG = re.compile(r"<[^>]+>")
BLOCK = re.compile(r"<(?:br|/p|/div|/li|/tr)[^>]*>", re.I)
SOUND = re.compile(r"\[sound:[^\]]*\]")
CLOZE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.S)
ARTICLE = re.compile(r"^(?:\((?:en|et|ei)\)\s*|(?:el|la|los|las|un|una|le|les|des|de|du|the|a|an|en|et|ei)\s+)", re.I)

# Fields holding (foreign, english) text per note type, for the cross-language set. Other types are skipped there.
BILINGUAL = {
    "Basic (and reversed card)": ("Front", "Back"),
    "Spanish": ("Word", "Meaning"),
    "Duo Norwegian (EN-NB)+": ("front", "back"),
}


def invoke(action, **params):
    req = urllib.request.Request(URL, json.dumps({"action": action, "version": 6, "params": params}).encode())
    with urllib.request.urlopen(req, timeout=120) as r:
        body = json.loads(r.read())
    if body["error"]:
        sys.exit(f"AnkiConnect error on {action}: {body['error']}")
    return body["result"]


def plain(value):
    value = SOUND.sub("", value)
    value = CLOZE.sub(r"\1", value)
    text = html.unescape(TAG.sub("", BLOCK.sub("\n", value)))
    return re.sub(r"\s+", " ", text).strip()


def fold(s):
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def build(seed=7, per_set=60):
    ids = invoke("findNotes", query="")  # Yanki notes included: search is read-only
    notes = []
    for i in range(0, len(ids), 1500):
        notes += invoke("notesInfo", notes=ids[i : i + 1500])
    corpus, bilingual = [], []
    for n in notes:
        fields = {k: plain(v["value"]) for k, v in n["fields"].items()}
        text = " | ".join(v for v in fields.values() if v)
        if len(text) < 3:  # image-only or empty notes carry nothing to embed
            continue
        corpus.append({"id": n["noteId"], "type": n["modelName"], "text": text[:600]})
        if (pair := BILINGUAL.get(n["modelName"])) and fields.get(pair[0]) and fields.get(pair[1]):
            bilingual.append((n["noteId"], n["modelName"], fields[pair[0]], fields[pair[1]]))
    rng = random.Random(seed)

    near = []
    for nid, _, foreign, _ in rng.sample(bilingual, min(len(bilingual), 400)):
        stripped = ARTICLE.sub("", foreign).strip()
        variant = stripped if stripped != foreign else None
        if variant is None or len(foreign) > 30:  # words and short phrases; the duplicate check matters most there
            continue
        near.append({"query": fold(variant) if rng.random() < 0.5 else variant.capitalize(), "expect": [nid]})
    near = near[:per_set]

    cross = []
    for nid, _, foreign, english in rng.sample(bilingual, min(len(bilingual), 400)):
        if len(english) > 3 and english.lower() != foreign.lower():
            cross.append({"query": english, "expect": [nid], "doc": foreign})
    cross = cross[:per_set]

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "corpus.json").write_text(json.dumps(corpus, ensure_ascii=False))
    (OUT / "queries.json").write_text(json.dumps({"near_dup": near, "cross": cross}, ensure_ascii=False, indent=1))
    print(f"{len(corpus)} notes in corpus, {len(bilingual)} bilingual; near_dup={len(near)} cross={len(cross)}")
    print(f"written to {OUT}")


def metrics(ranked, expect):
    """ranked: note ids best first. Returns (hit@5, reciprocal rank within top 20)."""
    for rank, nid in enumerate(ranked[:20], 1):
        if nid in expect:
            return (1.0 if rank <= 5 else 0.0), 1.0 / rank
    return 0.0, 0.0


def tokens(s):
    return set(re.findall(r"\w+", fold(s)))


def keyword_rank(query, docs):
    """Baseline mimicking Anki's search: a note matches when it contains every query word (substring); ties keep collection order."""
    words = [w for w in re.findall(r"\w+", fold(query)) if w]
    return [d["id"] for d in docs if words and all(w in fold(d["text"]) for w in words)][:20]


def score(models):
    import numpy as np

    corpus = json.loads((OUT / "corpus.json").read_text())
    sets = json.loads((OUT / "queries.json").read_text())
    manual = OUT / "manual.json"
    if manual.exists():
        resolved = []
        for m in json.loads(manual.read_text()):
            hits = [d["id"] for d in corpus if m["expect"].lower() in d["text"].lower()]
            if hits:
                resolved.append({"query": m["query"], "expect": hits})
            else:
                print(f"manual: no note contains {m['expect']!r}, skipped")
        sets["manual"] = resolved
    ids = [d["id"] for d in corpus]
    by_id = {d["id"]: d for d in corpus}
    report = defaultdict(dict)

    def run(label, rank_fn):
        for name, qs in sets.items():
            if not qs:
                continue
            res = [metrics(rank_fn(name, q), set(q["expect"])) for q in qs]
            report[label][name] = (sum(r[0] for r in res) / len(res), sum(r[1] for r in res) / len(res), len(qs))

    def keyword(name, q):
        docs = corpus
        if name == "cross":  # same handicap as the embeddings: the expected note is searchable by its foreign side only
            docs = [{**d, "text": q["doc"]} if d["id"] in q["expect"] else d for d in corpus]
        return keyword_rank(q["query"], docs)

    run("keyword (Anki-like)", keyword)

    from fastembed import TextEmbedding

    for model_name in models:
        t = time.monotonic()
        model = TextEmbedding(model_name)
        e5 = "e5" in model_name
        # Full-text index: what production would embed. The cross set embeds only the foreign side.
        full = np.array(list(model.embed([("passage: " if e5 else "") + d["text"] for d in corpus])))
        full /= np.linalg.norm(full, axis=1, keepdims=True)
        foreign_cache = {}
        print(f"{model_name}: embedded {len(corpus)} notes in {time.monotonic() - t:.0f}s")

        def rank(name, q, full=full, model=model, e5=e5, cache=foreign_cache):
            qv = np.array(list(model.embed([("query: " if e5 else "") + q["query"]])))[0]
            qv /= np.linalg.norm(qv)
            if name == "cross":
                # replace each expected note's vector with the foreign-side-only vector, keeping every other note as is
                mat = full.copy()
                for nid in q["expect"]:
                    i = ids.index(nid)
                    key = nid
                    if key not in cache:
                        v = np.array(list(model.embed([("passage: " if e5 else "") + q["doc"]])))[0]
                        cache[key] = v / np.linalg.norm(v)
                    mat[i] = cache[key]
                sims = mat @ qv
            else:
                sims = full @ qv
            return [ids[i] for i in np.argsort(-sims)[:20]]

        run(model_name, rank)

    print()
    names = sorted({n for r in report.values() for n in r})
    print(f"{'model':62}" + "".join(f"{n:>22}" for n in names))
    print(f"{'':62}" + "".join(f"{'recall@5 / MRR':>22}" for _ in names))
    for label, r in report.items():
        print(f"{label:62}" + "".join(f"{f'{r[n][0]:.2f} / {r[n][1]:.2f} (n={r[n][2]})':>22}" if n in r else f"{'-':>22}" for n in names))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("command", choices=["build", "score"])
    p.add_argument(
        "--models",
        default="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2,minishlab/potion-multilingual-128M",
        help="comma-separated fastembed model names",
    )
    a = p.parse_args()
    build() if a.command == "build" else score(a.models.split(","))
