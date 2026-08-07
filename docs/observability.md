# Observability

Every LLM call, tool call, and Neo4j query the API makes is recorded as an OpenTelemetry
span and written to a local SQL database. The **Traces** tab in the web app reads it.

No account, no API key, no SaaS. `make up` is enough.

## What you see

One API request is one trace. `POST /api/workout` produces a single trace containing:

```
POST /api/workout                          1.8 s
├─ neo4j.query   (member defaults)          12 ms
├─ invoke_agent constraint-extractor       410 ms
│  └─ chat claude-haiku-4-5                405 ms   180 in / 24 out   $0.0002
├─ neo4j.query   (resolver: exact)           4 ms
├─ neo4j.query   (resolver: vector)         31 ms
├─ neo4j.query   (safety: catalog)           9 ms
└─ invoke_agent workout-planner            1.3 s
   └─ chat claude-haiku-4-5                1.3 s   2140 in / 380 out  $0.0021
```

Expanding a span shows its truncated prompt and completion, its remaining attributes, and
any error. That is what makes a bad plan debuggable after the fact rather than by re-running
it with print statements.

`/api/health` is deliberately never traced: `make up` and the Compose healthcheck poll it
every five seconds, which would bury real traces under roughly 17,000 junk ones a day.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `OBS_ENABLED` | `true` | Turn tracing off entirely. |
| `OBS_DATABASE_URL` | SQLite file at the repo root | Any SQLAlchemy URL. Compose sets the Postgres service. |
| `OBS_CAPTURE_CONTENT` | `true` | Record prompt and completion text. |
| `OBS_CONTENT_MAX_CHARS` | `2000` | Cap on each stored preview. |
| `OBS_RETENTION_DAYS` | `7` | Traces older than this are deleted. |

The retention sweep piggybacks on span export rather than a scheduler, so there is no cron
job and no extra container, and it does no work when nothing is being traced.

Content capture matters more than it looks: the model-request span carries the *entire*
conversation history, and it grows every turn, so a multi-turn copilot session would write
megabytes per span uncapped. Content is truncated once at ingest and stripped from the raw
attribute blob so it is never stored twice.

## Why self-hosted, and why one database

Langfuse Cloud was the original choice and has been removed. The reason is not that it was
bad — it is that tracing was gated on `LANGFUSE_PUBLIC_KEY`, so with no keys the app
installed no tracer provider at all and every span became a no-op. The feature meant to
prove the system is inspectable was the one feature that required a third-party account.

The earlier rejection of self-hosting was specifically about **Langfuse v3's** topology:
ClickHouse plus Postgres plus Redis plus MinIO, four containers. One Postgres holding one
table is a different proposition, and it costs about 256 MB.

Storage is deliberately dialect-neutral. Postgres in Compose, SQLite everywhere else,
selected by URL alone:

```bash
# No container needed; this is the default outside Docker.
OBS_DATABASE_URL=sqlite+pysqlite:///./traces.db uv run uvicorn app.main:app
```

Nothing filters inside the JSON attribute column, costs are integer micro-USD rather than
`NUMERIC` (which round-trips as `Decimal` on one database and `float` on the other), and
timestamp arithmetic happens in Python because `EXTRACT(EPOCH …)` and `julianday()` are not
the same function. The schema is created at startup; there are no migrations to run.

## Porting to a different agent framework

This is the part the design is built around. Instrumentation is OpenTelemetry, which
Pydantic AI, LangGraph, OpenInference, and the raw provider SDKs all emit, so **the storage,
API, and UI are framework-agnostic**. Only one module knows what framework produced a span.

| File | Framework-coupled? | What to do when swapping frameworks |
|---|---|---|
| `app/observability/ingest.py` | **Yes — the whole point** | Check the constants block at the top; adjust if the new framework uses different attribute keys. |
| `app/observability/store.py` | No | Nothing. |
| `app/observability/exporter.py` | No | Nothing. |
| `app/observability/setup.py` | One line | Replace `Agent.instrument_all(...)` with the new framework's instrumentation call. |
| `app/observability/api.py` | No | Nothing. |
| `frontend/src/components/Traces*.tsx` | No | Nothing. |

Two rules keep it that way, and both are enforced by tests:

1. **Classify on `gen_ai.operation.name`, never on span names.** Operation names are
   OpenTelemetry semantic convention; span names are framework flavour. Pydantic AI has
   already renamed `agent run` → `invoke_agent {name}` and `running tool` →
   `execute_tool {name}` between instrumentation v2 and v5. Name-based classification would
   have silently broken; `test_span_categories_come_from_the_operation_name_not_the_span_name`
   pins the rule, and `test_real_pydantic_ai_spans_are_classified` drives a real agent
   through a real tracer so an upgrade fails the suite rather than the dashboard.
2. **No `gen_ai.` string outside `ingest.py`.**
   `test_framework_specific_attribute_keys_stay_in_the_ingest_module` asserts it by reading
   the source. Without that, framework coupling leaks outward and the next swap becomes a
   hunt instead of a one-file edit.

If a future framework emits no OpenTelemetry at all, the seam still holds: write spans by
hand with the OTel API and everything downstream is unchanged.

## Costs

Costs are not maintained here. Pydantic AI computes `operation.cost` per model request using
`genai-prices` — already an installed dependency, shipping an offline price snapshot, priced
at the request timestamp so historical rows stay correct. The ingest layer reads that value
and converts it to integer micro-USD. A hand-written price table would go stale silently and
produce confidently-wrong numbers, which is the worst possible failure for a cost view. An
unpriced model reads as `—`, never `$0.00`.

Token totals come from model-request spans only. The agent-run span repeats its children's
usage under `gen_ai.aggregated_usage.*`; summing both would report exactly double.

## Known limits

- **Batch loss is silent by design.** OpenTelemetry's batch processor removes spans from its
  queue before handing them to the exporter, so a database blip loses that batch. Traces are
  diagnostics, not the product; the exporter logs a rate-limited warning with a dropped-batch
  counter rather than retrying and blocking the writer.
- **No migrations.** The schema is created with `create_all` at startup. Adding a column
  later means an `ALTER TABLE` or dropping the volume.
- **Single process.** There is no trace context propagation across services because there is
  only one API process.
