#!/usr/bin/env python3
"""SWE-bench testing framework for coding agents.

Pipeline (one instance at a time):

  1. DATASET LOADER -> instance specs (see the `benchmarks` package)
  2. REPO PREPARER  -> fresh clone at base_commit into a workspace
  3. AGENT RUNNER   -> the selected agent works on the issue (see `agents`)
  4. FORMATTER      -> git diff vs base_commit -> predictions JSONL
  5. EVALUATION     -> SWE-bench harness (Docker required)

Each `run` writes everything into one timestamped directory:

    runs/<YYYYmmdd-HHMMSS>/
      run.json, predictions.jsonl, manifest.json
      logs/<instance_id>/   meta.json, events.jsonl, transcript.md, patch.diff

Usage:
    python run_pi_swebench.py list --dataset swebench:SWE-bench/SWE-bench_Lite --limit 10
    python run_pi_swebench.py run --dataset swebench:SWE-bench/SWE-bench_Lite \
        --skip 10 --limit 5 --agent pi --agent-model sonnet:high
    python run_pi_swebench.py evaluate --predictions runs/<timestamp>/predictions.jsonl \
        --dataset swebench:SWE-bench/SWE-bench_Lite
    python run_pi_swebench.py report --run-id <run-id>
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from agents import AgentRunner, get_agent
from cli_args import parse_args
from benchmarks import (
    InstanceSpec,
    load_instances,
    resolve_dataset,
)
from pi_logging import log
from repo_prep import (
    ensure_mirror,
    git,
    prepare_workspace,
    robust_rmtree,
)

DEFAULT_SPLIT = "test"
DEFAULT_MODEL_NAME = "pi-agent"
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


def clean_diff(raw_diff: str) -> str:
    """Drop binary-file sections so the harness can `git apply` the patch."""
    if not raw_diff.strip():
        return ""
    sections: list[list[str]] = []
    current: list[str] = []
    for line in raw_diff.splitlines(keepends=False):
        if line.startswith("diff --git "):
            if current:
                sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    kept = [sec for sec in sections if not in_binary_of(sec)]
    return "\n".join("\n".join(sec) for sec in kept).strip() + "\n" if kept else ""


def in_binary_of(section: list[str]) -> bool:
    return any(
        "GIT binary patch" in line or line.startswith("Binary files ")
        for line in section
    )


def capture_patch(spec: InstanceSpec, workspace: Path) -> str:
    """Diff working tree (incl. untracked, incl. accidental commits) vs base."""
    git(workspace, "add", "-A")  # stage everything so untracked files appear
    raw = git(workspace, "diff", "--cached", spec.base_commit)
    git(workspace, "reset", "--quiet", check=False)
    return clean_diff(raw)


def write_prediction(out_path: Path, spec: InstanceSpec, patch: str) -> None:
    """One JSON line: the sole interface into the SWE-bench harness."""
    record = {
        "instance_id": spec.instance_id,
        "model_name_or_path": DEFAULT_MODEL_NAME,
        "model_patch": patch,
    }
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


def process_instance(
    spec: InstanceSpec,
    run_dir: Path,
    agent: AgentRunner,
    agent_timeout: int,
    predictions_path: Path,
) -> RunResult:
    log(f"=== {spec.instance_id} ===")
    mirror_root = run_dir.parent / "mirrors"
    mirror_root.mkdir(parents=True, exist_ok=True)
    ws_root = run_dir / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    workspace = ws_root / spec.instance_id
    log_dir = run_dir / "logs" / spec.instance_id
    log_dir.mkdir(parents=True, exist_ok=True)
    start = time.time()

    try:
        mirror = ensure_mirror(spec.repo, mirror_root)
        prepare_workspace(spec, mirror, workspace)
    except Exception as e:  # noqa: BLE001
        log(f"  PREP FAILED: {e}")
        write_instance_meta(
            log_dir, spec, agent.model, "prep_error", time.time() - start, None, 0, str(e)
        )
        return RunResult(
            instance_id=spec.instance_id,
            status="prep_error",
            error=str(e),
            duration_s=time.time() - start,
            log_dir=str(log_dir),
        )

    outcome = agent.run(spec, workspace, log_dir, agent_timeout)

    try:
        patch = capture_patch(spec, workspace)
    except Exception as e:  # noqa: BLE001
        log(f"  DIFF CAPTURE FAILED: {e}")
        error = f"diff capture: {e}"
        write_instance_meta(
            log_dir, spec, agent.model, "agent_error",
            time.time() - start, outcome.usage, 0, error,
        )
        return RunResult(
            instance_id=spec.instance_id,
            status="agent_error",
            error=error,
            duration_s=time.time() - start,
            log_dir=str(log_dir),
            usage=outcome.usage,
        )

    patch_file = log_dir / "patch.diff"
    if patch:
        patch_file.write_text(patch, encoding="utf-8")
        write_prediction(predictions_path, spec, patch)
        final_status = "completed" if outcome.status == "ok" else outcome.status
        log(
            f"  patch: {len(patch.splitlines())} diff lines -> {patch_file.name} "
            f"({final_status}, {time.time() - start:.0f}s)"
        )
    else:
        final_status = "no_patch" if outcome.status == "ok" else outcome.status
        log(f"  EMPTY PATCH ({final_status}, {time.time() - start:.0f}s)")

    write_instance_meta(
        log_dir,
        spec,
        agent.model,
        final_status,
        time.time() - start,
        outcome.usage,
        len(patch.splitlines()) if patch else 0,
        outcome.detail if outcome.status != "ok" else "",
    )

    robust_rmtree(workspace)

    return RunResult(
        instance_id=spec.instance_id,
        status=final_status,
        duration_s=time.time() - start,
        patch=patch,
        error=outcome.detail if outcome.status != "ok" else "",
        log_dir=str(log_dir),
        usage=outcome.usage,
    )


def cmd_run(args: argparse.Namespace) -> int:
    specs = load_instances(
        args.dataset, DEFAULT_SPLIT, limit=args.limit, skip=args.skip
    )
    if not specs:
        log("No instances selected.")
        return 1
    log(f"Selected {len(specs)} instance(s): {', '.join(s.instance_id for s in specs)}")

    agent = get_agent(args.agent, args.agent_model, args.agent_extra_args)
    log(
        f"Agent: {agent.name} | model: {args.agent_model} | "
        f"extra args: {args.agent_extra_args or '(none)'}"
    )

    run_root = Path(DEFAULT_RUN_DIR).resolve()
    run_dir = run_root / time.strftime("%Y%m%d-%H%M%S")
    while run_dir.exists():  # same-second collision
        run_dir = run_dir.with_name(run_dir.name + "-")
    run_dir.mkdir(parents=True)
    predictions_path = run_dir / "predictions.jsonl"

    run_meta = {
        "run_id": run_dir.name,
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": resolve_dataset(args.dataset),
        "split": DEFAULT_SPLIT,
        "agent": args.agent,
        "agent_model": args.agent_model,
        "agent_extra_args": args.agent_extra_args,
        "agent_timeout": args.agent_timeout,
        "instances": [s.instance_id for s in specs],
    }
    (run_dir / "run.json").write_text(
        json.dumps(run_meta, indent=2), encoding="utf-8"
    )

    log(f"Run dir: {run_dir}")
    log(f"Predictions file: {predictions_path}")

    results: list[RunResult] = []
    for spec in specs:
        results.append(
            process_instance(spec, run_dir, agent, args.agent_timeout, predictions_path)
        )

    write_manifest(run_dir, results, args)
    print_summary(results)
    return 0 if all(r.status == "completed" for r in results) else 2


def write_manifest(
    run_dir: Path, results: list[RunResult], args: argparse.Namespace
) -> None:
    manifest = {
        "run_id": run_dir.name,
        "dataset": resolve_dataset(args.dataset),
        "split": DEFAULT_SPLIT,
        "model_name": DEFAULT_MODEL_NAME,
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


def normalize_predictions(preds: Path) -> Path:
    """Rename legacy 'patch' keys to the 'model_patch' the harness expects."""
    records: list[dict] = []
    needs_fix = False
    for line in preds.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if "model_patch" not in rec and "patch" in rec:
            rec["model_patch"] = rec.pop("patch")
            needs_fix = True
        records.append(rec)

    if not needs_fix:
        return preds

    out = preds.with_name(f"{preds.stem}.normalized{preds.suffix}")
    with open(out, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")
    log(f"Normalized predictions ('patch' -> 'model_patch') -> {out.name}")
    return out


def cmd_evaluate(args: argparse.Namespace) -> int:
    """Run the SWE-bench harness over the predictions file (needs Docker)."""
    preds = Path(args.predictions)
    if not preds.exists():
        log(f"Predictions file not found: {preds}")
        return 1
    preds = normalize_predictions(preds)
    n = sum(1 for _ in open(preds, encoding="utf-8"))
    log(f"Evaluating {n} prediction(s) from {preds}")

    run_id = f"pi-{time.strftime('%Y%m%d-%H%M%S')}"
    log(f"Run id: {run_id}")
    cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        resolve_dataset(args.dataset),
        "--split",
        DEFAULT_SPLIT,
        "--predictions_path",
        str(preds),
        "--run_id",
        run_id,
        "--max_workers",
        "1",
        "--report_dir",
        "logs",
    ]
    log("Harness command: " + " ".join(cmd))
    proc = subprocess.run(cmd)
    return proc.returncode


def cmd_report(args: argparse.Namespace) -> int:
    """Best-effort: find the harness report and print a summary.

    Handles both report shapes produced by SWE-bench >= 5:
      * aggregate      logs/<model>.<run_id>.json              (schema_version 2)
      * per-instance   logs/run_evaluation/<run_id>/<model>/<instance>/report.json
    plus the legacy  logs/run_evaluation/<run_id>*/report.json.
    """
    found: dict[Path, None] = {}
    for pat in (
        f"logs/*.{args.run_id}*.json",
        f"logs/run_evaluation/{args.run_id}*/**/report.json",
        f"logs/run_evaluation/{args.run_id}*/report.json",
    ):
        for p in Path(".").glob(pat):
            found[p.resolve()] = None
    candidates = sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

    if not candidates:
        log(
            "No report found. Expected logs/<model>.<run_id>.json or "
            "logs/run_evaluation/<run_id>/.../report.json."
        )
        return 1

    for path in candidates:  # prefer the aggregate report (has 'resolved_ids')
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(data, dict) and "resolved_ids" in data:
            log(f"Report: {path}")
            for key in (
                "total_instances",
                "submitted_instances",
                "completed_instances",
                "resolved_instances",
                "unresolved_instances",
                "error_instances",
                "infra_failure_instances",
                "empty_patch_instances",
            ):
                if key in data:
                    log(f"  {key}: {data[key]}")
            for iid in data.get("resolved_ids", []):
                log(f"  RESOLVED     {iid}")
            for iid in data.get("unresolved_ids", []):
                log(f"  unresolved   {iid}")
            for iid in data.get("error_ids", []):
                log(f"  error        {iid}")
            for iid in data.get("empty_patch_ids", []):
                log(f"  empty-patch  {iid}")
            return 0

    reports: list[dict] = []
    for path in candidates:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if (
            isinstance(data, dict)
            and len(data) == 1
            and isinstance(next(iter(data.values())), dict)
            and "resolved" in next(iter(data.values()))
        ):
            iid, inner = next(iter(data.items()))
            reports.append({"instance_id": iid, **inner})
        elif isinstance(data, dict) and "resolved" in data:
            reports.append(data)
    if not reports:
        log(f"Unrecognised report shape in {candidates[0]}")
        return 1
    resolved = sum(1 for r in reports if r.get("resolved"))
    log(f"Resolved: {resolved}/{len(reports)}")
    for r in reports:
        verdict = "RESOLVED" if r.get("resolved") else "unresolved"
        log(
            f"  {verdict:11} {r.get('instance_id', '?')}"
            f"  (patch_applied={r.get('patch_successfully_applied')},"
            f" infra_failure={r.get('infra_failure')})"
        )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
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
