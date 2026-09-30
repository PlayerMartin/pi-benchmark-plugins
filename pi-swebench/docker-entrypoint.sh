#!/usr/bin/env bash
#
# Entrypoint for the Pi x SWE-bench container.
#
#   * `docker compose run --rm pi run --limit 3 ...`  -> args are passed through
#     verbatim to run_pi_swebench.py, so the full CLI stays available.
#   * plain `docker compose up pi`                     -> the command is assembled
#     from environment variables (see .env.example for the full list).
#
set -euo pipefail

SCRIPT="/app/run_pi_swebench.py"

# ---- Guard against the common PI_HOME mistake -----------------------------
# Pi resolves its config at ~/.pi/agent, and compose mounts your Pi home (~/.pi)
# at /root/.pi. So /root/.pi/agent should hold models.json / auth.json. If
# /root/.pi itself holds models.json, the agent subdir was mounted by mistake.
if [[ -f /root/.pi/models.json && ! -f /root/.pi/agent/models.json ]]; then
    echo "WARNING: /root/.pi/models.json exists but /root/.pi/agent/models.json does not." >&2
    echo "         PI_HOME must point to your Pi home (~/.pi), the PARENT of 'agent/'." >&2
fi

# ---- Explicit CLI args -----------------------------------------------------
# Trailing args are forwarded verbatim. If they do not already start with a
# subcommand, the service's PIPELINE_CMD is prepended, so all of these work:
#   docker compose run --rm pi list --limit 10
#   docker compose run --rm pi --limit 3
#   docker compose run --rm evaluate --predictions /app/runs/<timestamp>/predictions.jsonl
if [[ $# -gt 0 ]]; then
    case "$1" in
        list|run|evaluate|report)
            exec python3 "$SCRIPT" "$@"
            ;;
        *)
            exec python3 "$SCRIPT" "${PIPELINE_CMD:-run}" "$@"
            ;;
    esac
fi

cmd="${PIPELINE_CMD:-run}"
args=("$cmd")

# DATASET + instance selection are only accepted by `list` and `run`; `evaluate`
# takes --dataset but no --skip/--limit; `report` takes neither.
add_selection() {
    if [[ -z "${DATASET:-}" ]]; then
        echo "ERROR: DATASET is not set — there is no default dataset. e.g. DATASET=swebench:SWE-bench/SWE-bench_Lite" >&2
        exit 2
    fi
    args+=(--dataset "$DATASET")
    # SKIP=N skips the first N instances (default 0).
    if [[ -n "${SKIP:-}" && "${SKIP}" != "0" ]]; then
        args+=(--skip "$SKIP")
    fi
    # LIMIT=0 (the default) means "all remaining instances".
    if [[ -n "${LIMIT:-}" && "${LIMIT}" != "0" ]]; then
        args+=(--limit "$LIMIT")
    fi
    return 0
}

# ---- Per-command flags -----------------------------------------------------
case "$cmd" in
    list)
        add_selection
        ;;
    run)
        add_selection
        : "${AGENT_MODEL:?AGENT_MODEL is required for 'run', e.g. AGENT_MODEL=sonnet:high}"
        args+=(--agent "${AGENT:-pi}")
        args+=(--agent-model "$AGENT_MODEL")
        args+=(--agent-timeout "${AGENT_TIMEOUT:-1800}")
        [[ -n "${AGENT_EXTRA_ARGS:-}" ]] && args+=(--agent-extra-args "$AGENT_EXTRA_ARGS")
        ;;
    evaluate)
        if [[ -z "${DATASET:-}" ]]; then
            echo "ERROR: DATASET is not set — there is no default dataset. e.g. DATASET=swebench:SWE-bench/SWE-bench_Lite" >&2
            exit 2
        fi
        args+=(--dataset "$DATASET")
        if [[ -n "${PREDICTIONS:-}" ]]; then
            args+=(--predictions "$PREDICTIONS")
        else
            # Default: the newest timestamped run directory (runs/<YYYYmmdd-HHMMSS>/).
            latest=$(ls -1d /app/runs/*/ 2>/dev/null | sort | tail -1)
            args+=(--predictions "${latest}predictions.jsonl")
        fi
        [[ -n "${EVAL_RUN_ID:-}" ]] && args+=(--run-id "$EVAL_RUN_ID")
        [[ -n "${EVAL_WORKERS:-}" ]] && args+=(--max-workers "$EVAL_WORKERS")
        [[ -n "${EVAL_EXTRA_ARGS:-}" ]] && args+=(--eval-extra-args "$EVAL_EXTRA_ARGS")
        [[ -n "${EVAL_REPORT_DIR:-}" ]] && args+=(--report-dir "$EVAL_REPORT_DIR")
        ;;
    report)
        [[ -n "${EVAL_RUN_ID:-}" ]] && args+=(--run-id "$EVAL_RUN_ID")
        [[ -n "${EVAL_REPORT_DIR:-}" ]] && args+=(--report-dir "$EVAL_REPORT_DIR")
        ;;
    *)
        echo "Unknown PIPELINE_CMD='${cmd}' (expected: list | run | evaluate | report)" >&2
        exit 2
        ;;
esac

echo ">> python3 ${SCRIPT} ${args[*]}" >&2
exec python3 "$SCRIPT" "${args[@]}"
