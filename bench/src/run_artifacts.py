"""Run outputs: result records, prediction/manifest/meta files, summaries.

Everything the pipeline writes to disk (besides the agent's own logs) and
everything printed as a run summary lives here. Prediction record shapes
are defined by the benchmark wrapper (see benchmarks/base.py).
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from benchmarks import DEFAULT_SPLIT, InstanceSpec, resolve_dataset
from pi_logging import log

DEFAULT_RUN_DIR = "runs"


@dataclass
class RunResult:
    instance_id: str
    status: str  # completed | no_patch | agent_error | agent_timeout | prep_error
    duration_s: float = 0.0
    patch: str = ""
    error: str = ""
    log_dir: str = ""
    usage: Optional[dict] = None


def write_prediction(out_path: Path, record: dict) -> None:
    """Append one record to the predictions JSONL.

    This file is the sole interface into the benchmark's evaluation harness;
    the record shape is defined by the benchmark wrapper.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def write_instance_meta(
    log_dir: Path,
    spec: InstanceSpec,
    model: str,
    status: str,
    duration_s: float,
    usage: Optional[dict],
    patch_lines: int,
    error: str,
) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "instance_id": spec.instance_id,
        "repo": spec.repo,
        "base_commit": spec.base_commit,
        "model": model,
        "status": status,
        "duration_s": round(duration_s, 1),
        "patch_lines": patch_lines,
        "error": error[:2000],
        "artifacts": sorted(p.name for p in log_dir.iterdir() if p.is_file()),
    }
    if usage:
        meta["usage"] = usage
    (log_dir / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def write_manifest(
    run_dir: Path, results: list[RunResult], args: argparse.Namespace
) -> None:
    manifest = {
        "run_id": run_dir.name,
        "dataset": resolve_dataset(args.dataset),
        "split": DEFAULT_SPLIT,
        "model_name": args.model_name,
        "agent": args.agent,
        "agent_model": args.agent_model,
        "agent_extra_args": args.agent_extra_args,
        "agent_timeout": args.agent_timeout,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "instances": len(results),
            "total_input_tokens": sum(
                r.usage["input_tokens"] for r in results if r.usage
            ),
            "total_output_tokens": sum(
                r.usage["output_tokens"] for r in results if r.usage
            ),
        },
        "results": [
            {
                "instance_id": r.instance_id,
                "status": r.status,
                "duration_s": round(r.duration_s, 1),
                "patch_lines": len(r.patch.splitlines()) if r.patch else 0,
                "log_dir": r.log_dir,
                "usage": r.usage,
                "error": r.error[:2000],
            }
            for r in results
        ],
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def print_summary(results: list[RunResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    total = len(results)
    log(
        f"Done. {total} instance(s): "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    no_patch = [r.instance_id for r in results if r.status == "no_patch"]
    if no_patch:
        log(f"  no patch produced: {', '.join(no_patch)}")
    failed = [
        r.instance_id
        for r in results
        if r.status.endswith("error") or r.status == "agent_timeout"
    ]
    if failed:
        log(f"  errored/timed out: {', '.join(failed)}")
