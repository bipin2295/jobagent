#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Container entrypoint
#
# 1. Start Xvfb  – virtual display (required by Playwright in headed mode)
# 2. Start VNC   – so you can open http://localhost:6080 and watch the browser
# 3. Ensure data directories exist on the mounted volume
# 4. Init the DB (idempotent)
# 5. exec the requested command
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ── 1. Virtual display (Xvfb) ─────────────────────────────────────────────────
# Remove a stale lock left by a crash
rm -f /tmp/.X${DISPLAY#:}-lock 2>/dev/null || true

Xvfb "${DISPLAY}" \
    -screen 0 1920x1080x24 \
    -ac \
    +extension GLX \
    +extension RANDR \
    +render \
    -noreset \
    &

# Give Xvfb a moment to come up
sleep 0.5

# ── 2. VNC server + noVNC web client ──────────────────────────────────────────
# Always start so the live view is immediately available at :6080.
# x11vnc → VNC on 5900, websockify bridges → noVNC web on 6080.

x11vnc \
    -display "${DISPLAY}" \
    -nopw \
    -listen 0.0.0.0 \
    -forever \
    -shared \
    -quiet \
    -bg \
    2>/dev/null || true

websockify \
    --web /usr/share/novnc \
    --wrap-mode=ignore \
    --log-file=/dev/null \
    6080 \
    0.0.0.0:5900 \
    &

# ── 3. Ensure volume-mounted data directories exist ───────────────────────────
mkdir -p \
    /app/data/profile \
    /app/data/secrets \
    /app/data/applications \
    /app/data/registers \
    /app/data/browser_profiles

# ── 4. Initialise database (idempotent – safe to run on every start) ──────────
jobagent db-init 2>/dev/null || true

# ── 5. Run the requested command ──────────────────────────────────────────────
echo "▶ Container ready. Dashboard → http://localhost:8501 | Live view → http://localhost:6080"
exec "$@"
