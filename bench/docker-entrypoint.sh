#!/usr/bin/env bash
#
# Entrypoint for the bench container. It is agent- and benchmark-agnostic:
# any agent (src/agents/) and any benchmark (src/benchmarks/) can be used;
# the bundled ones (pi, swebench) are just the defaults. Each compose service
# (run / eval / report) sets PIPELINE_CMD; everything else comes from a
# handful of environment variables (see .env.example).
#
#   docker compose up run                                  # steps 1-4, from .env
#   docker compose up eval                                 # step 5, newest predictions
#   docker compose up report                               # summarize newest report
#   docker compose run --rm run list --limit 10            # full CLI passthrough
#   docker compose run --rm run --limit 3                  # args -> current subcommand
#
set -euo pipefail

SCRIPT="/app/run_bench.py"

# ---- Guard against the common PI_HOME mistake (bundled pi agent only) -----
# Pi resolves its config at ~/.pi/agent, and compose mounts your Pi home (~/.pi)
# at /root/.pi. So /root/.pi/agent should hold models.json / auth.json. If
# /root/.pi itself holds models.json, the agent subdir was mounted by mistake.
if [[ "${AGENT:-pi}" == "pi" \
      && -f /root/.pi/models.json && ! -f /root/.pi/agent/models.json ]]; then
    echo "WARNING: /root/.pi/models.json exists but /root/.pi/agent/models.json does not." >&2
    echo "         PI_HOME must point to your Pi home (~/.pi), the PARENT of 'agent/'." >&2
fi

# ---- Explicit CLI args are forwarded verbatim ------------------------------
if [[ $# -gt 0 ]]; then
    case "$1" in
        list|run|evaluate|report)
            exec python3 "$SCRIPT" "$@"
            ;;
        *)
            exec python3 "$SCRIPT" "${PIPELINE_CMD:?}" "$@"
            ;;
    esac
fi

# ---- Assemble the command from the environment ------------------------------
cmd="${PIPELINE_CMD:?}"
args=("$cmd")

if [[ -z "${DATASET:-}" ]]; then
    echo "ERROR: DATASET is not set — there is no default dataset. Use <benchmark>:<dataset> (see src/benchmarks/), e.g. swebench:SWE-bench/SWE-bench_Lite" >&2
    exit 2
fi
args+=(--dataset "$DATASET")

case "$cmd" in
    run)
        : "${AGENT_MODEL:?AGENT_MODEL is required for 'run' — the model your agent runs (see src/agents/), e.g. sonnet:high for the bundled pi agent}"
        : "${MODEL_NAME:?MODEL_NAME is required for 'run' — there is no default model name. It is recorded in predictions and reports.}"
        args+=(
            --agent "${AGENT:-pi}"
            --agent-model "$AGENT_MODEL"
            --model-name "$MODEL_NAME"
            --agent-timeout "${AGENT_TIMEOUT:-1800}"
        )
        [[ -n "${SKIP:-}" && "${SKIP}" != "0" ]] && args+=(--skip "$SKIP")
        [[ -n "${LIMIT:-}" && "${LIMIT}" != "0" ]] && args+=(--limit "$LIMIT")
        [[ -n "${AGENT_EXTRA_ARGS:-}" ]] && args+=(--agent-extra-args "$AGENT_EXTRA_ARGS")
        ;;
    evaluate)
        # --predictions defaults to the newest runs/<timestamp>/predictions.jsonl
        args+=(--max-workers "${EVAL_WORKERS:-4}")
        ;;
    report)
        # --run-id defaults to the newest report in logs/
        ;;
    *)
        echo "Unknown PIPELINE_CMD='${cmd}' (expected: run | evaluate | report)" >&2
        exit 2
        ;;
esac

echo ">> python3 ${SCRIPT} ${args[*]}" >&2
exec python3 "$SCRIPT" "${args[@]}"
