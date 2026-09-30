"""Agent runner interface: how task instances get worked on (step 3).

To add an agent, implement :meth:`AgentRunner.run` and register it::

    class MyAgent(AgentRunner):
        def run(self, spec, workspace, log_dir, timeout) -> AgentOutcome: ...

    register_agent("mine", MyAgent)

then select it with ``--agent mine``. Importing ``agents`` registers the
built-in Pi runner under the ``pi`` name.

The ``run()`` contract:
  * Work inside ``workspace`` (fresh clone at ``spec.base_commit``); leave all
    changes uncommitted in the working tree.
  * Write artifacts into ``log_dir`` (the pipeline owns meta.json/patch.diff).
  * Return an AgentOutcome; ``status == "ok"`` means the agent finished
    cleanly (completed vs no_patch is decided from the captured diff).
  * ``usage``, if set, should carry ``input_tokens`` and ``output_tokens``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from benchmarks import InstanceSpec

_PROMPT_TEMPLATE = (Path(__file__).parent / "prompt.md").read_text(encoding="utf-8")


@dataclass
class AgentOutcome:
    """Result of one agent run: all the pipeline needs to know."""

    status: str  # "ok" | "agent_error" | "agent_timeout" | ...
    detail: str = ""
    duration_s: float = 0.0
    usage: Optional[dict] = None


class AgentRunner(ABC):
    """An agent that works on one instance inside a prepared workspace."""

    def __init__(
        self, name: str, model: str, extra_args: Optional[list[str]] = None
    ) -> None:
        self.name = name
        self.model = model
        self.extra_args = list(extra_args or [])

    def build_prompt(self, spec: InstanceSpec) -> str:
        """Task prompt from prompt.md; subclasses may override."""
        return _PROMPT_TEMPLATE.format(
            repo=spec.repo,
            instance_id=spec.instance_id,
            problem_statement=spec.problem_statement.strip(),
        )

    @abstractmethod
    def run(
        self, spec: InstanceSpec, workspace: Path, log_dir: Path, timeout: int
    ) -> AgentOutcome:
        """Work on ``spec`` in ``workspace``; see the module docstring."""


_REGISTRY: dict[str, type[AgentRunner]] = {}


def register_agent(name: str, cls: type[AgentRunner]) -> None:
    """Register an agent class under the ``--agent <name>`` namespace."""
    _REGISTRY[name] = cls


def get_agent(
    name: str, model: str, extra_args: Optional[list[str]] = None
) -> AgentRunner:
    """Resolve a ``--agent`` value to an AgentRunner instance."""
    known = ", ".join(sorted(_REGISTRY)) or "(none)"
    if not name or not name.strip():
        raise ValueError(
            f"Agent is not set: provide one via --agent (known agents: {known})"
        )
    if name not in _REGISTRY:
        raise ValueError(f"Unknown agent '{name}' (known: {known})")
    return _REGISTRY[name](name, model, extra_args)
