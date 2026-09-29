import asyncio
import io
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from mcp import Client
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from anki_mcp import client, server, tts
from anki_mcp.client import AnkiError

# OTel allows setting the global providers once per process, so this module installs in-memory ones for all its tests.
_spans = InMemorySpanExporter()
# Delta temporality: each collect() returns only what was recorded since the previous one.
_metrics = InMemoryMetricReader(preferred_temporality={Counter: AggregationTemporality.DELTA, Histogram: AggregationTemporality.DELTA})


@pytest.fixture(scope="module", autouse=True)
def _providers():
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(_spans))
    trace.set_tracer_provider(provider)
    metrics.set_meter_provider(MeterProvider(metric_readers=[_metrics]))


@pytest.fixture
def otel():
    _spans.clear()
    collect()  # drop what earlier tests recorded
    return _spans


def collect():
    """Metrics recorded since the last collect, as {name: {frozen attributes: sum (counter) or count (histogram)}}."""
    out = {}
    data = _metrics.get_metrics_data()
    for rm in data.resource_metrics if data else ():
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                for p in m.data.data_points:
                    out.setdefault(m.name, {})[frozenset(p.attributes.items())] = getattr(p, "value", None) or p.count
    return out


def attrs(**kw):
    return frozenset(kw.items())


def anki_over_http(monkeypatch, fake):
    """Route the real client's HTTP requests to FakeAnki, so client.invoke and its spans run for real."""
    def urlopen(req, timeout):
        body = json.loads(req.data)
        try:
            payload = {"result": fake(body["action"], **body["params"]), "error": None}
        except AnkiError as e:
            payload = {"result": None, "error": str(e)}
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(client.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(server, "invoke", client.invoke)


def call(tool, args):
    async def run():
        async with Client(server.mcp) as c:
            return await c.call_tool(tool, args)
    return asyncio.run(run())


def test_tool_call_nests_ankiconnect_spans_under_sdk_span(anki, monkeypatch, otel):
    anki_over_http(monkeypatch, anki)
    result = call("describe_deck", {"deck": "Languages::Spanish"})
    assert not result.is_error
    spans = {s.name: s for s in otel.get_finished_spans()}
    tool_span = spans["tools/call describe_deck"]
    assert tool_span.attributes["anki.deck"] == "Languages::Spanish"
    assert spans["ankiconnect findNotes"].parent.span_id == tool_span.context.span_id
    assert spans["ankiconnect findNotes"].kind == trace.SpanKind.CLIENT
    m = collect()
    assert m["anki_mcp.tool.calls"] == {attrs(**{"gen_ai.tool.name": "describe_deck", "outcome": "ok"}): 1}
    assert attrs(**{"anki.action": "findNotes", "outcome": "ok"}) in m["anki_mcp.ankiconnect.duration"]


def test_tool_error_is_counted_and_logged(anki, monkeypatch, otel, caplog):
    anki_over_http(monkeypatch, anki)
    assert call("describe_deck", {"deck": "Spansh"}).is_error
    span = next(s for s in otel.get_finished_spans() if s.name == "tools/call describe_deck")
    assert span.status.status_code is StatusCode.ERROR
    assert "Languages::Spanish" in span.attributes["error.message"]  # the fix-it suggestion survives
    assert collect()["anki_mcp.tool.calls"] == {attrs(**{"gen_ai.tool.name": "describe_deck", "outcome": "error"}): 1}
    assert "tool describe_deck failed" in caplog.text


def test_failed_ankiconnect_action_marks_span(monkeypatch, otel):
    monkeypatch.setattr(client.urllib.request, "urlopen",
                        lambda req, timeout: io.BytesIO(b'{"result": null, "error": "collection is not available"}'))
    with pytest.raises(AnkiError):
        client.invoke("deckNames")
    (span,) = otel.get_finished_spans()
    assert span.name == "ankiconnect deckNames" and span.status.status_code is StatusCode.ERROR
    assert "collection is not available" in span.status.description
    assert collect()["anki_mcp.ankiconnect.duration"] == {attrs(**{"anki.action": "deckNames", "outcome": "error"}): 1}


def test_tts_rate_limit_is_an_error_datapoint(monkeypatch, otel):
    class Limited:
        def __init__(self, **_): pass
        def write_to_fp(self, _): raise RuntimeError("429 (Too Many Requests)")

    monkeypatch.setattr(tts, "gTTS", Limited)
    with pytest.raises(AnkiError, match="429"):
        tts.synthesize("hola", "es-MX")
    (span,) = otel.get_finished_spans()
    assert span.name == "tts google" and span.attributes["tts.voice"] == "es-MX"
    assert collect()["anki_mcp.tts.duration"] == {attrs(**{"tts.engine": "google", "tts.voice": "es-MX", "outcome": "error"}): 1}


def test_writes_are_counted_by_deck(anki, otel, caplog):
    caplog.set_level("INFO", "anki_mcp")
    call("add_notes", {"notes": [{"deck": "Languages::Spanish", "note_type": "Spanish",
                                  "fields": {"Word": "el gato", "Meaning": "cat"}}]})
    assert collect()["anki_mcp.notes.written"] == {attrs(operation="add", **{"anki.deck": "Languages::Spanish"}): 1}
    assert "added 1 notes" in caplog.text


def test_dry_run_writes_count_nothing(anki, otel):
    call("add_notes", {"dry_run": True, "notes": [{"deck": "Languages::Spanish", "note_type": "Spanish",
                                                   "fields": {"Word": "el gato", "Meaning": "cat"}}]})
    assert "anki_mcp.notes.written" not in collect()


def test_setup_without_endpoint_does_not_export(monkeypatch):
    from anki_mcp import telemetry
    for var in telemetry._ENDPOINT_VARS:
        monkeypatch.delenv(var, raising=False)
    try:
        assert telemetry.setup() is False
    finally:
        telemetry.log.handlers.clear()
        telemetry.log.propagate = True


def test_setup_exports_all_three_signals_over_otlp_and_flushes_on_sigterm():
    """Real exporters against a fake OTLP/HTTP receiver, in a subprocess so the global providers are fresh."""
    paths = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            paths.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_):
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    script = (
        "import os, signal\n"
        "from anki_mcp import telemetry\n"
        "assert telemetry.setup()\n"
        "with telemetry.tracer.start_as_current_span('probe'): telemetry.log.info('hello')\n"
        "telemetry.tool_calls.add(1, {'gen_ai.tool.name': 'probe', 'outcome': 'ok'})\n"
        "os.kill(os.getpid(), signal.SIGTERM)\n"
    )
    env = {**os.environ, "OTEL_EXPORTER_OTLP_ENDPOINT": f"http://127.0.0.1:{httpd.server_port}"}
    try:
        proc = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    finally:
        httpd.shutdown()
    assert proc.returncode == 128 + 15, proc.stderr
    assert proc.stdout == ""  # stdout carries the MCP protocol
    assert "hello" in proc.stderr
    assert {"/v1/traces", "/v1/metrics", "/v1/logs"} <= set(paths)
