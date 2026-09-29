"""Minimal AnkiConnect client (https://foosoft.net/projects/anki-connect/)."""

import json
import os
import urllib.error
import urllib.request

from mcp.server.mcpserver.exceptions import ToolError

ANKI_URL = os.environ.get("ANKI_CONNECT_URL", "http://127.0.0.1:8765")


class AnkiError(ToolError):
    """Anticipated failure. Subclassing ToolError is what lets the message reach the model;
    any other exception type is reduced to a generic 'Error executing tool' in MCP SDK 2.x."""


def invoke(action: str, timeout: float = 30, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(ANKI_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except TimeoutError as e:
        raise AnkiError(
            f"AnkiConnect did not answer '{action}' within {timeout:g}s. Anki is busy or showing a dialog "
            "(AnkiConnect waits for it to close): ask the user to check the Anki window."
        ) from e
    except urllib.error.URLError as e:
        raise AnkiError(
            f"Cannot reach AnkiConnect at {ANKI_URL} ({e.reason}). "
            "Anki is probably not running: ask the user to open Anki (with the AnkiConnect add-on) and retry."
        ) from e
    if body.get("error"):
        raise AnkiError(f"AnkiConnect error on '{action}': {body['error']}")
    return body["result"]
