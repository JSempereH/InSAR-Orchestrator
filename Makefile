.DEFAULT_GOAL := help

BACKEND_DIR   := backend
FRONTEND_DIR  := frontend

BACKEND_PORT  ?= 8000
FRONTEND_PORT ?= 5173

.PHONY: help install backend frontend dev \
        test-backend test-insar-core test-all \
        lint docker-build docker-up docker-down backup-db clean

help: ## Show this help
	@echo "Available targets:"
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

## --- Setup ------------------------------------------------------------------

install: ## Create backend/.venv, install backend+insar_core+frontend deps
	./setup.sh

## --- Run ---------------------------------------------------------------------

backend: ## Run the FastAPI backend (:8000)
	cd $(BACKEND_DIR) && .venv/bin/uvicorn app.main:app --reload --host 0.0.0.0 --port $(BACKEND_PORT)

frontend: ## Run the Vite frontend (:5173)
	npm run dev --prefix $(FRONTEND_DIR) -- --port $(FRONTEND_PORT)

dev: ## Run backend + frontend together (Ctrl+C stops both)
	./dev.sh

## --- Tests --------------------------------------------------------------------

test-backend: ## Run the backend test suite
	cd $(BACKEND_DIR) && .venv/bin/pytest

test-insar-core: ## Run the insar_core (shared) test suite
	$(BACKEND_DIR)/.venv/bin/python -m pytest packages/insar_core/tests -q

test-all: test-backend test-insar-core ## Run every test suite in this repo

## --- Lint -----------------------------------------------------------------

lint: ## Run ruff and eslint
	uv run --with ruff ruff check .
	npm run lint --prefix $(FRONTEND_DIR)

## --- Docker (alternative to bare venvs; see docker-compose.yml) ------------

docker-build: ## Build backend/frontend container images
	docker compose build

docker-up: ## Run backend + frontend in containers (:8000/:5173)
	docker compose up

docker-down: ## Stop and remove the containers started by docker-up
	docker compose down

## --- Housekeeping ----------------------------------------------------------

backup-db: ## Snapshot the backend's SQLite database with a timestamp
	@mkdir -p backups
	@ts=$$(date +%Y%m%d-%H%M%S); \
	cp $(BACKEND_DIR)/insar_app.db backups/insar_app-$$ts.db && \
	echo "Wrote backups/insar_app-$$ts.db"

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -not -path "*/.venv/*" -not -path "*/node_modules/*" -exec rm -rf {} +
	rm -rf .pytest_cache
