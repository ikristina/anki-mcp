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


def test_missing_piper_voice_says_where_to_put_it(monkeypatch, tmp_path):
    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(tts, "PIPER_VOICES", tmp_path)
    (tmp_path / "other.onnx").write_bytes(b"")
    with pytest.raises(AnkiError, match=r"la_LA-vox-medium\.onnx\.json in .*Installed: \['other'\]"):
        tts.validate_voice("piper:la_LA-vox-medium")


def test_piper_voice_needs_its_json(monkeypatch, tmp_path):
    monkeypatch.setattr(tts.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(tts, "PIPER_VOICES", tmp_path)
    (tmp_path / "v.onnx").write_bytes(b"")
    with pytest.raises(AnkiError, match="not found"):
        tts.validate_voice("piper:v")
    (tmp_path / "v.onnx.json").write_text("{}")
    tts.validate_voice("piper:v")


def test_piper_audio_is_m4a(monkeypatch):
    monkeypatch.setattr(tts, "_piper_wav", lambda text, voice: b"wav")
    monkeypatch.setattr(tts, "_to_m4a", lambda audio, suffix: b"m4a:" + audio)
    name, data = tts.synthesize("amīcus", "piper:v")
    assert name.endswith(".m4a") and data == b"m4a:wav"


def test_edge_voice_name_is_checked():
    tts.validate_voice("edge:es-MX-JorgeNeural")
    with pytest.raises(AnkiError, match="es-MX-JorgeNeural"):
        tts.validate_voice("edge:jorge")


def test_edge_audio_is_mp3(monkeypatch):
    monkeypatch.setattr(tts, "_edge", lambda text, voice: b"mp3")
    name, data = tts.synthesize("hola", "edge:es-MX-JorgeNeural")
    assert name.endswith(".mp3") and data == b"mp3"


def test_random_voice_is_stable_per_text_and_uses_both(monkeypatch):
    voice = "random:es-MX,edge:es-MX-JorgeNeural"
    picks = {tts.resolve_voice(f"frase {i}", voice) for i in range(30)}
    assert picks == {"es-MX", "edge:es-MX-JorgeNeural"}
    assert tts.resolve_voice("hola", voice) == tts.resolve_voice("hola", voice)
    assert tts.audio_stem("hola", voice) == tts.audio_stem("hola", tts.resolve_voice("hola", voice))
    tts.validate_voice(voice)


def test_random_voice_needs_two_plain_voices():
    for bad in ("random:es-MX", "random:", "random:es-MX,random:fr,de"):
        with pytest.raises(AnkiError, match="two or more"):
            tts.validate_voice(bad)
