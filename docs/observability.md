# Observability

Every LLM call, tool call, and Neo4j query the API makes is recorded as an OpenTelemetry
span and written to a local SQL database. The **Traces** tab in the web app reads it.

No account, no API key, no SaaS. `make up` is enough.

## What you see

One API request is one trace. `POST /api/workout` produces a single trace containing:

```
POST /api/workout                                                    9.49 s
├─ resolve "patellofemoral pain" -> cond_pfps (exact 1.00)            82 ms
│  └─ MATCH (c:Concept) WHERE toLower(c.pref_label) = $q ...          81 ms
├─ resolve "burpees" -> mp_cardio_plyometric (fulltext 6.31)         125 ms
│  ├─ MATCH (c:Concept) WHERE toLower(c.pref_label) = $q ...           3 ms
│  └─ CALL db.index.fulltext.queryNodes('concept_text', $q) ...      121 ms
├─ invoke_agent constraint-extractor                                 2.57 s
│  └─ chat claude-haiku-4-5                          2.57 s  $0.0012
├─ safety pool -> 21 of 50 exercises (29 excluded)                   134 ms
│  ├─ MATCH (e:Exercise) RETURN e{.*} AS exercise ...                 81 ms
│  ├─ MATCH (r:SafetyRule)-[:CONTRAINDICATED_FOR]->(c:Condition) ...  19 ms
│  └─ MATCH (c:Condition {id: $condition_id})-[:ANCHORED_AT]-> ...    23 ms
└─ invoke_agent workout-planner                                      6.20 s
   └─ chat claude-haiku-4-5                          6.19 s  $0.0067
```

Two layers of naming make that readable, and both exist because **span names are
identifiers, not descriptions**: every resolver step is named `resolve_concepts` and every
graph query is named `neo4j.query`. So a row shows the summary the backend recorded —
the step's outcome where there is one, otherwise the statement it ran.

The names are deliberately *not* made descriptive at the source. The sampler's drop-list
keys on the name `neo4j.query` to discard health-check queries, so renaming spans would
silently stop that filtering.

A trace is named by **what ran**, not by what was requested: the row above reads
`constraint-extractor → workout-planner`, with the route as a secondary line. The agents come
from the trace's spans, not its root span — the root is the HTTP request, which has no agent.

The deterministic tool layer records its own steps through `traced_operation`, which is what
groups a resolver term's up-to-four passes under one row. Adding a step elsewhere is three
lines:

```python
with traced_operation("safe_exercise_pool") as operation:
    result = await _safe_exercise_pool(driver, constraints)
    operation.describe(f"safety pool -> {len(result.included)} of 50 exercises")
```

Expanding a span shows its truncated prompt and completion, its remaining attributes, and
any error. That is what makes a bad plan debuggable after the fact rather than by re-running
it with print statements.

The list defaults to **AI runs** — traces containing at least one model request — with
**All requests** and **Errors** alongside. Graph-only requests (member fetches, the
knowledge-graph explorer) are still traced and still worth reading, because their
`neo4j.query` spans are how you check the resolver and safety traversal; they just outnumber
agent runs many to one, so they are not the default view. Filtering happens in SQL before the
limit, so "the newest 20 errors" means that rather than "errors among the newest 20".

### What is never traced

- **`/api/health`** — `make up` and the Compose healthcheck poll it every five seconds, which
  would bury real traces under roughly 17,000 junk ones a day.
- **`/api/traces*`** — the trace store's own read API. Tracing it is a feedback loop: every
  render of the Traces page would mint two more traces, which inflate the figures on that same
  page and push the runs worth looking at off the list. The more you looked, the less you
  could see. Excluding the telemetry read path is the same reason an exporter never traces its
  own exports.

Both are excluded at the middleware, before a span is created, so the graph queries underneath
them are never recorded either.

The endpoints behind the page are `GET /api/traces?show=ai|all|errors`, with
`/api/traces/{trace_id}` for the span tree and `/api/traces/stats` for the header figures.

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
