# AI Customer Assistant — Makefile (Windows / macOS / Linux)
#
# Works with GNU Make + Git-Bash `sh` on Windows, plain `sh` elsewhere.
# Host-side targets (uvicorn/worker/ingest/migrate/test) talk to Postgres
# forwarded at localhost:5433 (see docker-compose.yml "5433:5432").
# Inside docker, the backend uses POSTGRES_HOST=postgres:5432 instead —
# so these defaults must stay host-side; never copy them into backend/.env.
SHELL := sh
.SHELLFLAGS := -eu -c

# Default environment variables. Plain "=" (not "?=") so an ambient
# POSTGRES_HOST/POSTGRES_PORT exported in the shell (e.g. from `source
# backend/.env`, which contains docker-internal values) can't leak into
# host-side targets. Override explicitly if needed:
#   make ingest URL=... POSTGRES_HOST=postgres POSTGRES_PORT=5432
POSTGRES_HOST = localhost
POSTGRES_PORT = 5433
APP_PORT ?= 8000
DEV_PORT = 8002
URL ?=
SITE ?=
USER_ID ?=
SERVICE ?=
DEFAULT_URL ?= https://alpiniststudios.com/app-prototype-a-complete-guide/
DEFAULT_USER_ID ?= 00000000-0000-0000-0000-000000000000

# Browser opener: Windows -> powershell start; Linux -> xdg-open; macOS -> open.
ifeq ($(OS),Windows_NT)
OPEN = powershell -c start
else
OPEN ?= $(shell command -v xdg-open >/dev/null 2>&1 && echo xdg-open || echo open)
endif

.PHONY: help setup up down status logs build rebuild migrate migrate-down \
	run uvicorn backend worker ingest verify psql user trunc \
	test test-quick smoke frontend chat graph clean

help: ## Show this help message
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  setup        Check prerequisites and create backend/.env if missing"
	@echo "  up           Bring up full docker stack and check status"
	@echo "  down         Stop docker containers"
	@echo "  status       Check docker container health status"
	@echo "  logs         Follow docker logs (SERVICE=name for one service)"
	@echo "  build        Build the backend docker image"
	@echo "  rebuild      Rebuild backend image and restart stack (picks up code changes)"
	@echo "  migrate      Apply Alembic migrations (host -> localhost:5433)"
	@echo "  migrate-down Roll back one migration"
	@echo "  run          Start the FastAPI local dev server (port 8002)"
	@echo "  uvicorn      Start the FastAPI local dev server (port 8002)"
	@echo "  backend      Alias for uvicorn"
	@echo "  worker       Start the ingestion worker process"
	@echo "  ingest       Crawl and ingest a URL (URL=... SITE=y USER_ID=...)"
	@echo "  verify       Check database records for knowledge sources"
	@echo "  psql         Open psql in the postgres container"
	@echo "  user         Ensure the default service account exists"
	@echo "  trunc        Truncate knowledge base tables (DESTRUCTIVE)"
	@echo "  test         Run the backend test suite"
	@echo "  test-quick   Run fast supervisor wiring tests"
	@echo "  smoke        Health-check docker backend + postgres"
	@echo "  frontend     Open the chat portal in your browser"
	@echo "  chat         Alias for frontend"
	@echo "  graph        Open the knowledge-graph explorer"
	@echo "  clean        Remove python caches (safe, keeps volumes)"

setup: ## Check prerequisites and create backend/.env if missing
	@command -v docker >/dev/null 2>&1 || { echo "ERROR: docker not found"; exit 1; }
	@command -v uv >/dev/null 2>&1 || { echo "ERROR: uv not found"; exit 1; }
	@if [ ! -f backend/.env ]; then \
		echo "Creating backend/.env from backend/.env.example ..."; \
		cp backend/.env.example backend/.env; \
		echo "Edit backend/.env (SMTP_*, JWT_SECRET_KEY, GROQ_API_KEY) then rerun."; \
	else \
		echo "backend/.env present."; \
	fi
	@echo "OK: docker + uv available."

up: ## Bring up full docker stack and check status
	docker compose up -d
	docker compose ps

down: ## Stop docker containers
	docker compose down

status: ## Check docker container health status
	docker compose ps

logs: ## Follow docker logs (SERVICE=name for one service)
	docker compose logs -f --tail=100 $(SERVICE)

build: ## Build the backend docker image
	docker compose build backend

rebuild: ## Rebuild backend image and restart stack (picks up code changes)
	docker compose up -d --build
	docker compose ps

migrate: ## Apply Alembic migrations (host -> localhost:5433)
	cd backend && \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	uv run --env-file .env alembic upgrade head

migrate-down: ## Roll back one migration
	cd backend && \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	uv run --env-file .env alembic downgrade -1

run: uvicorn

uvicorn: ## Start the FastAPI local development server (port 8002)
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	uv run --env-file backend/.env --project backend uvicorn main:app --reload --port $(DEV_PORT) --app-dir backend/src/ai_customer_assistant

backend: ## Alias for uvicorn
	@$(MAKE) uvicorn

worker: ## Start the ingestion worker process
	cd backend && \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	uv run --env-file .env python scripts/run_worker.py

ingest: ## Crawl and ingest a URL (URL=... SITE=y USER_ID=...)
	@if [ -z "$(URL)" ]; then \
		printf "Enter URL to ingest: "; \
		read TARGET_URL; \
	else \
		TARGET_URL="$(URL)"; \
	fi; \
	if [ -z "$(SITE)" ]; then \
		printf "Crawl entire site? [y/N]: "; \
		read SITE_CHOICE; \
	else \
		SITE_CHOICE="$(SITE)"; \
	fi; \
	case "$$SITE_CHOICE" in \
		y|Y|yes|YES) SITE_FLAG="--site" ;; \
		*) SITE_FLAG="" ;; \
	esac; \
	if [ -n "$(USER_ID)" ]; then UPLOADED_BY="$(USER_ID)"; else UPLOADED_BY="$(DEFAULT_USER_ID)"; fi; \
	if [ -z "$$TARGET_URL" ]; then TARGET_URL="$(DEFAULT_URL)"; fi; \
	cd backend && \
	POSTGRES_HOST=$(POSTGRES_HOST) \
	POSTGRES_PORT=$(POSTGRES_PORT) \
	uv run --env-file .env python scripts/crawl_and_ingest.py \
		"$$TARGET_URL" \
		--uploaded-by "$$UPLOADED_BY" \
		$$SITE_FLAG

verify: ## Check database records for knowledge sources
	docker exec -it -e PAGER=cat ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c \
		"SELECT source_id, source_name, source_type, updated_at FROM knowledge_source ORDER BY updated_at DESC;"

psql: ## Open psql in the postgres container
	docker exec -it ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant

user: ## Ensure the default service account exists
	docker exec -i ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c "INSERT INTO app_user (id, email, is_service_account) VALUES ('00000000-0000-0000-0000-000000000000','admin@admin.com', True) ON CONFLICT (id) DO NOTHING;"

trunc: ## Truncate knowledge base tables (DESTRUCTIVE)
	docker exec -i ai-customer-assistant-postgres psql -U ai_assistant -d ai_customer_assistant -c "TRUNCATE TABLE value, attribute, relation, entity, knowledge_source_entity_map, embedding_chunk, knowledge_injection_job, knowledge_source_version, knowledge_source, knowledge_category, app_user RESTART IDENTITY CASCADE;"

test: ## Run the backend test suite
	cd backend && uv run pytest tests

test-quick: ## Run fast supervisor wiring tests
	cd backend && uv run pytest tests/agents/supervisor_agent_test/test_agents_wiring.py -q

smoke: ## Health-check docker backend + postgres
	docker exec ai-customer-assistant-postgres pg_isready -U ai_assistant -d ai_customer_assistant
	curl -f --max-time 15 http://127.0.0.1:$(APP_PORT)/health

frontend: ## Open the AI Customer Assistant web app (chat) in your browser
	$(OPEN) "http://127.0.0.1:$(DEV_PORT)/#/chat"

chat: ## Alias for frontend — open the chat portal
	@$(MAKE) frontend

graph: ## Open the knowledge-graph explorer
	$(OPEN) "http://127.0.0.1:$(DEV_PORT)/#/graph"

clean: ## Remove python caches (safe, keeps volumes)
	rm -rf backend/__pycache__ backend/.pytest_cache backend/src/ai_customer_assistant/__pycache__
	find backend -name "__pycache__" -type d -prune -exec rm -rf {} +
