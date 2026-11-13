#!/usr/bin/env bash
# Daily run wrapper for launchd.
# Ensures the Docker container is running before invoking jobagent run.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "=== jobagent daily run $(date) ==="

# Ensure Docker Desktop is up
if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running — skipping daily run"
    exit 0
fi

# Bring the app container up if it's not already running
docker compose up -d app

# Wait for the app container to be healthy (max 30s)
for i in $(seq 1 30); do
    if docker compose exec -T app echo ok >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

# ── Gmail token renewal ───────────────────────────────────────────────────────
# Try to silently refresh the OAuth token using the stored refresh_token.
# This works automatically for up to 6 months on published apps; for apps in
# "Testing" mode Google expires the refresh token after 7 days and the user
# must re-run:  python scripts/gmail_connect.py
python3 - << 'PYEOF'
import pathlib, sys

token_path = pathlib.Path("data/secrets/gmail_token.json")
if not token_path.exists():
    print("Gmail token missing — skipping refresh (tracking will be disabled)")
    sys.exit(0)

try:
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    creds = Credentials.from_authorized_user_file(str(token_path))
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            token_path.write_text(creds.to_json())
            print("✓ Gmail token refreshed")
        else:
            print("⚠ Gmail token expired and cannot be refreshed automatically.")
            print("  Run:  python scripts/gmail_connect.py")
    else:
        print("✓ Gmail token valid")
except Exception as e:
    print(f"⚠ Gmail token check failed: {e}")
    print("  Continuing without Gmail tracking.")
PYEOF

# ── Full pipeline ─────────────────────────────────────────────────────────────
docker compose exec -T app jobagent run --limit 20

echo "=== done $(date) ==="
