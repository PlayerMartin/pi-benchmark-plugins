"""Benchmark interface: how task instances enter the pipeline (step 1).

A *benchmark* is a source of :class:`InstanceSpec`. The pipeline only ever
asks a Benchmark for a list of specs and then works off those; it never talks
to Hugging Face or any other backend directly.

To support a new benchmark, implement ``_load()`` and register the class::

    from benchmarks.base import Benchmark, InstanceSpec, register_benchmark

    class MyBenchmark(Benchmark):
        def _load(self, split: str) -> list[InstanceSpec]:
            ...

    register_benchmark("mine", MyBenchmark)

and select it on the CLI with ``--dataset mine:<anything>`` (the part after
the colon is passed to the class verbatim). There is no default benchmark
and no default dataset: a ``prefix:name`` identifier must always be given,
and an empty one fails with a "dataset is not set" error.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from pi_logging import log  # noqa: F401  (re-exported for subclass convenience)


@dataclass
class InstanceSpec:
    """Task spec: the only source of truth for 'what to fix'."""

    instance_id: str
    repo: str  # e.g. "django/django"
    base_commit: str
    problem_statement: str

    @property
    def repo_slug(self) -> str:
        return self.repo.replace("/", "__")


class Benchmark(ABC):
    """A source of InstanceSpecs.

    Subclasses implement only ``_load(split)``. Instance selection (skip N,
    then take M) is applied generically in :meth:`load_instances`, so every
    benchmark shares the same selection semantics.
    """

    def __init__(self, name: str) -> None:
        #: Dataset name as requested on the CLI, e.g.
        #: "swebench:SWE-bench/SWE-bench_Lite" (always prefix:rest form).
        self.name = name
        #: Canonical name after alias resolution; used in run metadata and by
        #: evaluation tooling. Subclasses may rewrite it in ``__init__``.
        self.canonical_name = name

    def load_instances(
        self, split: str, limit: int = 0, skip: int = 0
    ) -> list[InstanceSpec]:
        """Load this benchmark's instances, then select deterministically.

        ``skip > 0`` skips the first N instances; ``limit > 0`` then takes the
        next N (0 = all remaining). Selection is deterministic.
        """
        specs = self._load(split)
        if skip > 0:
            specs = specs[skip:]
        if limit > 0:
            specs = specs[:limit]
        return specs

    @abstractmethod
    def _load(self, split: str) -> list[InstanceSpec]:
        """Every instance of the benchmark for ``split``, in benchmark order."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[Benchmark]] = {}


def register_benchmark(prefix: str, cls: type[Benchmark]) -> None:
    """Register a benchmark class under the ``<prefix>:`` namespace."""
    _REGISTRY[prefix] = cls


def get_benchmark(name: str) -> Benchmark:
    """Resolve a ``--dataset`` value to a Benchmark instance.

    The name must be in ``prefix:rest`` form (e.g.
    ``swebench:SWE-bench/SWE-bench_Lite``). There is no default benchmark
    and no default dataset: empty, unprefixed, or unknown names all raise
    a visible error.
    """
    if not name or not name.strip():
        raise ValueError(
            "Dataset is not set: provide one via --dataset "
            "(e.g. --dataset swebench:SWE-bench/SWE-bench_Lite)"
        )
    prefix, sep, rest = name.partition(":")
    known = ", ".join(sorted(_REGISTRY)) or "(none)"
    if not sep:
        raise ValueError(
            f"Dataset '{name}' has no benchmark prefix; use '<prefix>:<dataset>' "
            f"(e.g. swebench:{name}; known benchmarks: {known})"
        )
    if not rest.strip():
        raise ValueError(
            "Dataset is not set: provide one after the benchmark prefix "
            "(e.g. --dataset swebench:SWE-bench/SWE-bench_Lite)"
        )
    if prefix not in _REGISTRY:
        raise ValueError(f"Unknown benchmark '{prefix}:' (known: {known})")
    return _REGISTRY[prefix](rest)


def load_instances(
    dataset: str, split: str, limit: int = 0, skip: int = 0
) -> list[InstanceSpec]:
    """Load instances from any benchmark (see get_benchmark).

    ``skip`` drops the first N instances; ``limit`` then takes the next N.
    Raises ValueError when no dataset is set.
    """
    return get_benchmark(dataset).load_instances(split, limit=limit, skip=skip)


def resolve_dataset(name: str) -> str:
    """Canonical dataset name for ``name`` (run metadata, evaluation harness)."""
    return get_benchmark(name).canonical_name
