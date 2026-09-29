"""Generate realistic tool traffic with OpenTelemetry export on, so Grafana has something to show.

Read-only: writes run only as dry runs (enforced below); sync and set_deck_profile are never called.
Needs Anki running and an OTLP backend (see docs/observability.md).

Run: uv run --extra otel python scripts/otel_traffic.py [--calls 60] [--pause 2]
"""

import argparse
import asyncio
import json
import os
import random
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

WRITES = {"add_notes", "add_audio", "update_notes"}
NEVER = {"sync", "set_deck_profile"}


def items(result):
    return [json.loads(c.text) for c in result.content]


class Traffic:
    def __init__(self, session):
        self.s = session
        self.decks: list[str] = []
        self.stats: dict[str, list[int]] = {}

    async def call(self, tool, args):
        assert tool not in NEVER, tool
        if tool in WRITES:
            assert args.get("dry_run") is True, f"{tool} must be a dry run"
        r = await self.s.call_tool(tool, args)
        ok, err = self.stats.setdefault(tool, [0, 0])
        self.stats[tool] = [ok + (not r.is_error), err + r.is_error]
        print(f"  {tool:16} {'ERROR' if r.is_error else 'ok'}")
        return None if r.is_error else items(r)

    def deck(self):
        return random.choice(self.decks)

    async def sample_note(self, deck):
        found = await self.call("search_notes", {"query": f'"deck:{deck}"', "limit": 20})
        notes = found[0]["notes"] if found else []
        return random.choice(notes) if notes else None

    # Scenarios: each is roughly one thing an agent would do.
    async def browse(self):
        await self.call("list_decks", {})

    async def inspect_deck(self):
        deck = self.deck()
        await self.call("get_deck_profile", {"deck": deck})
        await self.call("describe_deck", {"deck": deck, "sample_size": random.choice([50, 200, 500])})

    async def read_notes(self):
        if note := await self.sample_note(self.deck()):
            await self.call("get_notes", {"note_ids": [note["note_id"]], "raw": random.random() < 0.5})

    async def weak_cards(self):
        await self.call("get_weak_cards", {"deck": self.deck(), "limit": random.choice([5, 15, 30])})

    async def note_types(self):
        await self.call("list_note_types", {})

    async def dry_add(self):
        deck = self.deck()
        if note := await self.sample_note(deck):
            fields = {f: f"otel test {random.randint(1, 10**6)}" for f in note["fields"]}
            await self.call("add_notes", {"dry_run": True, "notes": [{"deck": deck, "note_type": note["note_type"], "fields": fields}]})

    async def dry_update(self):
        if note := await self.sample_note(self.deck()):
            (full,) = await self.call("get_notes", {"note_ids": [note["note_id"]], "raw": True}) or [None]
            if full:
                first = next(iter(full["fields"]))
                await self.call("update_notes", {"dry_run": True, "updates": [
                    {"note_id": full["note_id"], "fields": {first: full["fields"][first] + " (edited)"}}]})

    async def dry_audio(self):
        await self.call("add_audio", {"dry_run": True, "query": f'"deck:{self.deck()}"', "text_field": "Word",
                                      "audio_field": "Audio", "voice": "es-MX", "limit": 5})

    async def mistakes(self):
        """Things agents get wrong: misspelled decks, bad ids. These produce error spans and logs."""
        await random.choice([
            lambda: self.call("describe_deck", {"deck": random.choice(["Spansh", "Languages::Frnch", "latin"])}),
            lambda: self.call("get_weak_cards", {"deck": "Nonexistent::Deck"}),
            lambda: self.call("add_notes", {"dry_run": True, "notes": [
                {"deck": self.deck(), "note_type": "No Such Type", "fields": {"Front": "x"}}]}),
        ])()


SCENARIOS = [  # (weight, method name)
    (4, "browse"), (5, "inspect_deck"), (6, "read_notes"), (4, "weak_cards"), (2, "note_types"),
    (3, "dry_add"), (2, "dry_update"), (2, "dry_audio"), (2, "mistakes"),
]


async def main(calls: int, pause: float):
    env = {
        **os.environ,
        "OTEL_EXPORTER_OTLP_ENDPOINT": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318"),
        "OTEL_METRIC_EXPORT_INTERVAL": "5000",
    }
    params = StdioServerParameters(command="uv", args=["run", "--extra", "otel", "anki-mcp"], env=env)
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        t = Traffic(s)
        t.decks = [d["deck"] for d in await t.call("list_decks", {}) if d["own_cards"]]
        weights, names = zip(*SCENARIOS)
        start = time.monotonic()
        for i in range(calls):
            name = random.choices(names, weights)[0]
            print(f"[{i + 1}/{calls}] {name}")
            await getattr(t, name)()
            await asyncio.sleep(random.uniform(pause / 2, pause * 1.5))
        print(f"\n{sum(map(sum, t.stats.values()))} tool calls in {time.monotonic() - start:.0f}s")
        for tool, (ok, err) in sorted(t.stats.items()):
            print(f"  {tool:16} ok={ok} error={err}")
        await asyncio.sleep(6)  # let one more metric export happen before the server is stopped


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--calls", type=int, default=60, help="scenarios to run (each makes 1-3 tool calls)")
    p.add_argument("--pause", type=float, default=2.0, help="average seconds between scenarios")
    a = p.parse_args()
    asyncio.run(main(a.calls, a.pause))
