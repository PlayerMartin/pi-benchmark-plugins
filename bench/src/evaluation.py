"""Evaluation commands: dispatch to the benchmark's evaluation harness.

A benchmark wrapper provides evaluation as part of its interface (see
benchmarks/base.py): ``evaluate()`` runs its harness over a predictions file
and ``summarize_report()`` reads the harness's reports. These commands just
resolve the benchmark and delegate.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from benchmarks import get_benchmark
from pi_logging import log


def _newest_predictions() -> Path | None:
    """Newest runs/<timestamp>/predictions.jsonl relative to the CWD, or None."""
    candidates = sorted(
        Path(".").glob("runs/*/predictions.jsonl"),
        # mtime first; the timestamped dir name breaks ties (and sorts
        # chronologically), so identical mtimes stay deterministic.
        key=lambda p: (p.stat().st_mtime, p.name, p.parent.name),
        reverse=True,
    )
    return candidates[0] if candidates else None


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Evaluate a predictions file with the benchmark's harness."""
    preds = Path(args.predictions) if args.predictions else _newest_predictions()
    if preds is None or not preds.exists():
        log("No predictions file given and none found under runs/*/predictions.jsonl")
        return 1
    run_id = args.run_id or f"bench-{time.strftime('%Y%m%d-%H%M%S')}"
    n = sum(1 for _ in open(preds, encoding="utf-8"))
    log(f"Evaluating {n} prediction(s) from {preds}")
    log(f"Run id: {run_id}")
    benchmark = get_benchmark(args.dataset)
    return benchmark.evaluate(preds, run_id, max_workers=args.max_workers)


def cmd_report(args: argparse.Namespace) -> int:
    """Summarize a harness report for a run id (newest report if none given)."""
    if not args.run_id:
        log("No --run-id given; summarizing the newest report in logs/")
    return get_benchmark(args.dataset).summarize_report(args.run_id)
