"""The anki-mcp server backed by tests/conftest.py's FakeAnki, for evals. Never reaches the real collection.

Run over stdio like the real server: `uv run python evals/fake_server.py`. Uses a temp copy of evals/profiles.json.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

# Before importing the server: anything that slips past the patches below fails instead of reaching real Anki.
os.environ["ANKI_CONNECT_URL"] = "http://127.0.0.1:9"
ROOT = Path(__file__).resolve().parent.parent
profiles = Path(tempfile.mkdtemp(prefix="anki-mcp-eval-")) / "profiles.json"
shutil.copy(ROOT / "evals" / "profiles.json", profiles)
os.environ["ANKI_MCP_PROFILES"] = str(profiles)
sys.path.insert(0, str(ROOT / "tests"))

from conftest import FakeAnki  # noqa: E402

from anki_mcp import server  # noqa: E402
from anki_mcp.client import AnkiError  # noqa: E402

fake = FakeAnki()


def invoke(action, **params):
    try:
        return fake(action, **params)
    except AssertionError as e:  # the fake doesn't model this; tell the model instead of a generic tool error
        raise AnkiError(f"eval fake: {e}") from e


server.invoke = invoke
server.synthesize = lambda text, voice: (f"anki-mcp-{abs(hash((text, voice)))}.mp3", b"ID3fake")

if __name__ == "__main__":
    server.main()
