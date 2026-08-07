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
- Memory is capped per service (~1.5 GB total); Neo4j heap/pagecache are pinned deliberately,
  and the trace-store Postgres is held to 256 MB.
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

- `backend/` — FastAPI, managed with uv. `app/main.py` mounts routers; Neo4j async driver and
  the trace store live on `app.state` via lifespan.
- `backend/app/observability/` — local trace store for LLM/tool/graph calls. `ingest.py` is
  the framework seam: it is the only module allowed to reference `gen_ai.*` attribute keys,
  and a test enforces that. Classify spans on `gen_ai.operation.name`, never on span names —
  Pydantic AI has already renamed them once. See `docs/observability.md` before changing it.
- `frontend/` — React 19 + Vite + TypeScript, Tailwind v4 + shadcn/ui, `@/` aliases `src/`.
- `data/` — source datasets (`exercises.json`, `member-context.json`) and curated ontology
  mappings under `data/ontology/`.
- `docs/` — stack decisions and the KG 1 schema (`kg1-schema.md`). Read `data-overview.md`
  before touching ingest: it catalogs the dataset's quirks.

## Conventions

- GitHub work goes to `Josie29/knowledge-graph-coach-dashboard` (`origin`) only. Never push,
  file issues, or open PRs on `upstream` (the assessment org's repo).

## New feature workflow

Never build a feature on `main` — work in an isolated worktree and land it through a PR:

1. **Branch in a worktree.** Create the feature branch in a git worktree outside the `main`
   checkout — never build the feature on `main` itself.
2. **Open a PR into `main`.** The worktree branch ends in a pull request for a human to
   review and approve; do not merge it yourself.
3. **Clean up after merge.** Once merged, remove the worktree (`git worktree remove`) and
   delete the feature branch, locally and on `origin`.
