#!/bin/sh
# Run a long, browser-driving jobagent command with a HARD timeout that
# actually kills the whole process tree.
#
# Why this exists: `timeout N cmd` only signals its direct child. `uv run`
# spawns python, which spawns Playwright's node driver and Chromium — those
# grandchildren survive, leaving orphaned browsers (and a wedged apply run)
# behind. `setsid` puts the command in its own process group so the timeout
# can kill the group as a unit.
#
# Usage:
#   ./scripts/guarded_run.sh 900 uv run jobagent apply -n 5 --live
#   GUARD_LOG=/tmp/run.log ./scripts/guarded_run.sh 300 uv run jobagent apply -n 1
#
# Exit codes: the command's own code, or 124/137 when it was killed.

set -eu

BUDGET="${1:?usage: guarded_run.sh <seconds> <command...>}"
shift

GRACE=15
LOG="${GUARD_LOG:-}"

echo "[guarded_run] budget=${BUDGET}s grace=${GRACE}s cmd=$*" >&2

if [ -n "$LOG" ]; then
    # setsid --wait: run in a new process group, wait for completion, and let
    # `timeout` kill the entire group (-k sends SIGKILL after the grace period).
    exec setsid --wait timeout -k "$GRACE" "$BUDGET" "$@" >"$LOG" 2>&1
fi

exec setsid --wait timeout -k "$GRACE" "$BUDGET" "$@"
