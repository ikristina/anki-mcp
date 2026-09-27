# Roadmap: Headless On-The-Go Anki Sync (Desktop-Free)

## 1. Problem Statement
The current anki-mcp implementation relies on [AnkiConnect](https://ankiweb.net/shared/info/2055492159), which requires the local Anki Desktop GUI application running on `localhost:8765`. 

When away from a home desktop (or without owning a dedicated desktop server), users cannot:
1. Add vocabulary cards on the go via mobile Claude or remote agents.
2. Generate and attach pronunciation audio headlessly.
3. Automatically sync cards to AnkiWeb without manual `.apkg` file export/import steps.

## 2. Target Architecture

```
[ Mobile Claude / Agent ]
           │
           ▼  (MCP over stdio or Streamable HTTP/SSE)
┌────────────────────────────────────────────────────────┐
│                     anki-mcp Server                    │
│                                                        │
│  1. Audio Synthesis: gTTS -> MP3 (headless)            │
│  2. Media Registration: fastanki.add_media()           │
│  3. Card Creation: fastanki.add_card() + [sound:...]   │
│  4. Cloud Sync: fastanki.sync()                        │
└────────────────────────────────────────────────────────┘
           │
           ▼  (AnkiWeb Native Sync Protocol)
┌────────────────────────────────────────────────────────┐
│                        AnkiWeb                         │
└────────────────────────────────────────────────────────┘
           │
           ▼  (Normal Anki Mobile Sync)
[ AnkiMobile (iOS) / AnkiDroid (Android) ]
```

### Core Technologies
- **Sync Engine:** [`fastanki`](https://github.com/AnswerDotAI/fastanki) (AnswerDotAI): communicates directly with the native AnkiWeb sync protocol in pure Python, bypassing Anki Desktop, Qt, and Xvfb entirely.
- **Audio Engine:** [`gTTS`](src/anki_mcp/tts.py): headless text-to-speech generating MP3 files.
- **Protocol:** Model Context Protocol (MCP) Python SDK with dual transport support:
  - `stdio` for local execution (e.g. mobile Termux or cloud VM sessions).
  - `Streamable HTTP / SSE` for remote deployment accessible to mobile/web agents.

## 3. Implementation Plan

### Phase 1: Dual Backend Abstraction
- Abstract the backend interface in `src/anki_mcp/`:
  - `AnkiConnectClient`: Current implementation via `localhost:8765`.
  - `FastAnkiClient`: Headless sync implementation via `fastanki`.
- Select backend via environment variable: `ANKI_BACKEND=ankiconnect` (default) vs `ANKI_BACKEND=ankiweb`.

### Phase 2: Headless Audio & Media Integration
- Adapt [tts.py](src/anki_mcp/tts.py) so `synthesize()` outputs can be directly handed to `fastanki.add_media()`.
- Ensure generated `[sound:<filename>]` references are formatted correctly for AnkiWeb media uploads.

### Phase 3: Credentials & Sync Lifecycle
- Support AnkiWeb authentication via environment variables:
  - `ANKIWEB_USER`
  - `ANKIWEB_PASSWORD`
- Implement sync triggering after write operations (`add_notes`) so updates reach AnkiWeb immediately.

### Phase 4: Remote MCP Server Deployment
- Add HTTP/SSE transport support to [server.py](src/anki_mcp/server.py) so the server can run on lightweight cloud hosts (e.g., Fly.io, Railway, or VPS).
- Provide a `Dockerfile` and deployment guide for hosting the headless server 24/7.

## 4. Verification & Testing

- [ ] Unit tests for `FastAnkiClient` using mock sync responses.
- [ ] Integration test: synthesize MP3 audio via `gTTS`, call `add_media`, call `add_card`, and verify successful sync with AnkiWeb test account.
- [ ] Mobile verification: sync from AnkiMobile / AnkiDroid to verify cards and audio play without desktop involvement.
