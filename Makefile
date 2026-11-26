.DEFAULT_GOAL := help
SHELL := bash
.SHELLFLAGS := -euo pipefail -c

.PHONY: help test lint unit probe up down verify evidence

help: ## Show targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

test: lint unit ## Project checks (CI's required gate) — offline, no network

lint: ## Workflow policy + Python syntax
	python3 .factory/check_workflows.py .github/workflows
	python3 -m py_compile radar/*.py tests/*.py

unit: ## Parsers and a full build against recorded fixtures
	python3 -m unittest tests/test_radar.py -v 2>&1 | tail -n 25

probe: ## Fetch every live source once and print what worked (needs open internet: run in CI)
	RADAR_SAMPLE=1 python3 -m radar.build --out build/probe

up: ## Start paid resources (live workflow only)
	@echo "cloud: none — this project has no paid resources; nothing to start"

down: ## Tear down and prove nothing tagged remains
	@echo "cloud: none — nothing was ever created, so nothing tagged Project=ticker-radar can remain"

verify: ## The runtime check from the idea's "How we'll know it works" (defined with the page)
	@echo "not defined yet — arrives with the page PR"; exit 1

evidence: ## Capture proof for the PR (defined with the page)
	@echo "not defined yet — arrives with the page PR"; exit 1
