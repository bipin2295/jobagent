# ─────────────────────────────────────────────────────────────────────────────
# Job Application Agent – Docker image
#
# Single-stage build (simpler, no cross-stage venv path issues).
# Layer caching strategy:
#   1. System packages  – rarely changes
#   2. Python deps      – cached until pyproject.toml / uv.lock change
#   3. Playwright       – cached until playwright version changes
#   4. App source       – invalidated on every code change (fast layer)
#
# Build:  docker compose build
# Run:    docker compose up -d
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm

# ── Layer 1: System packages ──────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb \
    x11vnc \
    novnc \
    websockify \
    fonts-liberation \
    fonts-noto-cjk \
    tini \
    procps \
    curl \
    wget \
    gnupg \
    ca-certificates \
    sudo \
    libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: uv installer ─────────────────────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# ── Layer 3: Python dependencies (cached by uv.lock) ─────────────────────────
COPY pyproject.toml uv.lock ./
# Minimal stub so uv can resolve the project without full source
RUN mkdir -p src/jobagent && touch src/jobagent/__init__.py

RUN uv sync \
    --frozen \
    --no-install-project \
    --extra llm \
    --extra discovery \
    --extra browser \
    --extra profile-tools \
    --extra tracking \
    --extra dashboard

# ── Layer 4: Playwright Chromium + OS deps ────────────────────────────────────
# (runs apt-get internally, so must be in the same stage as the system apt-get)
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright
RUN uv run playwright install chromium --with-deps

# ── Layer 5: Application source ───────────────────────────────────────────────
COPY src/        ./src/
COPY config/     ./config/
COPY prompts/    ./prompts/
COPY templates/  ./templates/
COPY .streamlit/ ./.streamlit/

# Install the project package itself (now that src/ is present)
RUN uv sync \
    --frozen \
    --extra llm \
    --extra discovery \
    --extra browser \
    --extra profile-tools \
    --extra tracking \
    --extra dashboard

# ── Environment ───────────────────────────────────────────────────────────────
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
ENV PLAYWRIGHT_BROWSERS_PATH=/app/.playwright

# Docker-internal defaults (all overrideable via docker-compose environment:)
ENV JOBAGENT_OLLAMA_BASE_URL=http://ollama:11434
ENV JOBAGENT_DATA_DIR=/app/data
ENV JOBAGENT_DB_PATH=/app/data/jobagent.db
ENV JOBAGENT_PROFILE_SOURCE=/resume/master_resume.txt

# ── Ports ─────────────────────────────────────────────────────────────────────
EXPOSE 8501
EXPOSE 6080

# ── Entrypoint ────────────────────────────────────────────────────────────────
COPY docker/app/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]

# Default: Streamlit dashboard (stays running, accessible at :8501)
CMD ["streamlit", "run", "src/jobagent/dashboard/app.py"]
