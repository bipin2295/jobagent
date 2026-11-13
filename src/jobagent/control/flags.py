"""Control flags: pause / resume / stop / abort.

Control state is persisted in the DB (control_flags table) so it survives
process restarts.  Each worker also holds an in-memory asyncio.Event so that
cooperative cancellation checks resolve in < 1 s without polling the DB.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import Literal

# Scopes
Scope = Literal["global", "stage", "ats", "tenant", "application"]

STOP_SENTINEL = "data/STOP"  # touch this file for an emergency hard stop


class ControlState(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"


# In-process event: set → continue; clear → pause
_global_go = asyncio.Event()
_global_go.set()  # running by default


def is_stop_file_present() -> bool:
    from pathlib import Path
    return Path(STOP_SENTINEL).exists()


def request_pause() -> None:
    _global_go.clear()


def request_resume() -> None:
    _global_go.set()


def request_stop() -> None:
    _global_go.clear()


async def checkpoint(label: str = "") -> None:
    """Call before every agent action.  Blocks if paused; raises if stopped."""
    if is_stop_file_present():
        raise RuntimeError(f"Stop sentinel present – aborting at checkpoint '{label}'")
    if not _global_go.is_set():
        # Paused – wait until resumed or stop sentinel appears
        while not _global_go.is_set():
            if is_stop_file_present():
                raise RuntimeError(f"Stop sentinel during pause – aborting at '{label}'")
            await asyncio.sleep(0.5)
