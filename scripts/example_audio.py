"""Add a tap-to-play player for an example sentence to a note's 'Extra 2' (es-MX voice by default).

Usage: uv run python scripts/example_audio.py [--voice es-MX] 123456=Spanish sentence. 789=Another one.
Speaks only the Spanish sentence. Uses <audio controls>, not [sound:], so it doesn't autoplay (see LEARNINGS.md).
Skips notes whose field already has a player. Needs Anki running.
"""
import argparse
import base64

from anki_mcp.client import invoke
from anki_mcp.tts import synthesize

p = argparse.ArgumentParser()
p.add_argument("--voice", default="es-MX")
p.add_argument("--field", default="Extra 2")
p.add_argument("pairs", nargs="+", help="NOTE_ID=sentence")
a = p.parse_args()

for pair in a.pairs:
    nid, _, text = pair.partition("=")
    cur = invoke("notesInfo", notes=[int(nid)])[0]["fields"][a.field]["value"]
    if "<audio" in cur:
        print("skip", nid)
        continue
    name, data = synthesize(text, a.voice)
    invoke("storeMediaFile", filename=name, data=base64.b64encode(data).decode())
    invoke("updateNoteFields", note={"id": int(nid), "fields": {a.field: f'{cur}<br><audio controls="" src="{name}"></audio>'}})
    print("done", nid, name)
