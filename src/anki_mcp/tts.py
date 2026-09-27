"""Free text-to-speech. Three engines, chosen by the voice string:

- 'es-MX', 'fr', ...   Google Translate voices via gTTS (online). The same voices HyperTTS's GoogleTranslate uses.
- 'espeak:la'          eSpeak NG (offline, robotic, but has real rules for languages Google lacks, e.g. Latin).
- 'macos:Alice'        A macOS `say` voice (offline, natural). Run `say -v '?'` to list them.
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

# Region -> Google Translate top-level domain, which selects the accent.
_REGION_TLD = {
    "MX": "com.mx", "ES": "es", "US": "us", "GB": "co.uk", "AU": "com.au", "IN": "co.in",
    "CA": "ca", "FR": "fr", "BR": "com.br", "PT": "pt",
}
# Listed by gTTS but not real voices: Google falls back to English-sounding output.
_GOOGLE_FAKE = {"la"}


def synthesize(text: str, voice: str) -> tuple[str, bytes]:
    """Return (filename, audio bytes) for text spoken by voice. Raises AnkiError with a fix-it message."""
    engine, _, name = voice.partition(":") if ":" in voice else ("google", "", voice)
    try:
        if engine == "google":
            data, ext = _google(text, name), "mp3"
        elif engine == "espeak":
            data, ext = _to_m4a(_espeak_wav(text, name), ".wav"), "m4a"
        elif engine == "macos":
            data, ext = _to_m4a(_say_aiff(text, name), ".aiff"), "m4a"
        else:
            raise AnkiError(f"Unknown TTS engine '{engine}'. Use 'es-MX' (Google), 'espeak:la' or 'macos:Alice'.")
    except subprocess.CalledProcessError as e:
        raise AnkiError(f"{engine} TTS failed for {text!r}: {e.stderr.decode(errors='replace').strip() or e}") from e
    digest = hashlib.sha224(f"{voice}|{text}".encode()).hexdigest()
    return f"anki-mcp-{digest}.{ext}", data


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
    _require("say", "The macOS voices are only available on a Mac.")
    # `say` silently falls back to the default voice for unknown names, so check explicitly.
    listing = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, check=True).stdout
    names = {line.split("  ")[0].strip() for line in listing.splitlines() if line.strip()}
    if voice not in names:
        near = sorted(n for n in names if voice.lower() in n.lower())[:5]
        raise AnkiError(f"macOS voice '{voice}' not found. Similar: {near or 'none'}. Italian voices include 'Alice'.")
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "out.aiff"
        subprocess.run(["say", "-v", voice, "-r", "150", "-o", str(out), text], capture_output=True, check=True)
        return out.read_bytes()


def _to_m4a(audio: bytes, suffix: str) -> bytes:
    """Compress to AAC (.m4a) with macOS's afconvert; Anki plays m4a on all platforms."""
    _require("afconvert", "afconvert ships with macOS.")
    with tempfile.TemporaryDirectory() as tmp:
        src, dst = Path(tmp) / f"in{suffix}", Path(tmp) / "out.m4a"
        src.write_bytes(audio)
        subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", str(src), str(dst)], capture_output=True, check=True)
        return dst.read_bytes()
