"""Benchmark registry for the bench pipeline (step 1: dataset loading).

Public API:

    InstanceSpec                - the task spec the rest of the pipeline consumes
    Benchmark                   - abstract base; implement the wrapper to add one
    register_benchmark          - register a class under a ``prefix:`` namespace
    get_benchmark               - resolve a --dataset value to a Benchmark
    load_instances              - convenience: load + skip/limit in one call
    resolve_dataset             - canonical dataset name for metadata/harness
    DEFAULT_SPLIT               - split used when none is given

Importing this package registers the built-in benchmark loaders shipped in
this package. There is no default benchmark and no default dataset — dataset
identifiers are always in ``prefix:name`` form. See benchmarks/base.py for
how to plug in other benchmarks.
"""

from benchmarks.base import (
    DEFAULT_SPLIT,
    Benchmark,
    InstanceSpec,
    get_benchmark,
    load_instances,
    register_benchmark,
    resolve_dataset,
)
from benchmarks.swebench import SWEBenchBenchmark

__all__ = [
    "DEFAULT_SPLIT",
    "Benchmark",
    "InstanceSpec",
    "SWEBenchBenchmark",
    "get_benchmark",
    "load_instances",
    "register_benchmark",
    "resolve_dataset",
]
