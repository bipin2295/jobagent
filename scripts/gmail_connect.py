#!/usr/bin/env python3
"""Re-authenticate Gmail OAuth when the token expires.

Run this on your Mac (not inside Docker) whenever jobagent run shows:
  Gmail token ✗  run `jobagent gmail-connect`

Usage:
    python scripts/gmail_connect.py
"""
from pathlib import Path

try:
    from google_auth_oauthlib.flow import InstalledAppFlow
except ImportError:
    raise SystemExit(
        "Missing dependency. Run:  pip install google-auth-oauthlib"
    )

SECRET = Path("data/secrets/client_secret.json")
TOKEN  = Path("data/secrets/gmail_token.json")

if not SECRET.exists():
    raise SystemExit(
        f"client_secret.json not found at {SECRET}\n"
        "Download it from Google Cloud Console → APIs & Services → Credentials."
    )

TOKEN.parent.mkdir(parents=True, exist_ok=True)

flow = InstalledAppFlow.from_client_secrets_file(
    str(SECRET),
    scopes=["https://www.googleapis.com/auth/gmail.readonly"],
)
creds = flow.run_local_server(port=0)
TOKEN.write_text(creds.to_json())
print(f"✓ Gmail token saved to {TOKEN}")
print("  Valid for ~7 days (testing mode). Re-run this script when it expires.")
