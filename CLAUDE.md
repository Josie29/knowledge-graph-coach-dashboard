# Knowledge-Graph Coach Dashboard

Take-home build: a knowledge graph over exercise data + clinical ontologies, with a coach
dashboard on top (AI workout generator + member-context copilot).

## Run the full stack

```bash
cp .env.example .env   # first time only; ANTHROPIC_API_KEY needed for AI features
make up                # Neo4j + kg-build + API + web, then prints the URLs to open
```

- Web: http://localhost:5173 · API: http://localhost:8000 · Neo4j Browser: http://localhost:7474
- `make up` is a thin wrapper over `docker compose up -d --build`: it polls the web app,
  then prints a banner sourced from `GET /api/health` (graph counts, whether the AI
  features are configured). `make down` / `logs` / `restart` / `rebuild` / `test` also exist.
- Keep `/api/health` and the `banner` target in sync — the Makefile parses that JSON.
- Data persists in the `neo4j-data` volume.
- Memory is capped per service (~1.2 GB total); Neo4j heap/pagecache are pinned deliberately.
- `api` and `web` have healthchecks, so `web` only starts once the API answers.

## Run pieces without Docker

```bash
cd backend && uv run uvicorn app.main:app --reload   # API on :8000
cd frontend && npm run dev                           # web on :5173, proxies /api
cd backend && uv run python ../scripts/build_kg.py   # (re)build the knowledge graph
```

Neo4j still needs Docker: `docker compose up -d neo4j`. In Docker the `kg-build` one-shot
service runs the build automatically before the API starts. `build_kg.py --help` lists the
offline flags (`--dry-run`, `--skip-embeddings`, `--emit-ttl`, `--reset`).

## Checks

```bash
cd backend && uv run pytest      # backend tests live in backend/tests/
cd frontend && npm run build     # type-check + build
cd frontend && npm run lint
```

After changing backend Pydantic response models, regenerate the frontend API types:

```bash
cd backend && uv run python ../scripts/export_openapi.py ../frontend/openapi.json
cd frontend && npm run gen:api   # openapi.json -> src/lib/api-types.ts
```

## Layout

- `backend/` — FastAPI, managed with uv. `app/main.py` mounts routers; Neo4j async driver lives
  on `app.state` via lifespan.
- `frontend/` — React 19 + Vite + TypeScript, Tailwind v4 + shadcn/ui, `@/` aliases `src/`.
- `data/` — source datasets (`exercises.json`, `member-context.json`) and curated ontology
  mappings under `data/ontology/`.
- `docs/` — stack decisions and the KG 1 schema (`kg1-schema.md`). Read `data-overview.md`
  before touching ingest: it catalogs the dataset's quirks.

## Conventions

- GitHub work goes to `Josie29/knowledge-graph-coach-dashboard` (`origin`) only. Never push,
  file issues, or open PRs on `upstream` (the assessment org's repo).
- Work is tracked as issues #1-#14; branch `feature/<kebab-description>`, PR against `main`,
  squash merge.
- LLM calls go through Pydantic AI; typed agent outputs are the API contract. The model is a
  single switch point — `ANTHROPIC_MODEL` in `.env`, read via `settings.anthropic_model`.
  Default is `claude-haiku-4-5` to keep API spend low while validating; switch to
  `claude-opus-5` for demo and final runs. Never hardcode a model ID at a call site.
- Haiku 4.5 does not support the `effort` parameter or adaptive thinking — code that sets
  either must branch on the configured model, so keep those calls out of shared paths until
  the model is pinned.
