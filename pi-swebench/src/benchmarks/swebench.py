"""SWE-bench benchmark: instances from a SWE-bench-format HF dataset."""

from __future__ import annotations

from pi_logging import log

from benchmarks.base import Benchmark, InstanceSpec, register_benchmark

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


# Built-in: addressable as "swebench:<name>". There is no default benchmark
# and no default dataset — the prefix form is always required.
register_benchmark("swebench", SWEBenchBenchmark)
