import pytest

from anki_mcp import client, tts
from anki_mcp.client import AnkiError


def test_google_fake_latin_rejected():
    with pytest.raises(AnkiError, match="espeak:la"):
        tts.validate_voice("la")


def test_google_real_voice_accepted():
    tts.validate_voice("es-MX")
    tts.validate_voice("fr")


def test_unknown_engine_rejected():
    with pytest.raises(AnkiError, match="Unknown TTS engine"):
        tts.validate_voice("polly:Joanna")


def test_missing_binary_gives_install_hint(monkeypatch):
    monkeypatch.setattr(tts.shutil, "which", lambda name: None)
    with pytest.raises(AnkiError, match="brew install espeak-ng"):
        tts.validate_voice("espeak:la")


def test_filename_is_stable_per_text_and_voice(monkeypatch):
    monkeypatch.setattr(tts, "_google", lambda text, voice: b"mp3")
    a, _ = tts.synthesize("hola", "es-MX")
    b, _ = tts.synthesize("hola", "es-MX")
    c, _ = tts.synthesize("hola", "es-ES")
    assert a == b != c and a.startswith("anki-mcp-") and a.endswith(".mp3")


def test_unreachable_anki_says_what_to_do(monkeypatch):
    monkeypatch.setattr(client, "ANKI_URL", "http://127.0.0.1:9")  # nothing listens on the discard port
    with pytest.raises(AnkiError, match="open Anki"):
        client.invoke("version")


def test_slow_anki_says_to_check_for_a_dialog(monkeypatch):
    def hang(req, timeout):
        raise TimeoutError("timed out")
    monkeypatch.setattr(client.urllib.request, "urlopen", hang)
    with pytest.raises(AnkiError, match="within 5s.*dialog"):
        client.invoke("sync", timeout=5)


def test_error_is_a_tool_error():
    # Only ToolError messages reach the model in MCP SDK 2.x; anything else becomes "Error executing tool".
    from mcp.server.mcpserver.exceptions import ToolError
    assert issubclass(AnkiError, ToolError)
