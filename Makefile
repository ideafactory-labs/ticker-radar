.DEFAULT_GOAL := help
SHELL := bash
.SHELLFLAGS := -euo pipefail -c

.PHONY: help test lint unit e2e fixtures-data serve probe up down verify evidence

help: ## Show targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

test: lint unit e2e ## Project checks (CI's required gate) — offline, no network

lint: ## Workflow policy + Python syntax
	python3 .factory/check_workflows.py .github/workflows
	python3 -m py_compile radar/*.py tests/*.py
	node --check site/app.js

unit: ## Parsers and a full build against recorded fixtures
	python3 -m unittest tests/test_radar.py tests/test_macro.py -v 2>&1 | tail -n 25

fixtures-data: ## Build site/data from recorded fixtures (what tests and `make serve` use)
	python3 -c "import shutil; shutil.rmtree('site/data', ignore_errors=True)"
	python3 -m radar.build --out site/data --fixtures tests/fixtures --today 2026-11-25 >/dev/null

e2e: fixtures-data ## Browser checks in headless Chromium against fixture data
	node tests/e2e.mjs

serve: fixtures-data ## Look at the page locally with fixture data: http://127.0.0.1:8000/?today=2026-11-25
	node tests/serve.mjs

probe: ## Fetch every live source once and print what worked (needs open internet: run in CI)
	RADAR_SAMPLE=1 python3 -m radar.build --out build/probe

up: ## Start paid resources (live workflow only)
	@echo "cloud: none — this project has no paid resources; nothing to start"

down: ## Tear down and prove nothing tagged remains
	@echo "cloud: none — nothing was ever created, so nothing tagged Project=ticker-radar can remain"

verify: ## Browser checks against the deployed page (VERIFY_URL to override)
	BASE_URL="$${VERIFY_URL:-https://ideafactory-labs.github.io/ticker-radar/}" EVIDENCE_DIR=evidence/verify node tests/e2e.mjs

evidence: ## Screenshots and results into evidence/
	EVIDENCE_DIR=evidence $(MAKE) e2e
