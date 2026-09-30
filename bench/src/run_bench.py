#!/usr/bin/env python3
"""Benchmark testing framework for coding agents.

Pipeline (one instance at a time):

  1. DATASET LOADER -> instance specs (see the `benchmarks` package)
  2. REPO PREPARER  -> fresh clone at base_commit into a workspace
  3. AGENT RUNNER   -> the selected agent works on the issue (see `agents`)
  4. FORMATTER      -> git diff vs base_commit -> predictions JSONL
  5. EVALUATION     -> the benchmark's evaluation harness (via its wrapper)

Any agent and any benchmark can be used, as long as the required wrapper is
implemented (see agents/base.py and benchmarks/base.py).

Each `run` writes everything into one timestamped directory:

    runs/<YYYYmmdd-HHMMSS>/
      run.json, predictions.jsonl, manifest.json
      logs/<instance_id>/   meta.json, events.jsonl, transcript.md, patch.diff

Usage:
    python run_bench.py list --dataset <benchmark>:<dataset> --limit 10
    python run_bench.py run --dataset <benchmark>:<dataset> \
        --skip 10 --limit 5 --agent <name> --agent-model <model> \
        --model-name <name>
    python run_bench.py evaluate --dataset <benchmark>:<dataset> \
        [--predictions runs/<timestamp>/predictions.jsonl]
    python run_bench.py report --dataset <benchmark>:<dataset> [--run-id <run-id>]

    (evaluate defaults to the newest runs/<timestamp>/predictions.jsonl;
     report defaults to the newest report in logs/.)

Module layout:
    patch_utils    diff capture/cleaning (git diff -> patch text)
    run_artifacts  result records and everything written to disk / printed
    pipeline       per-instance orchestration and the `run` command
    evaluation     dispatch of evaluate/report to the benchmark wrapper
"""

from __future__ import annotations

import sys

from benchmarks import DEFAULT_SPLIT, load_instances
from cli_args import parse_args
from evaluation import cmd_evaluate, cmd_report
from pi_logging import log
from pipeline import cmd_run


def cmd_list(args) -> int:
    specs = load_instances(
        args.dataset, DEFAULT_SPLIT, limit=args.limit, skip=args.skip
    )
    for s in specs:
        print(s.instance_id)
    log(f"{len(specs)} instance(s).")
    return 0


def main() -> int:
    args = parse_args(
        None,
        __doc__,
        {
            "list": cmd_list,
            "run": cmd_run,
            "evaluate": cmd_evaluate,
            "report": cmd_report,
        },
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
