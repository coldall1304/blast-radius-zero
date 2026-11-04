# Blast Radius Zero — local workflow.
#
# One command to run the whole system on a laptop, no cloud account:
#
#     make demo     # docker compose up (MOCK_INFERENCE=1, no API key), wait for health
#     make lab      # seven live containment probes through the real sandbox path
#     make verify   # image -> unit tests -> integration tests -> containment lab
#     make down     # stop and remove the stack
#
# `make test*` and `make lab` use .venv/bin/python when it exists, else python3.
# Docker is needed for everything except `make test`. Python deps come from
# requirements.txt if you are not using the .venv.

SHELL := /bin/bash
.DEFAULT_GOAL := help

PY := $(or $(wildcard .venv/bin/python),python3)
COMPOSE := docker compose
IMAGE := brz-sandbox:1
BRZ_URL := http://127.0.0.1:8000

.PHONY: help demo down logs sandbox test test-all lab lab-http verify clean

# List the targets and what they do.
help:
	@echo "Blast Radius Zero — local targets"
	@echo
	@echo "  make demo      up the whole stack locally (MOCK_INFERENCE=1, no API key)"
	@echo "  make down      stop and remove the stack (./data is kept)"
	@echo "  make logs      follow the control-plane logs"
	@echo "  make sandbox   build brz-sandbox:1 only if it is missing"
	@echo "  make test      unit tests (no Docker)"
	@echo "  make test-all  full pytest suite, including Docker-marked integration tests"
	@echo "  make lab       run the 7 containment probes against the local Docker daemon"
	@echo "  make lab-http  same suite through a running stack (make demo first)"
	@echo "  make verify    sandbox image -> unit tests -> integration tests -> lab"
	@echo "  make clean     remove the stack, ledger, artifacts and caches"
	@echo
	@echo "  python: $(PY)"

# Build brz-sandbox:1 only if the image is missing (the script also smoke-tests it).
sandbox:
	@if docker image inspect $(IMAGE) >/dev/null 2>&1; then \
		echo "ok: $(IMAGE) already present, not rebuilt"; \
	else \
		echo "building $(IMAGE) from sandbox/Dockerfile ..."; \
		./deploy/build-sandbox.sh; \
	fi

# Bring the whole stack up locally, then wait until /api/health answers.
demo:
	@$(COMPOSE) version >/dev/null 2>&1 || { \
		echo "ERROR: the docker compose plugin is not available (docker compose version failed)."; \
		echo "       Install it, or read docs/architecture.md §3 and run uvicorn directly."; \
		exit 1; }
	@$(MAKE) --no-print-directory sandbox
	@$(COMPOSE) up -d
	@printf 'waiting for %s/api/health ' '$(BRZ_URL)'
	@for i in $$(seq 1 90); do \
		if curl -fsS $(BRZ_URL)/api/health >/dev/null 2>&1; then echo ' up'; break; fi; \
		printf '.'; sleep 2; \
	done
	@curl -fsS $(BRZ_URL)/api/config | $(PY) -c "import json,sys; c=json.load(sys.stdin); print('runtime: %s (gvisor: %s)  image_present: %s  mock_inference: %s' % (c['runtime'], c['gvisor'], c['image_present'], c['mock']))"
	@echo
	@echo "  Console:   $(BRZ_URL)/"
	@echo "  Lab:       $(BRZ_URL)/  -> Containment Lab tab"
	@echo "  Ledger:    $(BRZ_URL)/  -> Ledger tab"
	@echo "  Stop with: make down"

# Stop and remove the local stack; the SQLite ledger under ./data survives.
down:
	-@$(COMPOSE) down --remove-orphans

# Follow the control-plane logs (Ctrl-C to detach).
logs:
	@$(COMPOSE) logs -f control-plane

# Unit tests only: no Docker daemon and no sandbox image required.
test:
	$(PY) -m pytest -m 'not integration'

# Full pytest suite, including the Docker-marked integration tests.
test-all:
	$(PY) -m pytest

# Run the seven containment probes in-process, through the same sandbox entry
# point as POST /api/containment/runs. Needs Docker + brz-sandbox:1, no server.
lab:
	@$(PY) -c "import asyncio,sys; from app.redteam import run_suite, new_suite_id; s=asyncio.run(run_suite(new_suite_id(), lambda e: None)); [print(('PASS' if r['blocked'] else 'FAIL'), r['id'].ljust(12), r['evidence']) for r in s['results']]; print(); print('containment: %d/%d attacks blocked' % (s['contained'], s['total'])); sys.exit(0 if s['all_blocked'] else 1)"

# Same suite over HTTP against a running stack (make demo first).
lab-http:
	@rid=$$(curl -fsS -X POST $(BRZ_URL)/api/containment/runs | $(PY) -c 'import json,sys; print(json.load(sys.stdin)["run_id"])') && \
	echo "suite $$rid"; \
	body=''; \
	for i in $$(seq 1 120); do \
		body=$$(curl -fsS $(BRZ_URL)/api/runs/$$rid); \
		st=$$(printf '%s' "$$body" | $(PY) -c 'import json,sys; print(json.load(sys.stdin)["status"])'); \
		if [ "$$st" != "running" ]; then break; fi; \
		sleep 2; \
	done; \
	printf '%s' "$$body" | $(PY) -c "import json,sys; r=json.load(sys.stdin); [print(('PASS' if p['blocked'] else 'FAIL'), p['probe_id'].ljust(12), p['evidence']) for p in r['probes']]; print(); print(r.get('answer') or ''); sys.exit(0 if r.get('verified') else 1)"

# The single "verify it yourself" target: image, unit tests, integration tests, lab.
verify: sandbox
	@echo '== 1/3  unit tests (no Docker)'
	@$(PY) -m pytest -q -m 'not integration'
	@echo
	@echo '== 2/3  integration tests (Docker + brz-sandbox:1)'
	@$(PY) -m pytest -q -m integration
	@echo
	@echo '== 3/3  containment lab (seven live probes)'
	@$(MAKE) --no-print-directory lab
	@echo
	@echo 'verify: all three stages passed'

# Remove the stack, ledger, artifacts, caches and any stray brz-* containers.
clean:
	-@$(COMPOSE) down --remove-orphans 2>/dev/null || true
	-@docker ps -aq --filter 'name=brz-' | xargs -r docker rm -f >/dev/null 2>&1 || true
	-@rm -rf data/artifacts data/brz.sqlite3 data/brz.sqlite3-wal data/brz.sqlite3-shm 2>/dev/null || true
	@# The control plane runs as root inside its container, so ./data can end up
	@# root-owned; finish the job from a throwaway container rather than asking for sudo.
	-@if [ -e data/artifacts ] || [ -n "$$(ls -A data 2>/dev/null)" ]; then \
		if docker image inspect $(IMAGE) >/dev/null 2>&1; then \
			docker run --rm --user 0 -v "$(CURDIR)/data:/data" $(IMAGE) \
				sh -c 'rm -rf /data/artifacts /data/brz.sqlite3 /data/brz.sqlite3-wal /data/brz.sqlite3-shm'; \
		else \
			echo 'note: ./data holds root-owned files; remove them with: sudo rm -rf data/artifacts data/brz.sqlite3*'; \
		fi; \
	fi
	-@find . -path ./.venv -prune -o -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true
	-@rm -rf .pytest_cache
	@echo 'clean: stack down, ledger and artifacts removed'
