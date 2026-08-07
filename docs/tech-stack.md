# Tech Stack — Knowledge-Graph Backed Coach Dashboard

Full-stack decisions for the [ASSESSMENT.md](../ASSESSMENT.md) build. The two deep-dive decisions
are documented separately and summarized here:
[knowledge-graph-options.md](./knowledge-graph-options.md) ·
[agent-framework-options.md](./agent-framework-options.md).

| Layer | Component | Choice | Reason |
|---|---|---|---|
| Data | Graph store | Neo4j Community (Docker) | Cypher path serializes straight into the provenance trace; full-text + vector indexes cover all three resolver passes in one store — see [KG doc](./knowledge-graph-options.md) |
| Data | Ontology ingest | rdflib (build-time script) | Parse hand-curated OPE/COPPER/SKOS/PROV-O subsets offline, emit Cypher; no reasoner in the request path |
| Data | Embeddings (resolver pass 3) | fastembed (local ONNX) | $0, no API key, deterministic; ~200 concepts embed in milliseconds, no torch dependency |
| AI | Agent framework | Pydantic AI | Typed `WorkoutPlan`/`CopilotAnswer` outputs are the API contract; DI injects the Neo4j driver into a deterministic tool layer — see [agent doc](./agent-framework-options.md) |
| AI | LLM | Claude Opus 5 (`claude-opus-5`), effort low/medium | Low/medium effort is unusually strong on this model — main lever for the ~5s target; prompt caching on the ontology preamble |
| AI | Observability | Self-hosted trace store (Postgres) via OpenTelemetry | Pydantic AI emits OTel natively, so the sink is a swap, not an integration; one local Postgres means tracing works with no account and no keys — see [observability doc](./observability.md) |
| Backend | API framework | FastAPI | Pydantic models double as response schemas — one type from agent output to browser; async fits streaming |
| Backend | Python tooling | uv | Single lockfile, fast installs, `uv run` keeps the one-command story honest |
| Frontend | Framework | React 19 + Vite + TypeScript | SPA is all the dashboard needs; Vite dev server proxies to FastAPI; TS types generated from the Pydantic schemas |
| Frontend | Chat/streaming UI | Vercel AI Elements over Pydantic AI's AG-UI adapter | Copilot panel (messages, streaming, history) nearly free; adapter ships with the chosen framework |
| Frontend | Charts | Recharts | Declarative JSON-shaped props match agent-emitted chart specs; adherence/sleep trends are simple line/bar charts |
| Frontend | Styling | Tailwind CSS v4 + shadcn/ui | Fastest path to a credible dashboard in a day; components are copied in, not a runtime dependency |
| Frontend | Auth | Mock session (hardcoded coach) | Spec explicitly allows mock auth; real auth adds zero rubric value |
| Testing | Backend tests | pytest | Resolver + safety filter (the two required tests) are plain Python tools — no LLM mocking needed |
| Infra | Local runtime | Docker Compose (Neo4j + API + web) | One `docker compose up` = the "runs with one command" rubric line |

## Rejected alternatives

| Component | Option | Why not |
|---|---|---|
| Graph store | rdflib + Oxigraph | No vector index, weak viz; SPARQL demos worse than Cypher — full analysis in [KG doc](./knowledge-graph-options.md) |
| Graph store | Graphiti | Extracted, opinionated schema fights a hand-authored, auditable KG 1 |
| Graph store | LightRAG / MS GraphRAG | Built for text corpora, not curated ontologies — "semantic search with extra steps" |
| Embeddings | Voyage / OpenAI API | Second provider key + billing setup for 200 short strings; local is free and reproducible |
| Agent framework | LangGraph | Durability/checkpointing/interrupts all unused at sub-5s single-turn — full analysis in [agent doc](./agent-framework-options.md) |
| Agent framework | Mastra (single Next.js repo) | Flips ontology ingest to TypeScript, losing rdflib for SKOS/PROV-O |
| Agent framework | Anthropic SDK direct | Viable fallback, but hand-rolls streaming, tracing, and eval scaffolding a framework provides |
| LLM | Claude Sonnet 5 | Fallback if Opus latency misses ~5s; Opus at low effort is stronger per token here |
| Observability | LangSmith, Langfuse Cloud, Logfire | All are OTel sinks, so none is load-bearing; each makes the one feature that proves the system is inspectable depend on a third-party account. Self-hosting one Postgres costs ~256 MB and works out of the box |
| API framework | Next.js API routes | Only makes sense in the rejected all-TS Mastra path |
| Frontend framework | Next.js | SSR/routing machinery unused in a single-page coach dashboard; Vite is lighter and faster to iterate |
| Charts | D3 | Too low-level for three simple trend charts in a day |
| Charts | Chart.js | Imperative canvas API fits React worse than Recharts' declarative components |
| Styling | MUI / Chakra | Heavier runtime and theming ceremony than the day allows |
| Python tooling | pip + venv / Poetry | Slower, more setup steps; uv subsumes both |

## Open sub-decisions

- **Hosted deploy vs. local-only repo** — local-only satisfies the deliverable; a hosted demo
  (Railway/Vercel) would push the graph store toward Aura Free and needs its node limit verified
  in-console first (flagged in [KG doc](./knowledge-graph-options.md)).
- **Frontend component tests** — Vitest + React Testing Library if time allows; the two required
  tests are backend-only, so this is a nice-to-have.
- **Hosted vs. self-hosted tracing** — **resolved: self-hosted, one Postgres** (issue #13,
  revised). The earlier resolution was Langfuse's cloud free tier, on the grounds that Langfuse
  v3 self-hosting needs ClickHouse + Postgres + Redis + MinIO — four containers that blow this
  repo's memory budget. That argument holds against *Langfuse's* topology and not against
  self-hosting generally: one Postgres with one table costs ~256 MB. What forced the revision is
  that cloud tracing was gated on `LANGFUSE_*` keys, so a reviewer without an account got no
  tracer provider at all and every span became a no-op — the feature meant to prove the system is
  inspectable was the one feature requiring a third-party signup. Tracing is now on by default
  with nothing to configure, and because instrumentation stays OpenTelemetry the sink remains
  swappable: `OBS_DATABASE_URL` alone switches Postgres for SQLite. Details and the
  framework-porting guide in [observability doc](./observability.md).
