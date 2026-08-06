# Entry point for the Docker stack. `make up` builds, waits until every
# service is actually serving, then prints the URLs to open.

COMPOSE ?= docker compose
WEB_URL ?= http://localhost:5173
API_URL ?= http://localhost:8000
NEO4J_URL ?= http://localhost:7474
# The web probe polls every 2s; 90 attempts is a 3-minute ceiling, generous
# enough for a cold first build on a slow machine.
WAIT_ATTEMPTS ?= 90
RULE := ---------------------------------------------------------------

.DEFAULT_GOAL := help
.PHONY: help up down logs restart rebuild test banner

help:  ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*## "}{printf "  make %-9s %s\n", $$1, $$2}'

.env:
	@cp .env.example .env
	@echo "  created .env from .env.example -- add ANTHROPIC_API_KEY to enable the AI features"

up: .env  ## Build and start the full stack, then print what to open
	@echo "  building images and starting services"
	@echo "  (the first run pulls Neo4j and downloads the ~130 MB embedding model)"
	@$(COMPOSE) up -d --build
	@$(MAKE) --no-print-directory banner

# `up -d` returns once api is healthy (web depends on that), but web itself
# may still be booting Vite -- so poll the page a user would actually open.
banner:
	@printf '  waiting for the web app'
	@ok=0; for i in $$(seq 1 $(WAIT_ATTEMPTS)); do \
		if curl -fs -o /dev/null $(WEB_URL); then ok=1; break; fi; \
		printf '.'; sleep 2; \
	done; \
	echo; \
	if [ "$$ok" -eq 0 ]; then \
		echo "  web app never answered on $(WEB_URL) -- check '$(COMPOSE) logs web'"; \
		exit 1; \
	fi
	@health=$$(curl -fsS $(API_URL)/api/health 2>/dev/null); \
	field() { printf '%s' "$$health" \
		| sed -n "s/.*\"$$1\":[[:space:]]*\"\\{0,1\\}\\([^,}\"]*\\).*/\\1/p"; }; \
	pw=$$(sed -n 's/^NEO4J_PASSWORD=//p' .env 2>/dev/null); \
	pw=$${pw:-password}; \
	if [ "$$(field status)" = "ok" ]; then \
		graph="$$(field exercises) exercises, $$(field members) member(s) loaded"; \
	else \
		graph="NOT loaded -- check '$(COMPOSE) logs kg-build'"; \
	fi; \
	if [ "$$(field ai_enabled)" = "true" ]; then \
		ai="enabled ($$(field model))"; \
	else \
		ai="off -- set ANTHROPIC_API_KEY in .env, then 'make restart'"; \
	fi; \
	echo; \
	echo "  $(RULE)"; \
	echo "   Knowledge-Graph Coach Dashboard is up"; \
	echo "  $(RULE)"; \
	echo "   Web app        $(WEB_URL)   <- open this"; \
	echo "   API docs       $(API_URL)/docs"; \
	echo "   Neo4j Browser  $(NEO4J_URL)   (neo4j / $$pw)"; \
	echo; \
	echo "   Graph          $$graph"; \
	echo "   AI features    $$ai"; \
	echo; \
	echo "   Logs  make logs     Stop  make down"; \
	echo "  $(RULE)"; \
	echo

down:  ## Stop the stack (the graph survives in the neo4j-data volume)
	@$(COMPOSE) down

restart:  ## Restart api and web to pick up .env changes
	@$(COMPOSE) up -d --force-recreate api web
	@$(MAKE) --no-print-directory banner

logs:  ## Follow logs for all services
	@$(COMPOSE) logs -f

rebuild:  ## Re-run the knowledge-graph build against the running Neo4j
	@$(COMPOSE) run --rm kg-build

test:  ## Run backend tests and the frontend type-check + lint
	@cd backend && uv run pytest
	@cd frontend && npm run build && npm run lint
