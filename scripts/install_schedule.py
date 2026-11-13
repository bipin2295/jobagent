#!/usr/bin/env python3
"""Install a launchd plist to run jobagent daily at 08:00 local time.

Usage:
    python scripts/install_schedule.py           # install
    python scripts/install_schedule.py --remove  # remove
    python scripts/install_schedule.py --status  # check status

The plist runs:
    docker compose exec app jobagent run

from the project directory, logging to ~/Library/Logs/jobagent/.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

LABEL = "io.local.jobagent.daily"
PROJECT_DIR = Path(__file__).resolve().parent.parent
PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LABEL}.plist"
LOG_DIR = Path.home() / "Library" / "Logs" / "jobagent"

PLIST_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>{project_dir}/scripts/daily_run.sh</string>
    </array>

    <key>WorkingDirectory</key>
    <string>{project_dir}</string>

    <!-- Run every day at 08:00 local time -->
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>8</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>

    <key>StandardOutPath</key>
    <string>{log_dir}/daily.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/daily.err</string>

    <!-- Only run when logged in -->
    <key>RunAtLoad</key>
    <false/>

    <!-- Restart on crash with a 60s delay -->
    <key>ThrottleInterval</key>
    <integer>60</integer>

    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
"""


def install() -> None:
    PLIST_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    plist = PLIST_TEMPLATE.format(
        label=LABEL,
        project_dir=PROJECT_DIR,
        log_dir=LOG_DIR,
    )
    PLIST_PATH.write_text(plist, encoding="utf-8")
    print(f"✓ Plist written to {PLIST_PATH}")

    # Unload if already loaded
    subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
    result = subprocess.run(
        ["launchctl", "load", "-w", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"✓ Loaded into launchd — will run daily at 08:00")
        print(f"  Logs: {LOG_DIR}/daily.log")
    else:
        print(f"✗ launchctl load failed: {result.stderr}")
        sys.exit(1)


def remove() -> None:
    if PLIST_PATH.exists():
        subprocess.run(["launchctl", "unload", str(PLIST_PATH)], capture_output=True)
        PLIST_PATH.unlink()
        print(f"✓ Removed {PLIST_PATH}")
    else:
        print("Not installed.")


def status() -> None:
    result = subprocess.run(
        ["launchctl", "list", LABEL],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        print(f"✓ {LABEL} is loaded")
        print(result.stdout)
    else:
        print(f"✗ {LABEL} is NOT loaded")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Manage jobagent launchd schedule")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    if args.remove:
        remove()
    elif args.status:
        status()
    else:
        install()
