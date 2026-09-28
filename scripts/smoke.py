"""Talk to the server over stdio exactly like Claude Code does, and check the answers.

Read-only: add_notes runs with dry_run. Exits non-zero if any check fails.
Run: uv run python scripts/smoke.py
"""

import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

failures = []


def check(label, ok, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}{f'  ({detail})' if detail else ''}")
    if not ok:
        failures.append(label)


def items(result):
    """A list result arrives as one content block per item; a dict as a single block."""
    return [json.loads(c.text) for c in result.content]


async def main():
    params = StdioServerParameters(command="uv", args=["run", "anki-mcp"])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
        await s.initialize()
        names = {t.name for t in (await s.list_tools()).tools}
        expected = {"list_decks", "describe_deck", "search_notes", "get_notes", "get_weak_cards", "list_note_types", "add_notes", "add_audio", "update_notes", "get_deck_profile", "set_deck_profile"}
        check("all tools registered", names == expected, ", ".join(sorted(names)))

        decks = items(await s.call_tool("list_decks", {}))
        prof = items(await s.call_tool("get_deck_profile", {"deck": "Nonexistent deck"}))[0]
        check("get_deck_profile explains a missing profile", prof["profile"] is None and "describe_deck" in prof["hint"])
        by_name = {d["deck"]: d for d in decks}
        check("list_decks returns every deck", len(decks) > 1, f"{len(decks)} decks")
        check("Yanki deck labeled", by_name.get("Go", {}).get("source") == "yanki", "Go")
        check("native deck labeled", by_name.get("Languages::🇪🇸 Spanish", {}).get("source") == "anki", "Spanish")

        desc = items(await s.call_tool("describe_deck", {"deck": "Languages::🇪🇸 Spanish", "sample_size": 100}))[0]
        spanish_type = next((nt for nt in desc.get("note_types", []) if nt["note_type"] == "Spanish"), {})
        fields = spanish_type.get("fields", {})
        check("describe_deck finds the audio field", "audio" in fields.get("Audio", {}), str(fields.get("Audio", {}).get("audio")))
        check("describe_deck lists code values", "N" in fields.get("WordType", {}).get("values", {}), str(fields.get("WordType", {}).get("values")))
        check("describe_deck reads templates", "Read" in spanish_type.get("templates", {}), ", ".join(spanish_type.get("templates", {})))
        typo = await s.call_tool("describe_deck", {"deck": "Spansh"})
        check("describe_deck suggests on typo", typo.is_error and "Spanish" in typo.content[0].text)

        nts = items(await s.call_tool("list_note_types", {"name": "Spanish"}))
        sp = next((t for t in nts if t["note_type"] == "Spanish"), {})
        check("list_note_types filters and counts", "WordType" in sp.get("fields", []) and sp.get("notes", 0) > 0, f"{len(nts)} types, Spanish: {sp.get('notes')} notes")

        page = items(await s.call_tool("search_notes", {"query": "deck:Go", "limit": 3}))[0]
        check("search_notes paginates", page["returned"] == 3 and page["total"] >= 3, f"{page['returned']} of {page['total']}")

        weak = items(await s.call_tool("get_weak_cards", {"limit": 3}))
        check("get_weak_cards sorted by lapses", [c["lapses"] for c in weak] == sorted((c["lapses"] for c in weak), reverse=True),
              f"top: {weak[0]['fields'] if weak else '-'}")

        spanish = {"deck": "Languages::🇪🇸 Spanish", "note_type": "Spanish"}
        cases = [
            ("valid native card", "valid",
             {**spanish, "fields": {"Word": "smoke-test-zzz", "Meaning": "test"}, "audio": {"field": "Audio", "voice": "es-MX"}}),
            ("Yanki deck blocked", "Yanki", {"deck": "Go", "fields": {"Front": "q", "Back": "a"}}),
            ("unknown deck suggests names", "Similar", {"deck": "Golang", "fields": {"Front": "q", "Back": "a"}}),
            ("unknown field rejected", "Unknown fields", {**spanish, "fields": {"Question": "x"}}),
            ("wrong audio field rejected", "Audio field", {**spanish, "fields": {"Word": "x2"}, "audio": {"field": "Sound", "voice": "es-MX"}}),
            ("duplicate detected", "duplicate", {**spanish, "fields": {"Word": "la cumbre", "Meaning": "peak"}}),
        ]
        res = items(await s.call_tool("add_notes", {"dry_run": True, "notes": [c[2] for c in cases]}))[0]["results"]
        for (label, want, _), r in zip(cases, res):
            got = r["status"] if want == "valid" else r.get("error", "")
            check(f"add_notes: {label}", want in got, got[:90])

        latin = {"query": '"deck:Languages::Latin"', "text_field": "Front", "audio_field": "Audio", "limit": 3}
        plan = items(await s.call_tool("add_audio", {**latin, "voice": "espeak:la"}))[0]
        check("add_audio dry run plans a batch", plan["dry_run"] and len(plan["would_voice"]) <= 3, f"eligible {plan['eligible']}")
        fake = await s.call_tool("add_audio", {**latin, "voice": "la"})
        check("add_audio rejects Google 'la' even in dry run", fake.is_error and "espeak:la" in fake.content[0].text)

        voiced = items(await s.call_tool("search_notes", {"query": '"deck:Languages::🇪🇸 Spanish" Audio:*sound*', "limit": 1}))[0]["notes"]
        if voiced:
            nid = voiced[0]["note_id"]
            raw = items(await s.call_tool("get_notes", {"note_ids": [nid], "raw": True}))[0]["fields"]
            check("get_notes raw keeps [sound:] tags", "[sound:" in raw.get("Audio", ""), raw.get("Audio", "")[:60])
            upd = items(await s.call_tool("update_notes", {"updates": [
                {"note_id": nid, "fields": {"Audio": ""}},
                {"note_id": nid, "fields": {"Meaning": raw["Meaning"] + " (smoke)"}},
            ]}))[0]
            check("update_notes dry run refuses audio removal and shows a diff",
                  upd["dry_run"] and "Would remove audio" in upd["results"][0].get("error", "") and upd["results"][1]["status"] == "would_update",
                  str(upd["summary"]))

        bad = await s.call_tool("search_notes", {"query": "deck:Go", "limit": 500})
        check("schema rejects limit=500 with a readable error", bad.is_error and "less than or equal to 100" in bad.content[0].text)

    print(f"\n{len(failures)} failed" if failures else "\nall checks passed")
    sys.exit(1 if failures else 0)


asyncio.run(main())
