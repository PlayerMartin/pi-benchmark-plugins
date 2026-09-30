"""Benchmark registry for the pi-swebench pipeline (step 1: dataset loading).

Public API:

    InstanceSpec                - the task spec the rest of the pipeline consumes
    Benchmark                   - abstract base; implement _load() to add one
    register_benchmark          - register a class under a ``prefix:`` namespace
    get_benchmark               - resolve a --dataset value to a Benchmark
    load_instances              - convenience: load + skip/limit in one call
    resolve_dataset             - canonical dataset name for metadata/harness

Importing this package registers the built-in SWE-bench loader
(benchmarks/swebench.py) under the ``swebench:`` prefix. There is no default
benchmark and no default dataset — dataset identifiers are always in
``prefix:name`` form. See benchmarks/base.py for how to plug in other
benchmarks.
"""

from benchmarks.base import (
    Benchmark,
    InstanceSpec,
    get_benchmark,
    load_instances,
    register_benchmark,
    resolve_dataset,
)
from benchmarks.swebench import SWEBenchBenchmark

__all__ = [
    "Benchmark",
    "InstanceSpec",
    "SWEBenchBenchmark",
    "get_benchmark",
    "load_instances",
    "register_benchmark",
    "resolve_dataset",
]
