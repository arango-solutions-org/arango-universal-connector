# Developer shortcuts. Run `make` or `make help` to list targets.
#
# Works with Docker or Podman: CONTAINER is auto-detected (Docker preferred) and can be
# overridden, e.g. `make test-integration CONTAINER=podman`.

.DEFAULT_GOAL := help
SHELL := /bin/bash

CONTAINER ?= $(shell command -v docker >/dev/null 2>&1 && echo docker || (command -v podman >/dev/null 2>&1 && echo podman))
COMPOSE ?= $(CONTAINER) compose
IMAGE ?= arango-connector:dev

# Disposable ArangoDB for integration tests. Kept off 8529 so it never touches the
# Compose database or its volume.
TEST_DB_NAME ?= arango-connector-test-db
TEST_DB_PORT ?= 18529
TEST_DB_PASSWORD ?= test-password
TEST_DB_IMAGE ?= docker.io/arangodb/arangodb:3.12
TEST_DB_URL := http://localhost:$(TEST_DB_PORT)
TEST_DB_ENV := ARANGO_HOSTS=$(TEST_DB_URL) ARANGO_PASSWORD=$(TEST_DB_PASSWORD) ARANGO_USERNAME=root

.PHONY: help install lint format format-check typecheck test test-integration test-all check \
	test-db-up test-db-down run build up down logs reconcile-expiry gmail-auth clean \
	docs-gen docs-check wiki-build protos link-image link-e2e platform-up platform-down platform-status platform-forward \
	platform-load protos-check helm-lint shellcheck

help: ## Show this help
	@echo "Usage: make <target> [VAR=value]"
	@echo
	@awk 'BEGIN {FS = ":.*## "} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo
	@echo "Container runtime: $(if $(CONTAINER),$(CONTAINER),none found (install Docker or Podman))"

# -- development ----------------------------------------------------------

install: ## Install dependencies (incl. dev tools) with uv
	uv sync

lint: ## Run ruff lint checks
	uv run ruff check .

format: ## Format code and apply safe lint fixes
	uv run ruff check --fix .
	uv run ruff format .

format-check: ## Check formatting without changing files
	uv run ruff format --check .

typecheck: ## Run mypy
	uv run mypy

test: ## Run unit tests (no database needed)
	uv run pytest

test-integration: test-db-up ## Run integration tests against a disposable ArangoDB
	$(TEST_DB_ENV) uv run pytest -m integration

test-all: test test-integration ## Run unit and integration tests

check: lint format-check typecheck docs-check test ## Everything CI should pass (no database needed)

# -- links ----------------------------------------------------------------

protos: ## Regenerate the Links gRPC stubs from packages/links/proto
	uv run python tools/gen_protos.py

protos-check: protos ## Fail if the committed gRPC stubs differ from the protos
	git diff --exit-code -- packages/links/src/arango_connector_links/_pb

helm-lint: ## Lint the link chart for every link
	for link in gmail slack jira selftest; do \
		helm lint charts/arango-connector-link --strict --set link=$$link \
			--set arangodb_platform.deployment.name=deployment || exit 1; \
	done

shellcheck: ## Lint the dev scripts
	shellcheck dev/*.sh

link-image: _require-container ## Build the Links image (arango-connector-link:dev)
	$(CONTAINER) build -f packages/links/Dockerfile -t arango-connector-link:dev .

link-e2e: ## End-to-end: run a job on the local platform through the selftest link
	dev/link-e2e.sh

# -- docs -----------------------------------------------------------------

docs-gen: ## Regenerate config tables and the source catalogue in the docs from code
	uv run python tools/doctools.py gen

docs-check: ## Fail on stale generated docs, .env.example gaps or broken links
	uv run python tools/doctools.py check

wiki-build: ## Render the docs as GitHub wiki pages into build/wiki (what CI publishes)
	rm -rf build/wiki
	uv run python tools/doctools.py wiki build/wiki --sha "$$(git rev-parse HEAD)"

# -- disposable test database ---------------------------------------------

test-db-up: _require-container ## Start the disposable test ArangoDB and wait until ready
	@if [ -z "$$($(CONTAINER) ps -q --filter name=^$(TEST_DB_NAME)$$)" ]; then \
		$(CONTAINER) rm -f $(TEST_DB_NAME) >/dev/null 2>&1 || true; \
		echo "Starting $(TEST_DB_NAME) on port $(TEST_DB_PORT)"; \
		$(CONTAINER) run -d --name $(TEST_DB_NAME) -p $(TEST_DB_PORT):8529 \
			-e ARANGO_ROOT_PASSWORD=$(TEST_DB_PASSWORD) $(TEST_DB_IMAGE) >/dev/null; \
	fi
	@for i in $$(seq 1 60); do \
		curl -fs -o /dev/null $(TEST_DB_URL)/_admin/server/availability && echo "ArangoDB ready at $(TEST_DB_URL)" && exit 0; \
		sleep 1; \
	done; \
	echo "ArangoDB did not become ready in 60s" >&2; exit 1

test-db-down: _require-container ## Stop and remove the disposable test ArangoDB
	-$(CONTAINER) rm -f $(TEST_DB_NAME)

# -- local ArangoDB Platform (for Links) -----------------------------------
# kind + Podman cluster with the Enterprise operator, ArangoDB and platform storage.
# Needs ARANGO_LICENSE_KEY; see dev/platform.sh.

platform-up: ## Create the local Links test platform (kind + Podman; needs ARANGO_LICENSE_KEY)
	dev/platform.sh up

platform-down: ## Delete the local platform cluster
	dev/platform.sh down

platform-status: ## Show the local platform's resources
	dev/platform.sh status

platform-forward: ## Port-forward the platform's ArangoDB to https://localhost:8529
	dev/platform.sh forward

platform-load: ## Load a local image into the platform cluster (IMAGE=name:tag)
	dev/platform.sh load $(IMAGE)

# -- running --------------------------------------------------------------

run: ## Run the connector locally (reads .env)
	uv run python -m arango_connector_standalone.main

build: _require-container ## Build the connector image
	$(CONTAINER) build -f packages/standalone/Dockerfile -t $(IMAGE) .

up: _require-container ## Start ArangoDB and the Slack connector with Compose
	$(COMPOSE) --profile slack up --build -d

down: _require-container ## Stop the Compose stack, every source (keeps data volumes)
	$(COMPOSE) --profile slack --profile gmail --profile jira down

logs: _require-container ## Follow the connector's Compose logs
	$(COMPOSE) logs -f connector

reconcile-expiry: ## Apply RETENTION_DAYS to stored documents (ARGS="--dry-run")
	uv run arango-connector-reconcile-expiry $(ARGS)

GMAIL_CLIENT_SECRETS ?= secrets/gmail/client.json
GMAIL_TOKEN_DIR ?= secrets/gmail

gmail-auth: ## Authorise a mailbox for Gmail OAuth mode (MAILBOX=you@example.com)
	@if [ -z "$(MAILBOX)" ]; then echo "Usage: make gmail-auth MAILBOX=you@example.com" >&2; exit 1; fi
	@if [ ! -f "$(GMAIL_CLIENT_SECRETS)" ]; then \
		echo "OAuth client JSON not found at $(GMAIL_CLIENT_SECRETS); see docs/sources/gmail.md#setup" >&2; exit 1; \
	fi
	uv run arango-connector-gmail-auth --client-secrets $(GMAIL_CLIENT_SECRETS) \
		--mailbox $(MAILBOX) --out-dir $(GMAIL_TOKEN_DIR) $(ARGS)

# -- housekeeping ---------------------------------------------------------

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +

_require-container:
	@if [ -z "$(CONTAINER)" ]; then echo "Docker or Podman is required for this target" >&2; exit 1; fi
