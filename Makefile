.DEFAULT_GOAL := help

BACKEND_DIR   := backend
FRONTEND_DIR  := frontend

BACKEND_PORT  ?= 8000
FRONTEND_PORT ?= 5173
DOCS_PORT     ?= 8001

.PHONY: help install backend frontend dev \
        test-backend test-insar-core test-all \
        docs docs-assets docs-site docs-serve docs-install \
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

## --- Docs -------------------------------------------------------------------
# Screenshots come from the running app (started here if it is not), so the app
# needs some data. The animation needs ffmpeg and Google Chrome. See docs/maintaining-docs.md.

docs/node_modules: docs/package.json
	npm install --prefix docs
	@touch docs/node_modules

docs-install: docs/node_modules ## Install the tooling that generates the docs assets

docs-assets: docs/node_modules ## Regenerate screenshots, figures and the animation (ONLY=screenshots,figures,animation to pick)
	node docs/build.mjs $(if $(ONLY),--only=$(ONLY))

docs-site: docs/node_modules ## Build the docs site into docs/_site (the same one GitHub Pages publishes)
	node docs/site.mjs

docs-serve: docs-site ## Preview the docs site locally
	@echo "Docs site: http://localhost:$(DOCS_PORT)/   (Ctrl+C to stop)"
	@(sleep 1; xdg-open http://localhost:$(DOCS_PORT)/ >/dev/null 2>&1 || true) &
	python3 -m http.server $(DOCS_PORT) --directory docs/_site --bind 127.0.0.1

docs: docs-assets docs-serve ## Regenerate every docs asset, then preview the docs site locally

## --- Lint -----------------------------------------------------------------

lint: ## Run ruff and eslint
	uv run --with ruff ruff check .
	npm run lint --prefix $(FRONTEND_DIR)

## --- Container (one image with the app, MintPy and rclone; see docs/deploy.md) ---

docker-build: ## Build the container image
	docker compose build

docker-up: ## Build and run the app in a container (http://localhost:8000)
	docker compose up -d --build

docker-down: ## Stop and remove the container
	docker compose down

## --- Housekeeping ----------------------------------------------------------

backup-db: ## Take a safe snapshot of the database into backups/ (works while the app runs)
	cd $(BACKEND_DIR) && .venv/bin/python -m app.backup ../backups

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -not -path "*/.venv/*" -not -path "*/node_modules/*" -exec rm -rf {} +
	rm -rf .pytest_cache
