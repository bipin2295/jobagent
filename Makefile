# ─────────────────────────────────────────────────────────────────────────────
# Job Application Agent – Makefile
#
# All commands run inside the Docker container.
# You never need to install Python locally.
#
# Prerequisites: Docker Desktop (or Docker Engine + Compose plugin)
#
# First-time setup:
#   make build         # build the image (~5 min first time)
#   make up            # start app + ollama
#   make pull-models   # download nomic-embed-text into ollama (~300 MB, once)
#   make import        # parse your master_resume.txt
#
# Daily use:
#   make run           # full discover → score → tailor → apply → track
#   make status        # today's summary
# ─────────────────────────────────────────────────────────────────────────────

COMPOSE  := docker compose
APP_EXEC := $(COMPOSE) exec app
APP_RUN  := $(COMPOSE) run --rm app

.PHONY: help
help:
	@echo ""
	@echo "  ── Setup (run once) ──────────────────────────────────────────────"
	@echo "  make build          Build the Docker image"
	@echo "  make up             Start all services (app + ollama)"
	@echo "  make pull-models    Download nomic-embed-text into Ollama"
	@echo "  make import         Parse master_resume.txt → master_profile.yaml"
	@echo ""
	@echo "  ── Daily use ─────────────────────────────────────────────────────"
	@echo "  make run            Full pipeline: discover → score → tailor → apply → track"
	@echo "  make status         Today's summary (jobs found / applied / cost)"
	@echo ""
	@echo "  ── Individual pipeline stages ────────────────────────────────────"
	@echo "  make discover       Run only the discovery stage"
	@echo "  make score          Run only the scoring stage"
	@echo "  make tailor         Run only the tailoring stage"
	@echo "  make apply          Run only the apply stage"
	@echo "  make track          Run only the inbox-tracking stage"
	@echo ""
	@echo "  ── Control ───────────────────────────────────────────────────────"
	@echo "  make pause          Pause all workers after the current action"
	@echo "  make resume         Resume paused workers"
	@echo "  make stop           Graceful stop"
	@echo ""
	@echo "  ── Auth & keys ───────────────────────────────────────────────────"
	@echo "  make llm-ping       Test Portkey connection"
	@echo "  make gmail-connect  Authenticate Gmail in browser"
	@echo ""
	@echo "  ── Infrastructure ────────────────────────────────────────────────"
	@echo "  make up             Start services"
	@echo "  make up-dev         Start services + Mailpit (for tests)"
	@echo "  make down           Stop all services"
	@echo "  make restart        Restart the app container"
	@echo "  make logs           Follow app container logs"
	@echo "  make shell          Open a bash shell inside the container"
	@echo "  make rebuild        Rebuild the image and restart"
	@echo ""
	@echo "  ── URLs ──────────────────────────────────────────────────────────"
	@echo "  make dashboard      Open the Streamlit dashboard in your browser"
	@echo "  make novnc          Open the live browser view (watch the agent)"
	@echo "  make mailpit        Open the test inbox (dev profile only)"
	@echo ""

# ── Image & services ──────────────────────────────────────────────────────────

.PHONY: build
build:
	$(COMPOSE) build --pull

.PHONY: up
up:
	$(COMPOSE) up -d
	@echo ""
	@echo "  Services started."
	@echo "  Dashboard  → http://localhost:8501"
	@echo "  Live view  → http://localhost:6080/vnc.html"
	@echo ""
	@echo "  If this is your first start, run:  make pull-models"

.PHONY: up-dev
up-dev:
	$(COMPOSE) --profile dev up -d
	@echo ""
	@echo "  Services started (incl. Mailpit)."
	@echo "  Mailpit    → http://localhost:8025"

.PHONY: down
down:
	$(COMPOSE) down

.PHONY: restart
restart:
	$(COMPOSE) restart app

.PHONY: rebuild
rebuild: down build up

.PHONY: logs
logs:
	$(COMPOSE) logs -f app

.PHONY: shell
shell:
	$(APP_EXEC) bash

# ── Profile ───────────────────────────────────────────────────────────────────

.PHONY: import
import:
	$(APP_EXEC) jobagent profile import

.PHONY: show-profile
show-profile:
	$(APP_EXEC) jobagent profile show

# ── Pipeline ──────────────────────────────────────────────────────────────────

.PHONY: run
run:
	$(APP_EXEC) jobagent run

.PHONY: discover
discover:
	$(APP_EXEC) jobagent run --stage discover

.PHONY: score
score:
	$(APP_EXEC) jobagent run --stage score

.PHONY: tailor
tailor:
	$(APP_EXEC) jobagent run --stage tailor

.PHONY: apply
apply:
	$(APP_EXEC) jobagent run --stage apply

.PHONY: track
track:
	$(APP_EXEC) jobagent run --stage track

.PHONY: status
status:
	$(APP_EXEC) jobagent status

# ── Control ───────────────────────────────────────────────────────────────────

.PHONY: pause
pause:
	$(APP_EXEC) jobagent ctl pause

.PHONY: resume
resume:
	$(APP_EXEC) jobagent ctl resume

.PHONY: stop
stop:
	$(APP_EXEC) jobagent ctl stop

# ── Auth & keys ───────────────────────────────────────────────────────────────

.PHONY: db-init
db-init:
	$(APP_EXEC) jobagent db-init

.PHONY: llm-ping
llm-ping:
	$(APP_EXEC) jobagent llm-ping

.PHONY: gmail-connect
gmail-connect:
	$(COMPOSE) exec -it app jobagent gmail-connect

# ── Ollama model management ───────────────────────────────────────────────────

.PHONY: pull-models
pull-models:
	@echo "Pulling nomic-embed-text (embedding model, ~300 MB)…"
	$(COMPOSE) exec ollama ollama pull nomic-embed-text
	@echo ""
	@echo "Available models:"
	$(COMPOSE) exec ollama ollama list

.PHONY: ollama-shell
ollama-shell:
	$(COMPOSE) exec ollama bash

# ── URLs (macOS open) ─────────────────────────────────────────────────────────

.PHONY: dashboard
dashboard:
	@open http://localhost:8501 2>/dev/null || xdg-open http://localhost:8501

.PHONY: novnc
novnc:
	@open "http://localhost:6080/vnc.html?autoconnect=1" 2>/dev/null \
	    || xdg-open "http://localhost:6080/vnc.html?autoconnect=1"

.PHONY: mailpit
mailpit:
	@open http://localhost:8025 2>/dev/null || xdg-open http://localhost:8025
