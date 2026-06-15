.DEFAULT_GOAL := help
PY ?= python3
VENV := .venv
BIN := $(VENV)/bin

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
	  awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

$(VENV): ## Create the virtualenv
	$(PY) -m venv $(VENV)

.PHONY: setup
setup: $(VENV) ## Create venv + install (editable, with dev deps)
	$(BIN)/pip install -U pip
	$(BIN)/pip install -e ".[dev]"
	@test -f .env || cp .env.example .env
	@echo "✓ setup complete. Edit .env, then 'make db' and 'make run'."

.PHONY: db
db: ## Start Postgres (docker) for the engine
	docker compose up -d db

.PHONY: run
run: ## Run the autonomy loop (forge run); pass GOAL="..." for a new goal
	$(BIN)/forge run $(if $(GOAL),--goal "$(GOAL)",) $(if $(PROJECT),--project $(PROJECT),)

.PHONY: serve
serve: ## Serve the control-plane API (:8787)
	$(BIN)/forge serve

.PHONY: gui
gui: ## Run the web console dev server (proxies /api -> control plane)
	cd web && npm install && npm run dev

.PHONY: test
test: ## Run the test suite (incl. regression)
	$(BIN)/pytest

.PHONY: fmt
fmt: ## Lint/format with ruff
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

.PHONY: config
config: ## Print the resolved configuration
	$(BIN)/forge config

.PHONY: clean
clean: ## Remove caches and the venv
	rm -rf $(VENV) .pytest_cache .ruff_cache **/__pycache__
