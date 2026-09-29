"""OpenTelemetry traces, metrics and logs.

The instrumentation below uses only `opentelemetry-api`, which the MCP SDK already depends on, so it costs nothing
until `setup()` installs real providers. That happens when an OTLP endpoint is configured with the standard env vars
(`OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318`, or a per-signal `OTEL_EXPORTER_OTLP_*_ENDPOINT`) and the
`otel` extra is installed. Exporters speak OTLP over HTTP; nothing ever goes to stdout, which carries the protocol.

The MCP SDK emits the SERVER span per request (`tools/call add_notes`); spans here nest under it.
Telemetry never records note contents: only tool and action names, deck names, voices, counts and error messages.
"""

import logging
import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import version
from typing import Any

from mcp.server.context import CallNext, HandlerResult, ServerMiddleware, ServerRequestContext
from mcp.types import CallToolResult
from opentelemetry import metrics, trace
from opentelemetry.trace import Span, SpanKind

log = logging.getLogger("anki_mcp")
tracer = trace.get_tracer("anki_mcp")
meter = metrics.get_meter("anki_mcp")

tool_calls = meter.create_counter("anki_mcp.tool.calls", description="MCP tool calls, by tool and outcome.")
tool_duration = meter.create_histogram("anki_mcp.tool.duration", unit="s", description="MCP tool call duration.")
ankiconnect_duration = meter.create_histogram(
    "anki_mcp.ankiconnect.duration", unit="s", description="AnkiConnect request duration, by action and outcome."
)
tts_duration = meter.create_histogram("anki_mcp.tts.duration", unit="s", description="Audio synthesis duration, by engine and outcome.")
notes_written = meter.create_counter(
    "anki_mcp.notes.written", description="Notes changed in the collection, by operation (add, audio, update)."
)

_ENDPOINT_VARS = ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
                  "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT", "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT")
_ERROR_CHARS = 500


@contextmanager
def operation(name: str, histogram, attributes: dict[str, Any], kind: SpanKind = SpanKind.INTERNAL) -> Iterator[Span]:
    """Span plus duration histogram for one unit of work. `attributes` go on both, so keep them low-cardinality.

    An exception marks the span as failed (with the message) and records outcome=error, then propagates.
    """
    start = time.perf_counter()
    outcome = "error"
    try:
        with tracer.start_as_current_span(name, kind=kind, attributes=attributes) as span:
            yield span
        outcome = "ok"
    finally:
        histogram.record(time.perf_counter() - start, {**attributes, "outcome": outcome})


class ToolMetrics(ServerMiddleware[Any]):
    """Counts and times tool calls, tags the SDK's span with the deck, and logs tool errors.

    Runs inside the SDK's OpenTelemetryMiddleware, so the current span is its `tools/call <name>` SERVER span.
    A tool raising AnkiError doesn't reach middleware as an exception: it comes back as an isError result.
    """

    async def __call__(self, ctx: ServerRequestContext[Any, Any], call_next: CallNext) -> HandlerResult:
        if ctx.method != "tools/call" or not ctx.params:
            return await call_next(ctx)
        tool = str(ctx.params.get("name"))
        args = ctx.params.get("arguments") or {}
        span = trace.get_current_span()
        if isinstance(args.get("deck"), str):
            span.set_attribute("anki.deck", args["deck"])
        if isinstance(args.get("dry_run"), bool):
            span.set_attribute("anki.dry_run", args["dry_run"])

        start = time.perf_counter()
        outcome = "exception"
        try:
            result = await call_next(ctx)
            if message := _tool_error(result):
                outcome = "error"
                span.set_attribute("error.message", message[:_ERROR_CHARS])
                log.warning("tool %s failed: %s", tool, message[:_ERROR_CHARS])
            else:
                outcome = "ok"
            return result
        finally:
            attrs = {"gen_ai.tool.name": tool, "outcome": outcome}
            tool_calls.add(1, attrs)
            tool_duration.record(time.perf_counter() - start, attrs)


def _tool_error(result: Any) -> str | None:
    """The error text of a failed tool result (a model or its wire dict), else None."""
    match result:
        case CallToolResult(is_error=True, content=content):
            return " ".join(getattr(c, "text", "") for c in content) or "tool error"
        case {"isError": True}:
            return " ".join(c.get("text", "") for c in result.get("content", []) if isinstance(c, dict)) or "tool error"
    return None


def setup() -> bool:
    """Log to stderr, and export traces, metrics and logs over OTLP if an endpoint is configured.

    Returns True when exporting. Missing packages or a disabled SDK only produce a stderr warning: telemetry must
    never stop the server from starting.
    """
    level = os.environ.get("ANKI_MCP_LOG_LEVEL", "INFO").upper()
    log.setLevel(level)
    log.propagate = False
    stderr = logging.StreamHandler(sys.stderr)
    stderr.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    log.addHandler(stderr)

    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true" or not any(os.environ.get(v) for v in _ENDPOINT_VARS):
        return False
    try:
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        log.warning("OTLP endpoint set but OpenTelemetry SDK missing (%s). Install with `uv sync --extra otel`.", e)
        return False

    # OTEL_SERVICE_NAME and OTEL_RESOURCE_ATTRIBUTES still apply; Resource.create merges them in.
    resource = Resource.create({"service.name": "anki-mcp", "service.version": version("anki-mcp")})
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(tracer_provider)

    # Sessions are short and the host may kill the process, so export metrics more often than the 60s default.
    interval = int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "10000"))
    meter_provider = MeterProvider(resource=resource, metric_readers=[
        PeriodicExportingMetricReader(OTLPMetricExporter(), export_interval_millis=interval)])
    metrics.set_meter_provider(meter_provider)

    logger_provider = LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    set_logger_provider(logger_provider)
    log.addHandler(LoggingHandler(logger_provider=logger_provider))

    providers = (tracer_provider, meter_provider, logger_provider)

    def flush_and_exit(signum, _frame):
        # The providers flush at interpreter exit, but SIGTERM (how hosts stop stdio servers) skips that.
        for p in providers:
            p.shutdown()
        os._exit(128 + signum)

    signal.signal(signal.SIGTERM, flush_and_exit)
    log.info("exporting OpenTelemetry traces, metrics and logs over OTLP")
    return True
