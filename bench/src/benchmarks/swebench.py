"""SWE-bench benchmark: instances, predictions and evaluation via its harness.

Concrete wrapper for anything SWE-bench-shaped: instance specs come from a
SWE-bench-format HF dataset, predictions use the SWE-bench JSONL schema, and
evaluation runs the official SWE-bench harness (Docker required).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from pi_logging import log

from benchmarks.base import (
    DEFAULT_SPLIT,
    Benchmark,
    InstanceSpec,
    register_benchmark,
)

# SWE-bench >= 5 needs the re-released datasets that carry `image`, `eval_type`,
# `eval_script` and `log_parser`. Map the legacy `princeton-nlp/...` names onto
# their `SWE-bench/...` counterparts so old configs keep working.
DATASET_ALIASES = {
    "princeton-nlp/SWE-bench_Lite": "SWE-bench/SWE-bench_Lite",
    "princeton-nlp/SWE-bench_Verified": "SWE-bench/SWE-bench_Verified",
}


class SWEBenchBenchmark(Benchmark):
    """Loads SWE-bench-format rows from a Hugging Face dataset.

    Any SWE-bench-format dataset works: instance_id / repo / base_commit /
    problem_statement are read verbatim from each row. There is no default
    dataset — a name must be given (get_benchmark enforces this); legacy
    names are aliased to their re-releases.
    """

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.canonical_name = DATASET_ALIASES.get(name, name)

    def _load(self, split: str) -> list[InstanceSpec]:
        from datasets import load_dataset  # deferred: heavy import

        log(f"Loading dataset {self.canonical_name} (split={split}) ...")
        ds = load_dataset(self.canonical_name, split=split)
        log(f"Loaded {len(ds)} instances total.")
        return [
            InstanceSpec(
                instance_id=row["instance_id"],
                repo=row["repo"],
                base_commit=row["base_commit"],
                problem_statement=row["problem_statement"],
            )
            for row in ds
        ]

    # ------------------------------------------------------------------
    # Predictions (SWE-bench JSONL schema)
    # ------------------------------------------------------------------

    def format_prediction(
        self, spec: InstanceSpec, patch: str, model_name: str
    ) -> dict:
        return {
            "instance_id": spec.instance_id,
            "model_name_or_path": model_name,
            "model_patch": patch,
        }

    @staticmethod
    def _normalize_predictions(preds: Path) -> Path:
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

    # ------------------------------------------------------------------
    # Evaluation (official SWE-bench harness; needs Docker)
    # ------------------------------------------------------------------

    def evaluate(self, preds: Path, run_id: str, max_workers: int = 1) -> int:
        preds = self._normalize_predictions(preds)
        cmd = [
            sys.executable,
            "-m",
            "swebench.harness.run_evaluation",
            "--dataset_name",
            self.canonical_name,
            "--split",
            DEFAULT_SPLIT,
            "--predictions_path",
            str(preds),
            "--run_id",
            run_id,
            "--max_workers",
            str(max_workers),
            "--report_dir",
            "logs",
        ]
        log("Harness command: " + " ".join(cmd))
        proc = subprocess.run(cmd)
        return proc.returncode

    def summarize_report(self, run_id: str) -> int:
        """Best-effort: find the harness report and print a summary.

        Handles both report shapes produced by SWE-bench >= 5:
          * aggregate      logs/<model>.<run_id>.json              (schema_version 2)
          * per-instance   logs/run_evaluation/<run_id>/<model>/<instance>/report.json
        plus the legacy  logs/run_evaluation/<run_id>*/report.json.
        """
        found: dict[Path, None] = {}
        for pat in (
            f"logs/*.{run_id}*.json",
            f"logs/run_evaluation/{run_id}*/**/report.json",
            f"logs/run_evaluation/{run_id}*/report.json",
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


# Built-in: addressable as "swebench:<name>". There is no default benchmark
# and no default dataset — the prefix form is always required.
register_benchmark("swebench", SWEBenchBenchmark)
