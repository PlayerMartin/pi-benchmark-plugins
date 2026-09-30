#!/usr/bin/env python3
"""
Automatic SWE-bench testing framework for the Pi coding agent.

Pipeline (one instance at a time):

  1. DATASET LOADER    -> load instance spec (instance_id, repo, base_commit,
                          problem_statement) from a SWE-bench dataset on HF.
  2. REPO PREPARER     -> fresh clone of the repo at base_commit into an
                          isolated workspace directory (per-run, no dirty state).
  3. PI AGENT RUNNER   -> run `pi --mode json` with the problem_statement as
                          the task prompt, inside the workspace, under a
                          timeout. The full event stream (every LLM message,
                          tool call and tool result, plus token usage/cost) is
                          captured to logs/<instance>/events.jsonl and rendered
                          to a readable transcript.md.
  4. FORMATTER         -> capture `git diff` vs base_commit as a unified diff
                          (staged incl. untracked, binaries dropped) and write
                          the SWE-bench predictions JSONL.
  5. EVALUATION        -> hand the predictions to the SWE-bench harness
                          (Docker + Linux / WSL required) and report results.

Each `run` invocation writes everything into one timestamped directory:

    runs/<YYYYmmdd-HHMMSS>/
      run.json                  # run metadata: model, dataset, instances
      predictions.jsonl
      manifest.json             # results, totals (cost/tokens), status
      logs/<instance_id>/
        meta.json              # model, task, prompt, status, usage, artifacts
        events.jsonl           # full pi event stream (all LLM instructions)
        transcript.md          # human-readable chat history
        patch.diff
        pi.stderr.log

Usage examples:
    python run_pi_swebench.py list --dataset swebench:SWE-bench/SWE-bench_Lite --limit 10
    python run_pi_swebench.py run --dataset swebench:SWE-bench/SWE-bench_Lite \
        --skip 10 --limit 5 --pi-model sonnet:high
    python run_pi_swebench.py evaluate --predictions runs/<timestamp>/predictions.jsonl \
        --dataset swebench:SWE-bench/SWE-bench_Lite
    python run_pi_swebench.py report --run-dir runs
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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

# Step 1 (dataset loading) lives in the `benchmarks` package; InstanceSpec,
# load_instances and resolve_dataset are imported from it. There is no default
# dataset: --dataset must be given explicitly.

DEFAULT_SPLIT = "test"
DEFAULT_MODEL_NAME = "pi-agent"
DEFAULT_RUN_DIR = "runs"
DEFAULT_PI_TIMEOUT = 1800  # seconds per instance

# ---------------------------------------------------------------------------
# Data model (task specs come from the `benchmarks` package)
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    instance_id: str
    status: str  # completed | no_patch | pi_error | pi_timeout | prep_error
    duration_s: float = 0.0
    patch: str = ""
    error: str = ""
    log_dir: str = ""
    usage: Optional[dict] = None


# ---------------------------------------------------------------------------
# Small helpers (repo/git plumbing and workspace preparation live in repo_prep;
# logging lives in pi_logging)
# ---------------------------------------------------------------------------


def resolve_pi_cmd(explicit: Optional[str]) -> str:
    """Find the pi CLI. On Windows this resolves pi.cmd via PATHEXT."""
    if explicit:
        found = shutil.which(explicit) or explicit
        return found
    for name in ("pi", "pi.cmd", "pi.exe"):
        found = shutil.which(name)
        if found:
            return found
    raise FileNotFoundError(
        "Could not find the 'pi' CLI on PATH. Install it with "
        "`npm install -g @earendil-works/pi-coding-agent` or pass --pi-cmd."
    )



# ---------------------------------------------------------------------------
# Step 3: Pi agent runner -> (worked on the repo, we capture the diff)
# ---------------------------------------------------------------------------


def build_prompt(spec: InstanceSpec) -> str:
    return f"""You are an autonomous software engineering agent. Your current working \
directory is a clone of the repository {spec.repo}.

Resolve the following issue:

--- BEGIN ISSUE: {spec.instance_id} ---
{spec.problem_statement.strip()}
--- END ISSUE ---

Instructions:
- Edit the source code in this repository to fix the issue described above.
- Keep the change minimal and focused: only modify what is necessary.
- You may add new files if needed, and you may inspect, search, and run commands
  to understand the codebase.
- Do NOT create git commits, branches, or tags; leave all changes uncommitted
  in the working tree. Never touch the .git directory.
- Test dependencies may not be installed in this environment; running the full
  test suite is optional.
- Finish with a one-paragraph summary of what you changed and why.
"""


def run_pi(
    spec: InstanceSpec,
    workspace: Path,
    pi_cmd: str,
    pi_extra_args: list[str],
    timeout: int,
    log_dir: Path,
) -> dict:
    """
    Run Pi headless in the workspace in JSON event mode.

    Pi writes its full event stream -- every LLM message (the exact
    instructions sent and received), every tool call and tool result, plus
    per-response token usage and cost -- to logs/<instance>/events.jsonl via
    stdout. The prompt goes in via stdin (avoids Windows command-line length
    limits); Pi prepends piped stdin to the first prompt. Diagnostics go to
    pi.stderr.log. Returns a dict with status/detail/duration/usage.
    """
    prompt = build_prompt(spec)
    args = [
        pi_cmd,
        "--mode", "json",  # structured event stream on stdout, not final text
        "--no-session",  # don't pollute the persistent session store
        *pi_extra_args,
        "Fix the GitHub issue described below. Work directly in this repository.",
    ]
    log_dir.mkdir(parents=True, exist_ok=True)
    events_file = log_dir / "events.jsonl"
    stderr_file = log_dir / "pi.stderr.log"
    start = time.time()
    with open(events_file, "w", encoding="utf-8") as events_fh, open(
        stderr_file, "w", encoding="utf-8"
    ) as stderr_fh:
        try:
            proc = subprocess.run(
                args,
                cwd=str(workspace),
                input=prompt,
                stdout=events_fh,
                stderr=stderr_fh,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
            status = "ok" if proc.returncode == 0 else f"pi_error(exit={proc.returncode})"
            detail = ""
        except subprocess.TimeoutExpired:
            status = "pi_timeout"
            detail = f"Pi did not finish within {timeout}s; partial work kept."
        except Exception as e:  # noqa: BLE001
            status = "pi_error"
            detail = f"{type(e).__name__}: {e}"

        duration = time.time() - start
        if not detail:
            tail = stderr_file.read_text(encoding="utf-8", errors="replace").strip()
            detail = tail[-4000:]

    usage = condense_events(events_file)
    return {
        "status": status,
        "detail": detail,
        "duration_s": duration,
        "events_file": str(events_file),
        "usage": usage,
    }


def condense_events(events_file: Path) -> dict:
    """
    Keep events.jsonl compact: drop streaming `message_update` deltas (the
    authoritative `message_end` records already carry the full messages) and
    aggregate token usage / cost across assistant responses. Rewrites the
    file in place; returns the usage totals.
    """
    kept: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cost_usd": 0.0,
        "responses": 0,
    }
    pending_usage: dict | None = None
    text = events_file.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # truncated tail after a timeout/kill
        if rec.get("type") == "message_update":
            if rec.get("usage"):
                pending_usage = rec["usage"]
            continue
        if rec.get("type") == "message_end":
            msg = rec.get("message") or {}
            if msg.get("role") == "assistant":
                u = msg.get("usage") or pending_usage
                if u:
                    usage["input_tokens"] += u.get("input", 0)
                    usage["output_tokens"] += u.get("output", 0)
                    usage["cache_read_tokens"] += u.get("cacheRead", 0)
                    usage["cache_write_tokens"] += u.get("cacheWrite", 0)
                    usage["cost_usd"] += (u.get("cost") or {}).get("total", 0)
                    usage["responses"] += 1
                pending_usage = None
        kept.append(json.dumps(rec))
    events_file.write_text(
        "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8"
    )
    return usage


def render_transcript(
    events_file: Path,
    out_path: Path,
    spec: InstanceSpec,
    model: str,
    status: str,
    duration_s: float,
    usage: dict,
) -> None:
    """Render a human-readable chat history (markdown) from the event stream."""

    def render_content(content) -> str:
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content or []:
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "thinking":
                thinking = "\n> ".join(block.get("thinking", "").splitlines())
                parts.append(f"> [thinking]\n> {thinking}")
            elif btype == "toolCall":
                call = json.dumps(block.get("arguments", {}), indent=2)
                parts.append(f"**tool call `{block.get('name')}`**\n```json\n{call}\n```")
        return "\n\n".join(p for p in parts if p)

    def render_tool_result(result) -> str:
        chunks = [
            block.get("text", "")
            for block in (result or {}).get("content") or []
            if block.get("type") == "text"
        ]
        text = "\n".join(chunks)
        if len(text) > 4000:
            text = (
                text[:2000]
                + "\n... [truncated; full result in events.jsonl] ...\n"
                + text[-1000:]
            )
        return text

    lines = [
        f"# Transcript: {spec.instance_id}",
        "",
        f"- **repo**: {spec.repo} @ `{spec.base_commit[:12]}`",
        f"- **model**: `{model}`",
        f"- **status**: {status}",
        f"- **duration**: {duration_s:.0f}s",
        f"- **usage**: {usage['input_tokens']} in / {usage['output_tokens']} out tokens, "
        f"${usage['cost_usd']:.4f}, {usage['responses']} responses",
        "",
        "---",
        "",
    ]
    text = events_file.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        rtype = rec.get("type")
        if rtype == "message_end":
            msg = rec.get("message") or {}
            lines.append(f"## {msg.get('role', '?').title()}\n")
            lines.append(render_content(msg.get("content")))
            lines.append("")
        elif rtype == "tool_execution_end":
            name = rec.get("toolName", "?")
            flag = "error" if rec.get("isError") else "ok"
            lines.append(f"### tool result `{name}` ({flag})\n")
            lines.append(f"```\n{render_tool_result(rec.get('result'))}\n```")
            lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Step 4: Formatter -> clean unified diff -> predictions JSONL
# ---------------------------------------------------------------------------


def clean_diff(raw_diff: str) -> str:
    """
    Drop binary-file sections and trailing whitespace noise so the harness can
    `git apply` the patch cleanly. Untracked files are already included because
    we diff the index (staged with `git add -A`) against base_commit.
    """
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
    # Stage everything so untracked files appear; respect .gitignore (noise stays out).
    git(workspace, "add", "-A")
    raw = git(workspace, "diff", "--cached", spec.base_commit)
    # Leave the workspace as found (staging is harmless, but be tidy).
    git(workspace, "reset", "--quiet", check=False)
    return clean_diff(raw)


def write_prediction(out_path: Path, spec: InstanceSpec, patch: str) -> None:
    """One JSON line: the sole interface into the SWE-bench harness.

    SWE-bench (>=5) expects the patch under the `model_patch` key.
    """
    record = {
        "instance_id": spec.instance_id,  # must match the dataset string exactly
        "model_name_or_path": DEFAULT_MODEL_NAME,
        "model_patch": patch,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


# ---------------------------------------------------------------------------
# Orchestration: one instance through steps 2-4
# ---------------------------------------------------------------------------


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
    """Per-instance metadata: model, task, prompt, status, usage, artifacts."""
    meta = {
        "instance_id": spec.instance_id,
        "repo": spec.repo,
        "base_commit": spec.base_commit,
        "model": model,
        "status": status,
        "duration_s": round(duration_s, 1),
        "patch_lines": patch_lines,
        "error": error[:2000],
        "artifacts": {
            "events": "events.jsonl",
            "transcript": "transcript.md",
            "patch": "patch.diff",
            "stderr": "pi.stderr.log",
        },
    }
    if usage:
        meta["usage"] = usage
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "meta.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )


def process_instance(
    spec: InstanceSpec,
    run_dir: Path,
    pi_cmd: str,
    pi_extra_args: list[str],
    pi_timeout: int,
    predictions_path: Path,
    pi_model: str,
) -> RunResult:
    log(f"=== {spec.instance_id} ===")
    # Mirrors are expensive to clone, so they are shared across runs; every
    # run gets its own timestamped directory with its own logs and workspaces.
    mirror_root = run_dir.parent / "mirrors"
    mirror_root.mkdir(parents=True, exist_ok=True)
    ws_root = run_dir / "workspaces"
    ws_root.mkdir(parents=True, exist_ok=True)
    workspace = ws_root / spec.instance_id
    log_dir = run_dir / "logs" / spec.instance_id
    start = time.time()

    try:
        mirror = ensure_mirror(spec.repo, mirror_root)
        prepare_workspace(spec, mirror, workspace)
    except Exception as e:  # noqa: BLE001
        log(f"  PREP FAILED: {e}")
        write_instance_meta(
            log_dir, spec, pi_model, "prep_error", time.time() - start, None, 0, str(e)
        )
        return RunResult(
            instance_id=spec.instance_id,
            status="prep_error",
            error=str(e),
            duration_s=time.time() - start,
            log_dir=str(log_dir),
        )

    outcome = run_pi(spec, workspace, pi_cmd, pi_extra_args, pi_timeout, log_dir)
    status, detail, usage = outcome["status"], outcome["detail"], outcome["usage"]

    try:
        patch = capture_patch(spec, workspace)
    except Exception as e:  # noqa: BLE001
        log(f"  DIFF CAPTURE FAILED: {e}")
        render_transcript(
            log_dir / "events.jsonl",
            log_dir / "transcript.md",
            spec,
            pi_model,
            status,
            outcome["duration_s"],
            usage,
        )
        write_instance_meta(
            log_dir, spec, pi_model, "pi_error", time.time() - start, usage, 0,
            f"diff capture: {e}",
        )
        return RunResult(
            instance_id=spec.instance_id,
            status="pi_error",
            error=f"diff capture: {e}",
            duration_s=time.time() - start,
            log_dir=str(log_dir),
            usage=usage,
        )

    patch_file = log_dir / "patch.diff"
    if patch:
        patch_file.write_text(patch, encoding="utf-8")
        write_prediction(predictions_path, spec, patch)
        final_status = "completed" if status == "ok" else status
        log(
            f"  patch: {len(patch.splitlines())} diff lines -> {patch_file.name} "
            f"({final_status}, {time.time() - start:.0f}s, "
            f"${usage['cost_usd']:.4f})"
        )
    else:
        final_status = "no_patch" if status == "ok" else status
        log(f"  EMPTY PATCH ({final_status}, {time.time() - start:.0f}s)")

    render_transcript(
        log_dir / "events.jsonl",
        log_dir / "transcript.md",
        spec,
        pi_model,
        status,
        outcome["duration_s"],
        usage,
    )
    write_instance_meta(
        log_dir,
        spec,
        pi_model,
        final_status,
        time.time() - start,
        usage,
        len(patch.splitlines()) if patch else 0,
        detail if status != "ok" else "",
    )

    robust_rmtree(workspace)

    return RunResult(
        instance_id=spec.instance_id,
        status=final_status,
        duration_s=time.time() - start,
        patch=patch,
        error=detail if status != "ok" else "",
        log_dir=str(log_dir),
        usage=usage,
    )


def cmd_run(args: argparse.Namespace) -> int:
    specs = load_instances(
        args.dataset, DEFAULT_SPLIT, limit=args.limit, skip=args.skip
    )
    if not specs:
        log("No instances selected.")
        return 1
    log(f"Selected {len(specs)} instance(s): {', '.join(s.instance_id for s in specs)}")

    pi_cmd = resolve_pi_cmd(None)
    pi_extra_args = ["--model", args.pi_model, *args.pi_extra_args]
    log(
        f"Pi CLI: {pi_cmd} | model: {args.pi_model} | "
        f"extra args: {args.pi_extra_args or '(none)'}"
    )

    # Every run gets its own timestamped directory holding all of its
    # artifacts: run.json, predictions, per-instance logs, workspaces.
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
        "pi_cmd": pi_cmd,
        "pi_model": args.pi_model,
        "pi_extra_args": args.pi_extra_args,
        "pi_timeout": args.pi_timeout,
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
                spec,
                run_dir,
                pi_cmd,
                pi_extra_args,
                args.pi_timeout,
                predictions_path,
                args.pi_model,
            )
        )

    write_manifest(run_dir, results, args, pi_cmd)
    print_summary(results)
    return 0 if all(r.status == "completed" for r in results) else 2


def write_manifest(
    run_dir: Path, results: list[RunResult], args: argparse.Namespace, pi_cmd: str
) -> None:
    manifest = {
        "run_id": run_dir.name,
        "dataset": resolve_dataset(args.dataset),
        "split": DEFAULT_SPLIT,
        "model_name": DEFAULT_MODEL_NAME,
        "pi_cmd": pi_cmd,
        "pi_model": args.pi_model,
        "pi_extra_args": args.pi_extra_args,
        "pi_timeout": args.pi_timeout,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "totals": {
            "instances": len(results),
            "total_cost_usd": round(
                sum(r.usage["cost_usd"] for r in results if r.usage), 4
            ),
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
        if r.status.endswith("error") or r.status == "pi_timeout"
    ]
    if failed:
        log(f"  errored/timed out: {', '.join(failed)}")


# ---------------------------------------------------------------------------
# Step 5: SWE-bench harness -> Result
# ---------------------------------------------------------------------------


def normalize_predictions(preds: Path) -> Path:
    """Normalize the predictions file for SWE-bench >= 5.

    The harness reads `model_patch`; some predictions files (and older versions
    of this script) used `patch`. If any record needs renaming, write a sibling
    `.normalized.jsonl` and return that path; otherwise return the input.
    """
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
    """Run the SWE-bench harness over the predictions file.

    NOTE: the harness needs Docker and a Linux environment. On Windows run it
    under WSL by passing e.g. --eval-python "wsl python" (see README).
    """
    preds = Path(args.predictions)
    if not preds.exists():
        log(f"Predictions file not found: {preds}")
        return 1
    preds = normalize_predictions(preds)
    n = sum(1 for _ in open(preds, encoding="utf-8"))
    log(f"Evaluating {n} prediction(s) from {preds}")

    cmd = [
        *args.eval_python.split(),
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        resolve_dataset(args.dataset),
        "--split",
        DEFAULT_SPLIT,
        "--predictions_path",
        str(preds),
        "--run_id",
        args.run_id,
        "--max_workers",
        str(args.max_workers),
        "--report_dir",
        args.report_dir,
        *args.eval_extra_args,
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
    if args.report_json:
        candidates = [Path(args.report_json)]
    else:
        report_dir = getattr(args, "report_dir", None) or "logs"
        found: dict[Path, None] = {}
        for pat in (
            f"{report_dir}/*.{args.run_id}*.json",                 # aggregate
            f"logs/*.{args.run_id}*.json",                         # aggregate fallback
            f"logs/run_evaluation/{args.run_id}*/**/report.json",  # per-instance
            f"logs/run_evaluation/{args.run_id}*/report.json",     # legacy
        ):
            for p in Path(".").glob(pat):
                found[p.resolve()] = None
        candidates = sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

    if not candidates:
        log(
            "No report found. Expected logs/<model>.<run_id>.json or "
            "logs/run_evaluation/<run_id>/.../report.json; pass --report-json PATH."
        )
        return 1

    # Prefer the aggregate report (it carries 'resolved_ids').
    for path in candidates:
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

    # No aggregate: summarize every per-instance report we found.
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    specs = load_instances(
        args.dataset, DEFAULT_SPLIT, limit=args.limit, skip=args.skip
    )
    for s in specs:
        print(s.instance_id)
    log(f"{len(specs)} instance(s).")
    return 0


def add_instance_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dataset",
        required=True,
        help="benchmark:dataset identifier, e.g. swebench:SWE-bench/SWE-bench_Lite",
    )
    p.add_argument(
        "--skip", type=int, default=0, help="skip the first N instances"
    )
    p.add_argument(
        "--limit", "-l", type=int, default=0, help="take the next N (0 = all)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list dataset instance ids")
    add_instance_selection(p_list)
    p_list.set_defaults(func=cmd_list)

    p_run = sub.add_parser("run", help="run the full pipeline (steps 1-4)")
    add_instance_selection(p_run)
    p_run.add_argument(
        "--pi-model",
        required=True,
        help="model for Pi, passed as 'pi --model <value>', e.g. sonnet:high",
    )
    p_run.add_argument(
        "--pi-extra-args",
        default="",
        help="extra args passed verbatim to pi",
    )
    p_run.add_argument(
        "--pi-timeout",
        type=int,
        default=DEFAULT_PI_TIMEOUT,
        help="seconds per instance",
    )
    p_run.set_defaults(func=cmd_run)

    p_eval = sub.add_parser("evaluate", help="run the SWE-bench harness (step 5)")
    p_eval.add_argument("--predictions", required=True, help="predictions JSONL path")
    p_eval.add_argument("--dataset", required=True, help="dataset the predictions are from")
    p_eval.add_argument("--run-id", default=f"pi-{time.strftime('%Y%m%d-%H%M%S')}")
    p_eval.add_argument(
        "--eval-workers", "--max-workers", type=int, default=4, dest="max_workers"
    )
    p_eval.add_argument(
        "--eval-python",
        default=sys.executable,
        help="python (or 'wsl python') used to run the harness",
    )
    p_eval.add_argument("--eval-extra-args", default="", help="extra harness args")
    p_eval.add_argument(
        "--report-dir",
        default="logs",
        help="harness --report_dir (must be a mounted path; default logs)",
    )
    p_eval.set_defaults(func=cmd_evaluate)

    p_rep = sub.add_parser("report", help="summarize a harness report")
    p_rep.add_argument("--run-id", default="pi", help="run id prefix to search for")
    p_rep.add_argument(
        "--report-json", default=None, help="explicit path to report.json"
    )
    p_rep.add_argument(
        "--report-dir", default="logs", help="harness report dir (default logs)"
    )
    p_rep.set_defaults(func=cmd_report)

    args = parser.parse_args()
    if getattr(args, "pi_extra_args", None) and isinstance(args.pi_extra_args, str):
        args.pi_extra_args = (
            args.pi_extra_args.split() if args.pi_extra_args.strip() else []
        )
    if getattr(args, "eval_extra_args", None) is not None and isinstance(
        args.eval_extra_args, str
    ):
        args.eval_extra_args = (
            args.eval_extra_args.split() if args.eval_extra_args.strip() else []
        )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
