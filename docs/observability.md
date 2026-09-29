# Observability for MCP servers (OpenTelemetry)

This guide shows how to see what an MCP server does when an agent uses it: which tools the agent called, how long each
call took, what failed, and why. Part 1 runs it for anki-mcp. Part 2 is the recipe for adding the same to any Python MCP
server.

```
agent host (Claude Code) ──stdio──▶ MCP server ──OTLP/HTTP :4318──▶ collector ──▶ Tempo (traces)
                                        │                                   ├──▶ Prometheus (metrics)
                                        └── stderr logs (host's MCP log)    └──▶ Loki (logs) ──▶ Grafana :3000
```

The server exports all three signals over **OTLP**, the vendor-neutral OpenTelemetry protocol. Any backend that speaks
OTLP works (Grafana, Jaeger, Honeycomb, Datadog…), and switching backends means changing one env var.

## Part 1: See anki-mcp's telemetry

### 1. Start a local backend

`grafana/otel-lgtm` runs a collector, Tempo, Prometheus, Loki and Grafana in one container:

```bash
docker run -d --name otel-lgtm -p 3000:3000 -p 4318:4318 grafana/otel-lgtm
```

Stop it with `docker rm -f otel-lgtm`. Its data is lost when you do, which is fine for local debugging.

### 2. Point the server at it

Exporting needs two things: the `otel` extra (the OpenTelemetry SDK and exporter) and an endpoint. Without an
endpoint the server exports nothing and adds no overhead.

```bash
claude mcp remove anki -s user    # removes only the "anki" entry at user scope; `add` won't overwrite an existing one
claude mcp add anki --scope user -e OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
  -- uv --directory /absolute/path/to/anki-mcp run --extra otel anki-mcp
```

To leave the global entry alone, use `--scope local` instead. It applies only when you work in this folder and takes
precedence over the user-scope entry. Start a new Claude Code session afterwards so it launches the server again.

To get data without using Claude Code, generate traffic. This script starts its own server with exporting on and
makes random read-only calls, dry-run writes and a few deliberate mistakes (it refuses to write for real):

```bash
uv run --extra otel python scripts/otel_traffic.py              # 60 scenarios, about 2 minutes
uv run --extra otel python scripts/otel_traffic.py --calls 300  # more data, for rate() and latency graphs
```

### 3. Look at it

Open http://localhost:3000 (login `admin` / `admin` if asked) and go to **Explore**:

| Signal       | Data source    | Query                                                                                                        |
| --------------| ----------------| --------------------------------------------------------------------------------------------------------------|
| Traces       | Tempo → Search | Service Name = `anki-mcp`, time range *Last 1 hour*                                                          |
| Metrics      | Prometheus     | `sum by (gen_ai_tool_name, outcome) (anki_mcp_tool_calls_total)`                                             |
| Latency      | Prometheus     | `histogram_quantile(0.9, sum by (le, anki_action) (rate(anki_mcp_ankiconnect_duration_seconds_bucket[5m])))` |
| TTS failures | Prometheus     | `sum by (tts_voice) (anki_mcp_tts_duration_seconds_count{outcome="error"})`                                  |
| Logs         | Loki           | `{service_name="anki-mcp"}`                                                                                  |

Prometheus names differ from the OTel ones: dots become underscores, and the unit and `_total` are appended
(`anki_mcp.tool.calls` → `anki_mcp_tool_calls_total`). `rate()` queries need a few minutes of traffic before they
return anything. A metric appears only after its first use, so the TTS one shows up once audio has been generated.

A trace for one tool call looks like this (this output came from a real run):

```
tools/call describe_deck 359.7ms  {anki.deck: Languages::SpanishRandom}
  └ ankiconnect deckNames 24.5ms
  └ ankiconnect findNotes 147.9ms
  └ ankiconnect getDeckStats 34.0ms
  └ ...
tools/call describe_deck 13.1ms ERROR  {anki.deck: Spansh,
     error.message: "Deck 'Spansh' does not exist. Similar: ['Languages::SpanishRandom', ...]"}
```

### What anki-mcp emits

- **Spans:** `tools/call <tool>` (from the MCP SDK) with `anki.deck`, `anki.dry_run` and `error.message`. Its
  children are `ankiconnect <action>` (CLIENT) and `tts <engine>`.
- **Metrics:** `anki_mcp.tool.calls` and `anki_mcp.tool.duration` (tool, outcome), `anki_mcp.ankiconnect.duration`
  (action, outcome), `anki_mcp.tts.duration` (engine, voice, outcome), and `anki_mcp.notes.written` (operation, deck).
- **Logs:** writes (added, voiced, updated, synced) and tool errors. They always go to stderr; with an endpoint set they
  go to OTLP too.
- **Never recorded:** note contents. Error messages can quote the word being voiced.

### Knobs (standard OpenTelemetry env vars)

| Variable                                        | Use                                                                                 |
| -------------------------------------------------| -------------------------------------------------------------------------------------|
| `OTEL_EXPORTER_OTLP_ENDPOINT`                   | Turns exporting on. Per-signal: `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT` |
| `OTEL_EXPORTER_OTLP_HEADERS`                    | Auth for hosted backends, e.g. `x-honeycomb-team=<key>`                             |
| `OTEL_SERVICE_NAME`, `OTEL_RESOURCE_ATTRIBUTES` | Rename the service or tag it (`deployment.environment=laptop`)                      |
| `OTEL_METRIC_EXPORT_INTERVAL`                   | In milliseconds. anki-mcp defaults to 10000, not 60000 (see gotchas)                |
| `OTEL_SDK_DISABLED=true`                        | Turns exporting off without removing the endpoint                                   |
| `ANKI_MCP_LOG_LEVEL`                            | stderr/OTLP log level (default `INFO`)                                              |

### Troubleshooting

- **No traces in Tempo search:** widen the time range. The default window is short, and Tempo can take a few seconds
  before new traces show up in search.
- **Nothing at all:** check the server's stderr in Claude Code's MCP log
  (`~/Library/Caches/claude-cli-nodejs/<project>/mcp-logs-anki/`). If exporting started, it says
  `exporting OpenTelemetry traces, metrics and logs over OTLP`. If the extra is missing, it says `OpenTelemetry SDK missing`.
- **Env var set in your shell but ignored:** MCP hosts launch servers with a minimal environment. Pass the variables
  with `claude mcp add -e`, or the `env` block of the host's config.

## Part 2: Add OpenTelemetry to your own MCP server

The recipe below is for the MCP Python SDK 2.x (`MCPServer`). The ideas carry over to other SDKs.

### 1. See what the SDK already gives you

The Python SDK 2.x ships `OpenTelemetryMiddleware`, which is **on by default**. It opens a SERVER span for every request
(`tools/call <name>`, `initialize`, `tools/list`) and sets `gen_ai.tool.name` and `mcp.method.name` on it. It also
continues a trace context sent by the client in `_meta`. The middleware uses only `opentelemetry-api`, so it does
nothing until you install an SDK provider. Step 3 is often all you need to get traces.

### 2. Instrument against the API only

Record spans and metrics through the API (`opentelemetry.trace`, `opentelemetry.metrics`), and log with the standard
`logging` module. Tracers and meters created at import time are proxies that start working once a provider is
installed, so module-level instruments are fine:

```python
tracer = trace.get_tracer("my_server")
meter = metrics.get_meter("my_server")
calls = meter.create_counter("my_server.tool.calls")

with tracer.start_as_current_span("backend fetch", kind=SpanKind.CLIENT, attributes={"backend.action": action}):
    ...  # exceptions are recorded on the span and set its status to ERROR
```

What to instrument:
- **Every outbound call** (HTTP API, database, subprocess), as a CLIENT span plus a duration histogram tagged with the
  action and the outcome. Slow tool calls usually spend their time here.
- **Per-tool metrics.** The SDK gives you spans, not metrics. Add a middleware:
  `MCPServer(..., middleware=[ToolMetrics()])`. It runs *inside* the SDK's OTel middleware, so
  `trace.get_current_span()` in it is the `tools/call` span, and you can add domain attributes (deck, project, repo) to it.
- **Domain counters**, e.g. items written by operation. These answer "what did the agent actually change?".

### 3. Configure the SDK once, in `main()`, when an endpoint is set

Put `opentelemetry-sdk` and `opentelemetry-exporter-otlp-proto-http` in an optional extra. Then install a
`TracerProvider`, `MeterProvider` and `LoggerProvider` with OTLP exporters, but only when an `OTEL_EXPORTER_OTLP_*`
endpoint is set. Attach `LoggingHandler` to your logger to bridge `logging` into OTLP logs. See
`src/anki_mcp/telemetry.py:setup()` for a complete, tested version, about 50 lines long.

### 4. Gotchas specific to MCP servers

- **Keep telemetry off stdout.** On stdio servers stdout carries the protocol, so a console exporter or a stray `print`
  corrupts the stream. Use OTLP exporters and send logs to stderr. Add a test that asserts stdout is empty.
- **Tool errors aren't exceptions in middleware.** A raised `ToolError` is turned into a result with `isError: true`
  before middleware sees it, so detect failures by inspecting the result. The SDK's own middleware does the same.
- **Hosts stop stdio servers with SIGTERM**, which skips `atexit`. Without a handler, the batched spans and logs from a
  session's last few seconds are lost. Install a SIGTERM handler that calls `shutdown()` on each provider (this flushes
  them) and then exits.
- **Sessions are short.** The default 60s metric export interval loses most of a short session, so lower it (10s).
- **Keep metric attributes low-cardinality.** Tool names, actions, outcomes and deck names are fine. Note IDs, queries
  and user text belong on spans, if anywhere.
- **Privacy.** Tool arguments and results are often personal data. Record names, IDs and counts, not contents, and be
  careful which hosted backend gets error messages that quote user input.
- **Testing.** Global providers can be set only once per process. Install in-memory ones (`InMemorySpanExporter`, and
  `InMemoryMetricReader` with *delta* temporality so each test sees only its own points) once per test module. To test
  real OTLP wiring, run `setup()` in a subprocess against a fake HTTP receiver. See `tests/test_telemetry.py`.
- **Environment.** Hosts start servers with a minimal environment, so pass the `OTEL_*` variables explicitly in the
  host config.

### 5. Beyond one server

- **Client-side traces.** A client that sends W3C trace context in `_meta` makes the server's spans children of the
  agent's own trace, so the whole agent run shows up as one trace. The Python SDK's client and server both do this.
  Whether an agent host sends it depends on the host.
- **The agent host's own telemetry.** Claude Code can export its own metrics and events (token usage, cost, tool
  decisions) over OTLP. Send them to the same backend and you can see the agent side next to the server side. Check
  the Claude Code monitoring docs for the current variable names.
