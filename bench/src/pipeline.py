"""Pipeline orchestration: per-instance execution and the `run` command."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from agents import AgentRunner, get_agent
from benchmarks import (
    DEFAULT_SPLIT,
    Benchmark,
    InstanceSpec,
    get_benchmark,
    load_instances,
    resolve_dataset,
)
from patch_utils import capture_patch
from pi_logging import log
from repo_prep import ensure_mirror, prepare_workspace, robust_rmtree
from run_artifacts import (
    DEFAULT_RUN_DIR,
    RunResult,
    print_summary,
    write_instance_meta,
    write_manifest,
    write_prediction,
)


def process_instance(
    spec: InstanceSpec,
    run_dir: Path,
    benchmark: Benchmark,
    agent: AgentRunner,
    agent_timeout: int,
    predictions_path: Path,
    model_name: str,
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
        record = benchmark.format_prediction(spec, patch, model_name)
        write_prediction(predictions_path, record)
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
    benchmark = get_benchmark(args.dataset)
    log(f"Model name: {args.model_name}")

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
        "model_name": args.model_name,
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
            process_instance(
                spec, run_dir, benchmark, agent, args.agent_timeout,
                predictions_path, args.model_name,
            )
        )

    write_manifest(run_dir, results, args)
    print_summary(results)
    return 0 if all(r.status == "completed" for r in results) else 2
