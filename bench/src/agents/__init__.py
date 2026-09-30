"""Agent runner registry for the bench pipeline (step 3: agent runner).

Public API:

    AgentRunner   - abstract base; implement run() to add an agent
    AgentOutcome  - result of one agent run (status, usage, ...)
    register_agent - register a class under a ``--agent <name>`` namespace
    get_agent      - resolve a ``--agent`` value to an AgentRunner

Importing this package registers the built-in Pi runner
(agents/pi_runner.py, under the ``pi`` name). See agents/base.py for how to
plug in other agents.
"""

from agents import pi_runner  # noqa: F401  (registers the built-in "pi" agent)
from agents.base import AgentOutcome, AgentRunner, get_agent, register_agent

__all__ = [
    "AgentOutcome",
    "AgentRunner",
    "get_agent",
    "register_agent",
]
