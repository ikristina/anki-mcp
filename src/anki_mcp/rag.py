"""Semantic index of the collection (Phase 8 of the roadmap): embeddings in Redis, kept fresh from Anki.

Everything here is optional. Heavy dependencies (`fastembed`, `redis`) are imported lazily and come from the `rag`
extra, so the rest of the server never needs them, and Redis being down must never break an existing tool: only the
functions in this module raise, and they raise `AnkiError` with the fix.

The index is derived data. It can always be rebuilt from Anki (`anki-mcp-index --full`), so Redis persistence only
saves re-embedding time. Changing the embedding model rebuilds it automatically: vectors of different models can't
be compared.

Known limit: Anki bumps a note's modified time when its fields or tags change, not when a card moves to another
deck, so a moved note keeps its old `deck` in the index until the next `--full` run.
"""

import argparse
import re
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol

from anki_mcp.client import AnkiError, invoke
from anki_mcp.telemetry import log, operation, rag_duration

# Chosen by the 8.1 eval (scripts/rag_eval.py): best cross-language recall of the light models; 384 dimensions.
MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384
INDEX = "idx:anki_notes"
KEY_PREFIX = "anki:note:"
META_KEY = "anki:index:meta"
_BUILD_HINT = "Build or refresh the index with `uv run --extra rag anki-mcp-index` (Anki must be running)."
SIMILAR_THRESHOLD = 0.8  # starting value: a paraphrase-level match on MiniLM; related-but-different notes sit at 0.5-0.75
TEXT_CHARS = 600  # the model reads ~128 tokens anyway; this bounds what is stored in Redis

_SOUND_RE = re.compile(r"\[sound:[^\]]*\]")
_ALT_RE = re.compile(r'<span class="alt">.*?</span>', re.DOTALL)
_CLOZE_RE = re.compile(r"\{\{c\d+::(.*?)(?:::[^}]*)?\}\}", re.DOTALL)


def note_text(note: dict) -> str:
    """The text to embed: plain field text, with [sound:] tags, alternatives and cloze markup removed."""
    from anki_mcp.server import _plain  # lazy: server imports this package's siblings

    parts = []
    for _, f in sorted(note["fields"].items(), key=lambda kv: kv[1]["order"]):
        value = _CLOZE_RE.sub(r"\1", _ALT_RE.sub("", _SOUND_RE.sub("", f["value"])))
        if text := _plain(value, None):
            parts.append(text)
    return " | ".join(parts)[:TEXT_CHARS]


_LABEL_RE = re.compile(r"\([^)]*\)")
_ARTICLE_RE = re.compile(r"^(?:el|la|los|las|un|una|le|les|l'|des|du|the|a|an|en|et|ei|ett|der|die|das)\s+", re.IGNORECASE)


def core_text(value: str) -> str:
    """A first field reduced to its word: no HTML, bracketed notes like '(Spain: gambas)', article, case, accents or
    punctuation. 'la Cumbre', 'cumbre' and 'La cumbre (peak)' all give 'cumbre'."""
    import unicodedata

    from anki_mcp.server import _plain  # lazy: see note_text

    text = _LABEL_RE.sub(" ", _plain(_SOUND_RE.sub("", value), None)).strip()
    text = "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")
    text = re.sub(r"[^\w\s]", " ", text)
    text = _ARTICLE_RE.sub("", re.sub(r"\s+", " ", text).strip())
    return text.strip()


def lexical_matches(call: Callable[..., Any], first_field: str, limit: int = 3) -> list[dict]:
    """Existing notes (any deck) whose first field is the same word apart from article, case, accents or a bracketed
    note. Needs only Anki, so it works without the semantic index. This is what embeddings do worst: 'cumbre' vs
    'la cumbre' (8.1 eval: keyword recall@5 0.97, MiniLM 0.55)."""
    core = core_text(first_field)
    if len(core) < 2:
        return []
    ids = call("findNotes", query=f'"nc:{core}"')[:30]  # `nc:` = substring of any field, ignoring case and accents
    if not ids:
        return []
    notes = call("notesInfo", notes=ids)
    decks = _card_decks(call, [n["cards"][0] for n in notes if n.get("cards")])
    out = []
    for n in notes:
        first = min(n["fields"].values(), key=lambda f: f["order"])["value"]
        if core_text(first) == core:
            exact = first.strip() == first_field.strip()
            out.append({"note_id": n["noteId"], "deck": decks.get(n["cards"][0], "?") if n.get("cards") else "?",
                        "text": note_text(n)[:120],
                        "match": "same text" if exact else "same word apart from article, case, accents or a bracketed note"})
    return out[:limit]


class Store(Protocol):
    def ensure_index(self, model: str, dim: int) -> bool:
        """Create the index if missing. Return True when it was (re)built empty, e.g. because the model changed."""

    def clear(self) -> None: ...
    def mods(self) -> dict[int, int]: ...
    def upsert(self, items: list[dict]) -> None: ...
    def delete(self, ids: Iterable[int]) -> None: ...
    def finish(self, model: str, dim: int) -> None: ...


class Embedder:
    """fastembed (ONNX) wrapper that returns L2-normalised float32 rows, so cosine similarity is a dot product."""

    def __init__(self, model: str = MODEL):
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise AnkiError("Semantic search needs the 'rag' extra: run `uv sync --extra rag` (or `uv run --extra rag ...`).") from e
        self.model_name = model
        self._model = TextEmbedding(model)

    def embed(self, texts: list[str]):
        import numpy as np

        vecs = np.array(list(self._model.embed(texts, batch_size=64)), dtype=np.float32)
        return vecs / np.linalg.norm(vecs, axis=1, keepdims=True)


def _escape_tag(value: str) -> str:
    """Escape ASCII punctuation and spaces for a Redis TAG query: deck names look like 'Languages::🇪🇸 Spanish'.

    Non-ASCII characters (accents, emoji) must stay unescaped: a backslash before one is a syntax error.
    """
    return "".join(f"\\{c}" if c.isascii() and not (c.isalnum() or c == "_") else c for c in value)


class RedisStore:
    def __init__(self, url: str | None = None, index: str = INDEX, prefix: str = KEY_PREFIX, meta_key: str = META_KEY):
        import os

        try:
            import redis
        except ImportError as e:
            raise AnkiError("Semantic search needs the 'rag' extra: run `uv sync --extra rag` (or `uv run --extra rag ...`).") from e
        self.url = url or os.environ.get("ANKI_MCP_REDIS_URL", "redis://127.0.0.1:6379")
        self.index, self.prefix, self.meta_key = index, prefix, meta_key
        self.r = redis.Redis.from_url(self.url)
        try:
            self.r.ping()
        except redis.RedisError as e:
            raise AnkiError(
                f"Cannot reach Redis at {self.url} ({e}). Start it with `docker start anki-redis` "
                "(if Docker isn't running: `colima start`), or set ANKI_MCP_REDIS_URL."
            ) from e

    def _index_exists(self) -> bool:
        return self.index.encode() in self.r.execute_command("FT._LIST")

    def clear(self) -> None:
        if self._index_exists():
            self.r.ft(self.index).dropindex(delete_documents=True)
        self.r.delete(self.meta_key)

    def ensure_index(self, model: str, dim: int) -> bool:
        from redis.commands.search.field import NumericField, TagField, TextField, VectorField
        from redis.commands.search.index_definition import IndexDefinition, IndexType

        meta = {k.decode(): v.decode() for k, v in self.r.hgetall(self.meta_key).items()}
        rebuilt = False
        if self._index_exists() and meta and (meta.get("model"), meta.get("dim")) != (model, str(dim)):
            log.warning("embedding model changed (%s -> %s): rebuilding the index", meta.get("model"), model)
            self.clear()
            rebuilt = True
        if not self._index_exists():
            self.r.ft(self.index).create_index(
                [
                    VectorField("vector", "HNSW", {"TYPE": "FLOAT32", "DIM": dim, "DISTANCE_METRIC": "COSINE"}),
                    TextField("text"),
                    TagField("deck", separator="|"),
                    TagField("note_type", separator="|"),
                    NumericField("mod"),
                ],
                definition=IndexDefinition(prefix=[self.prefix], index_type=IndexType.HASH),
            )
            rebuilt = True
        if not meta or rebuilt:
            # Record the model now, not only at the end: a run interrupted midway must resume, not look like a model change.
            self.r.hset(self.meta_key, mapping={"model": model, "dim": dim})
        return rebuilt

    def mods(self) -> dict[int, int]:
        keys = list(self.r.scan_iter(match=f"{self.prefix}*", count=1000))
        out: dict[int, int] = {}
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            pipe = self.r.pipeline(transaction=False)
            for k in chunk:
                pipe.hget(k, "mod")
            for k, mod in zip(chunk, pipe.execute()):
                if mod is not None:
                    out[int(k.decode().removeprefix(self.prefix))] = int(mod)
        return out

    def upsert(self, items: list[dict]) -> None:
        pipe = self.r.pipeline(transaction=False)
        for it in items:
            pipe.hset(f"{self.prefix}{it['id']}", mapping={
                "vector": it["vector"].tobytes(), "text": it["text"], "deck": it["deck"],
                "note_type": it["note_type"], "mod": it["mod"],
            })
        pipe.execute()

    def delete(self, ids: Iterable[int]) -> None:
        keys = [f"{self.prefix}{i}" for i in ids]
        for i in range(0, len(keys), 1000):
            self.r.delete(*keys[i : i + 1000])

    def finish(self, model: str, dim: int) -> None:
        self.r.hset(self.meta_key, mapping={"model": model, "dim": dim, "indexed_at": int(time.time())})

    def info(self) -> dict:
        """Index size and when it was last fully brought up to date ({} fields missing before the first run)."""
        import redis

        try:
            n = int(self.r.ft(self.index).info()["num_docs"]) if self._index_exists() else 0
        except redis.RedisError as e:
            raise AnkiError(f"Redis search index unavailable ({e}). {_BUILD_HINT}") from e
        meta = {k.decode(): v.decode() for k, v in self.r.hgetall(self.meta_key).items()}
        return {"notes": n, "model": meta.get("model"), "indexed_at": int(meta["indexed_at"]) if "indexed_at" in meta else None}

    def search(self, vector, k: int = 5, deck: str | None = None) -> list[dict]:
        """Nearest notes by cosine similarity; `deck` limits to that deck and its subdecks."""
        import redis
        from redis.commands.search.query import Query

        flt = "*"
        if deck:
            d = _escape_tag(deck)
            flt = f"@deck:{{{d}|{d}\\:\\:*}}"
        q = (Query(f"({flt})=>[KNN {k} @vector $v AS dist]").sort_by("dist")
             .return_fields("dist", "text", "deck", "note_type").dialect(2).paging(0, k))
        try:
            res = self.r.ft(self.index).search(q, query_params={"v": vector.tobytes()})
        except redis.RedisError as e:
            raise AnkiError(f"Semantic search failed ({e}). {_BUILD_HINT}") from e
        return [{"note_id": int(d.id.removeprefix(self.prefix)), "similarity": round(1 - float(d.dist), 4),
                 "deck": d.deck, "note_type": d.note_type, "text": d.text} for d in res.docs]


def _card_decks(call: Callable[..., Any], card_ids: list[int]) -> dict[int, str]:
    """card id -> deck name. AnkiConnect's getDecks fails the whole batch on one missing card (seen on the real
    collection: a note whose first card id no longer exists), so fall back to one card at a time and skip the bad ones."""
    if not card_ids:
        return {}
    try:
        return {c: d for d, cs in call("getDecks", cards=card_ids).items() for c in cs}
    except AnkiError:
        out: dict[int, str] = {}
        for cid in card_ids:
            try:
                out.update({c: d for d, cs in call("getDecks", cards=[cid]).items() for c in cs})
            except AnkiError:
                log.warning("card %s not found; its note is indexed with deck '?'", cid)
        return out


def sync_index(
    store: Store,
    embedder: Embedder | Any,
    call: Callable[..., Any] = invoke,
    full: bool = False,
    batch: int = 200,
    limit: int | None = None,
    progress: Callable[[str], None] = lambda _: None,
) -> dict:
    """Bring the index in line with Anki: embed new and changed notes, drop deleted and now-empty ones.

    `limit` caps how many notes are embedded in this run (the rest are picked up by the next run).
    """
    with operation("rag index", rag_duration, {"rag.op": "index"}):
        start = time.monotonic()
        if full:
            store.clear()
        store.ensure_index(embedder.model_name, DIM)
        ids = call("findNotes", query="")
        current = {m["noteId"]: m["mod"] for m in call("notesModTime", notes=ids)} if ids else {}
        known = store.mods()
        gone = [i for i in known if i not in current]
        pending = [i for i, mod in current.items() if known.get(i) != mod]
        todo = pending[:limit] if limit is not None else pending
        store.delete(gone)
        indexed = empty = 0
        for i in range(0, len(todo), batch):
            notes = call("notesInfo", notes=todo[i : i + batch])
            first_card = {n["noteId"]: n["cards"][0] for n in notes if n.get("cards")}
            decks = _card_decks(call, list(first_card.values()))
            items, drop = [], []
            for n in notes:
                text = note_text(n)
                if not text:
                    drop.append(n["noteId"])  # image-only or blank: nothing to embed
                    continue
                items.append({"id": n["noteId"], "text": text, "note_type": n["modelName"], "mod": current[n["noteId"]],
                              "deck": decks.get(first_card.get(n["noteId"]), "?")})
            if items:
                vecs = embedder.embed([it["text"] for it in items])
                for it, v in zip(items, vecs):
                    it["vector"] = v
                store.upsert(items)
            store.delete(drop)
            indexed += len(items)
            empty += len(drop)
            progress(f"{min(i + batch, len(todo))}/{len(todo)} notes")
        store.finish(embedder.model_name, DIM)
        result = {"indexed": indexed, "removed": len(gone), "skipped_empty": empty,
                  "unchanged": len(current) - len(pending), "remaining": len(pending) - len(todo),
                  "seconds": round(time.monotonic() - start, 1)}
        log.info("indexed %s", result)
        return result


_cache: dict[str, Any] = {}


def get_store() -> RedisStore:
    """One connection per process, checked for liveness each call so a Redis restart is noticed with a clear error."""
    store = _cache.get("store")
    if store is None:
        store = _cache["store"] = RedisStore()
    else:
        try:
            store.r.ping()
        except Exception:
            _cache.pop("store", None)
            return get_store()
    return store


def get_embedder() -> Embedder:
    """The model takes seconds to load, so keep it for the life of the server."""
    if "embedder" not in _cache:
        _cache["embedder"] = Embedder()
    return _cache["embedder"]


def find_similar(text: str, k: int = 5, deck: str | None = None, exclude_id: int | None = None,
                 store: Store | Any = None, embedder: Embedder | Any = None) -> dict:
    """Notes most similar in meaning to `text`, best first. Raises AnkiError when the index is missing or empty."""
    with operation("rag search", rag_duration, {"rag.op": "search"}):
        store = store or get_store()
        info = store.info()
        if not info["notes"]:
            raise AnkiError(f"The semantic index is empty. {_BUILD_HINT}")
        embedder = embedder or get_embedder()
        hits = store.search(embedder.embed([text])[0], k + (1 if exclude_id else 0), deck)
        hits = [h for h in hits if h["note_id"] != exclude_id][:k]
        return {"results": hits, "index": info}


def main() -> None:
    p = argparse.ArgumentParser(description="Index the Anki collection into Redis for semantic search (needs Anki running).")
    p.add_argument("--full", action="store_true", help="drop the index and rebuild everything")
    p.add_argument("--limit", type=int, help="embed at most this many notes in this run")
    a = p.parse_args()
    try:
        store = RedisStore()
        print(f"loading {MODEL} ...", file=sys.stderr)
        result = sync_index(store, Embedder(), full=a.full, limit=a.limit, progress=lambda s: print(s, file=sys.stderr))
    except AnkiError as e:
        sys.exit(str(e))
    print(result)


if __name__ == "__main__":
    main()
