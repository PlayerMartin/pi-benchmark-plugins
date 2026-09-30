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


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Evaluate a predictions file with the benchmark's harness."""
    preds = Path(args.predictions)
    if not preds.exists():
        log(f"Predictions file not found: {preds}")
        return 1
    run_id = args.run_id or f"bench-{time.strftime('%Y%m%d-%H%M%S')}"
    n = sum(1 for _ in open(preds, encoding="utf-8"))
    log(f"Evaluating {n} prediction(s) from {preds}")
    log(f"Run id: {run_id}")
    benchmark = get_benchmark(args.dataset)
    return benchmark.evaluate(preds, run_id, max_workers=args.max_workers)


def cmd_report(args: argparse.Namespace) -> int:
    """Summarize a harness report for a run id."""
    return get_benchmark(args.dataset).summarize_report(args.run_id)
