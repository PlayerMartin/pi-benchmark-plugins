"""CLI argument registration for run_pi_swebench.py."""

from __future__ import annotations

import argparse
from typing import Callable

DEFAULT_AGENT_TIMEOUT = 1800  # seconds per instance


def build_parser(
    description: str, funcs: dict[str, Callable[[argparse.Namespace], int]]
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=description, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list dataset instance ids")
    _add_instance_selection(p_list)
    p_list.set_defaults(func=funcs["list"])

    p_run = sub.add_parser("run", help="run the full pipeline (steps 1-4)")
    _add_instance_selection(p_run)
    p_run.add_argument(
        "--agent",
        required=True,
        help="agent runner to use (see the agents registry)",
    )
    p_run.add_argument(
        "--agent-model", required=True, help="model for the agent, e.g. sonnet:high"
    )
    p_run.add_argument(
        "--agent-extra-args", default="", help="extra args passed verbatim to the agent"
    )
    p_run.add_argument(
        "--agent-timeout",
        type=int,
        default=DEFAULT_AGENT_TIMEOUT,
        help="seconds per instance",
    )
    p_run.set_defaults(func=funcs["run"])

    p_eval = sub.add_parser("evaluate", help="run the SWE-bench harness (step 5)")
    p_eval.add_argument("--predictions", required=True, help="predictions JSONL path")
    p_eval.add_argument(
        "--dataset", required=True, help="dataset the predictions are from"
    )
    p_eval.set_defaults(func=funcs["evaluate"])

    p_rep = sub.add_parser("report", help="summarize a harness report")
    p_rep.add_argument("--run-id", required=True, help="harness run id to summarize")
    p_rep.set_defaults(func=funcs["report"])

    return parser


def parse_args(
    argv: list[str] | None,
    description: str,
    funcs: dict[str, Callable[[argparse.Namespace], int]],
) -> argparse.Namespace:
    args = build_parser(description, funcs).parse_args(argv)
    value = args.agent_extra_args if hasattr(args, "agent_extra_args") else None
    if isinstance(value, str):
        args.agent_extra_args = value.split() if value.strip() else []
    return args


def _add_instance_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--dataset",
        required=True,
        help="benchmark:dataset identifier, e.g. swebench:SWE-bench/SWE-bench_Lite",
    )
    p.add_argument("--skip", type=int, default=0, help="skip the first N instances")
    p.add_argument(
        "--limit", "-l", type=int, default=0, help="take the next N (0 = all)"
    )
