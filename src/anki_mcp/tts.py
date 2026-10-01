"""Free text-to-speech. Four engines, chosen by the voice string:

- 'es-MX', 'fr', ...   Google Translate voices via gTTS (online). The same voices HyperTTS's GoogleTranslate uses.
- 'espeak:la'          eSpeak NG (offline, robotic, but has real rules for languages Google lacks, e.g. Latin).
- 'macos:Alice'        A macOS `say` voice (offline, natural). Run `say -v '?'` to list them.
- 'piper:la_LA-vox-medium'  A Piper neural voice (offline, natural): <name>.onnx + .onnx.json in PIPER_VOICES.
"""

import hashlib
import io
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

from gtts import gTTS
from gtts.lang import tts_langs

from anki_mcp.client import AnkiError
from anki_mcp.telemetry import operation, tts_duration

PIPER_VOICES = Path("~/.local/share/piper-voices").expanduser()
# Region -> Google Translate top-level domain, which selects the accent.
_REGION_TLD = {
    "MX": "com.mx", "ES": "es", "US": "us", "GB": "co.uk", "AU": "com.au", "IN": "co.in",
    "CA": "ca", "FR": "fr", "BR": "com.br", "PT": "pt",
}
# Listed by gTTS but not real voices: Google falls back to English-sounding output.
_GOOGLE_FAKE = {"la"}


def validate_voice(voice: str) -> None:
    """Cheap check (no audio generated) so dry runs reject voices that would fail or produce wrong audio."""
    engine, _, name = voice.partition(":") if ":" in voice else ("google", "", voice)
    if engine == "google":
        lang = name.partition("-")[0].lower()
        if lang in _GOOGLE_FAKE or lang not in tts_langs():
            raise AnkiError(f"Google has no real '{lang}' voice. For Latin use 'espeak:la' or 'macos:Alice'.")
    elif engine == "espeak":
        _require("espeak-ng", "Install it with `brew install espeak-ng`.")
        listing = subprocess.run(["espeak-ng", f"--voices={name}"], capture_output=True, text=True).stdout
        if len(listing.strip().splitlines()) < 2:
            raise AnkiError(f"eSpeak has no '{name}' voice. List them with `espeak-ng --voices`.")
    elif engine == "macos":
        _check_macos_voice(name)
    elif engine == "piper":
        _piper_model(name)
    else:
        raise AnkiError(f"Unknown TTS engine '{engine}'. Use 'es-MX' (Google), 'espeak:la', 'macos:Alice' or 'piper:<voice>'.")


def synthesize(text: str, voice: str) -> tuple[str, bytes]:
    """Return (filename, audio bytes) for text spoken by voice. Raises AnkiError with a fix-it message."""
    engine, _, name = voice.partition(":") if ":" in voice else ("google", "", voice)
    with operation(f"tts {engine}", tts_duration, {"tts.engine": engine, "tts.voice": voice}) as span:
        try:
            if engine == "google":
                data, ext = _google(text, name), "mp3"
            elif engine == "espeak":
                data, ext = _to_m4a(_espeak_wav(text, name), ".wav"), "m4a"
            elif engine == "macos":
                data, ext = _to_m4a(_say_aiff(text, name), ".aiff"), "m4a"
            elif engine == "piper":
                data, ext = _to_m4a(_piper_wav(text, name), ".wav"), "m4a"
            else:
                raise AnkiError(f"Unknown TTS engine '{engine}'. Use 'es-MX' (Google), 'espeak:la', 'macos:Alice' or 'piper:<voice>'.")
        except subprocess.CalledProcessError as e:
            raise AnkiError(f"{engine} TTS failed for {text!r}: {e.stderr.decode(errors='replace').strip() or e}") from e
        span.set_attribute("tts.audio_bytes", len(data))
    return f"{audio_stem(text, voice)}.{ext}", data


def audio_stem(text: str, voice: str) -> str:
    """Media filename (no extension) for text spoken by voice: the same text and voice always give the same file."""
    return "anki-mcp-" + hashlib.sha224(f"{voice}|{text}".encode()).hexdigest()


def _google(text: str, voice: str) -> bytes:
    lang, _, region = voice.partition("-")
    lang = lang.lower()
    if lang in _GOOGLE_FAKE or lang not in tts_langs():
        raise AnkiError(f"Google has no real '{lang}' voice. For Latin use 'espeak:la' or 'macos:Alice'.")
    buf = io.BytesIO()
    try:
        gTTS(text=text, lang=lang, tld=_REGION_TLD.get(region.upper(), "com")).write_to_fp(buf)
    except Exception as e:  # network errors, rate limits
        raise AnkiError(f"Audio generation failed for {text!r} ({voice}): {e}. Retry, or add the note without audio.") from e
    return buf.getvalue()


def _require(binary: str, hint: str) -> None:
    if not shutil.which(binary):
        raise AnkiError(f"'{binary}' is not installed. {hint}")


def _espeak_wav(text: str, lang: str) -> bytes:
    _require("espeak-ng", "Install it with `brew install espeak-ng`.")
    raw = subprocess.run(["espeak-ng", "-v", lang, "-s", "140", "--stdout", text], capture_output=True, check=True).stdout
    # espeak streams WAV with placeholder chunk sizes that afconvert rejects; rewrite a proper header.
    rate = struct.unpack("<I", raw[24:28])[0]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(raw[raw.find(b"data") + 8 :])
    return buf.getvalue()


def _say_aiff(text: str, voice: str) -> bytes:
    _check_macos_voice(voice)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.aiff"
        subprocess.run(["say", "-v", voice, "-r", "150", "-o", str(out), text], capture_output=True, check=True)
        return out.read_bytes()


def _check_macos_voice(voice: str) -> None:
    _require("say", "The macOS voices are only available on a Mac.")
    # `say` silently falls back to the default voice for unknown names, so check explicitly.
    listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    names = {line.split("  ")[0].strip() for line in listing.splitlines() if line.strip()}
    if voice not in names:
        near = sorted(n for n in names if voice.lower() in n.lower())[:5]
        raise AnkiError(f"macOS voice '{voice}' not found. Similar: {near or 'none'}. Italian voices include 'Alice'.")


def _piper_model(voice: str) -> Path:
    _require("piper", "Install it with `uv tool install piper-tts`.")
    model = PIPER_VOICES / f"{voice}.onnx"
    # piper needs the .onnx.json next to the model (phoneme map, espeak voice); without it, it can't speak.
    if not (model.is_file() and model.with_suffix(".onnx.json").is_file()):
        have = sorted(p.stem for p in PIPER_VOICES.glob("*.onnx"))
        raise AnkiError(f"Piper voice '{voice}' not found: put {voice}.onnx and {voice}.onnx.json in {PIPER_VOICES}. "
                        f"Installed: {have or 'none'}.")
    return model


def _piper_wav(text: str, voice: str) -> bytes:
    model = _piper_model(voice)
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.wav"
        subprocess.run(["piper", "-m", str(model), "-f", str(out)], input=text.encode(), capture_output=True, check=True)
        return out.read_bytes()


def _to_m4a(audio: bytes, suffix: str) -> bytes:
    """Compress to AAC (.m4a) with macOS's afconvert; Anki plays m4a on all platforms."""
    _require("afconvert", "afconvert ships with macOS.")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / f"in{suffix}", Path(tmp) / "out.m4a"
        src.write_bytes(audio)
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(src), str(dst)], capture_output=True, check=True)
        return dst.read_bytes()
