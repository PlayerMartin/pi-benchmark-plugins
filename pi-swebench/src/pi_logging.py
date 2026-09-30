"""Shared logging for the pi-swebench pipeline.

Every module (the pipeline script, the benchmark loaders, future extensions)
logs through ``log`` so output stays uniform and no module re-implements it.
"""

from __future__ import annotations

import time


def log(msg: str) -> None:
    """Print a timestamped progress line to stdout."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)
