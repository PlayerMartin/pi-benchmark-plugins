# Pi × SWE-bench: automatic testing framework

Runs the [Pi coding agent](https://github.com/earendil-works/pi-coding-agent) against
[SWE-bench_Lite](https://huggingface.co/datasets/SWE-bench/SWE-bench_Lite) (or any
SWE-bench-format dataset) end-to-end and produces a predictions file the official
SWE-bench harness can evaluate.

## Architecture

```
┌──────────────┐   task spec   ┌───────────────┐  workspace   ┌──────────────┐  git diff
│ 1. dataset   │ ────────────▶ │ 2. repo       │ ──────────▶ │ 3. Pi agent  │ ────────┐
│    loader    │  instance_id, │    preparer   │  fresh clone │    runner    │         │
│ (HF datasets)│  repo, commit │ (git mirror)  │ @base_commit│ (pi --print) │         ▼
└──────────────┘  problem_stmt └───────────────┘             └──────────────┘ ┌───────────┐
                                                                                 │ 4. JSONL  │
                                                                 predictions ────▶│ formatter │
                                                                                 └─────┬─────┘
                                                                                       ▼
                                                                        ┌──────────────────────┐
                                                                        │ 5. SWE-bench harness │
                                                                        │ (Docker, FAIL_TO_PASS │
                                                                        │  / PASS_TO_PASS)     │
                                                                        └──────────────────────┘
```

The connection points from the design doc are enforced in code:

- **1→2 no drift**: after checkout, the script asserts `git rev-parse HEAD == base_commit`
  and that `git status --porcelain` is empty.
- **3→4 clean diff**: the patch is `git diff --cached <base_commit>` after staging with
  `git add -A` — so untracked files Pi created are included, ignored files (caches, noise)
  are excluded, binary file sections are dropped, and changes survive even if Pi
  accidentally committed. `core.autocrlf=false` is forced to keep line endings intact.
- **4→5 exact IDs**: `instance_id` is copied verbatim from the dataset row.

## Requirements

- Python 3.10+ with `pip install -r requirements.txt`
- Git on PATH
- Node.js + Pi on PATH (`npm install -g @earendil-works/pi-coding-agent`),
  with a configured model/provider (`pi auth login`)
- For step 5 only: a Linux environment with Docker and `pip install swebench`.
  On Windows, run evaluation under WSL (see below).

## Usage

```bash
# Inspect instances (--dataset is always <benchmark>:<name>; no default)
python src/run_pi_swebench.py list --dataset swebench:SWE-bench/SWE-bench_Lite --limit 10

# Run Pi on the first 5 instances (steps 1-4)
python src/run_pi_swebench.py run --dataset swebench:SWE-bench/SWE-bench_Lite \
    --limit 5 --pi-model sonnet:high

# Instances 11-15, 30 min budget each
python src/run_pi_swebench.py run --dataset swebench:SWE-bench/SWE-bench_Lite \
    --skip 10 --limit 5 --pi-model sonnet:high --pi-timeout 1800

# Evaluate a predictions file (Linux/Docker or WSL)
python src/run_pi_swebench.py evaluate \
    --predictions runs/<timestamp>/predictions.jsonl \
    --dataset swebench:SWE-bench/SWE-bench_Lite
python src/run_pi_swebench.py evaluate \
    --predictions runs/<timestamp>/predictions.jsonl \
    --dataset swebench:SWE-bench/SWE-bench_Lite \
    --eval-python "wsl python"          # Windows: harness inside WSL

# Summarize harness output
python src/run_pi_swebench.py report --run-id <run-id>
```

## Docker

The container packages Python, git, Node and the Pi CLI, so the host needs only
Docker. It mirrors the two halves of the pipeline:

| Service    | Image              | Runs                          | Needs                  |
|------------|--------------------|-------------------------------|------------------------|
| `pi`       | `pi-swebench:latest` | steps 1-4 (`list`, `run`)   | Pi credentials         |
| `evaluate` | `pi-swebench:eval`   | step 5 (`evaluate`, `report`) | Linux + Docker daemon |

### Quick start

```bash
cp .env.example .env          # then set PI_MODEL and a provider key / PI_HOME

# Inspect instances
docker compose run --rm pi list --limit 10

# Run steps 1-4 using the .env defaults
docker compose up pi

# Run with explicit args (overrides .env)
docker compose run --rm pi run --limit 5 --pi-model sonnet:high --pi-timeout 1800

# Step 5: evaluate the predictions (uses the mounted Docker daemon)
docker compose --profile eval run --rm evaluate
docker compose --profile eval run --rm evaluate report
```

`docker compose run` forwards trailing arguments verbatim to the Python CLI, so
the full command-line interface is always available. Running the service with no
arguments (`docker compose up pi`) assembles the command from environment
variables instead.

### Configuration

All knobs live in `.env` (see `.env.example` for the annotated list). The most
important ones:

| Variable          | Default                          | Meaning                                            |
|-------------------|----------------------------------|----------------------------------------------------|
| `PIPELINE_CMD`    | `run`                            | `list` \| `run` \| `evaluate` \| `report`            |
| `PI_MODEL`        | — (required for `run`)           | Value passed to `pi --model`, e.g. `sonnet:high`   |
| `LIMIT`           | `0`                              | Take the next N instances after `SKIP`; `0` = all |
| `SKIP`            | `0`                              | Skip the first N instances                         |
| `DATASET`         | — (required)                     | `benchmark:dataset`, e.g. `swebench:SWE-bench/SWE-bench_Lite` |
| `PI_TIMEOUT`      | `1800`                           | Seconds per instance                               |
| `PI_EXTRA_ARGS`   | —                                | Extra args appended verbatim to the `pi` command   |
| `PI_HOME`         | `./.pi-home`                     | Host Pi home `~/.pi` (contains `agent/`)            |
| `EVAL_RUN_ID`     | `pi-docker`                      | Run id / report prefix for the harness             |
| `EVAL_WORKERS`    | `4`                              | Parallel harness workers                           |
| `EVAL_EXTRA_ARGS` | —                                | Extra args forwarded to the harness                |
| `EVAL_REPORT_DIR` | `logs`                           | Harness `--report_dir` (keep it mounted)           |

Credentials (`HF_TOKEN`, `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, ...) are read
from the same `.env` and passed into the container.

### Volumes

- `./runs` — pipeline output: one timestamped directory per run
  (`run.json`, `predictions.jsonl`, `manifest.json`, `workspaces/`, `logs/`)
  plus the shared `mirrors/`. Persists on the host.
- `${PI_HOME}` — your host Pi home `~/.pi` (`C:/Users/you/.pi` on Windows),
  mounted at `/root/.pi`. Pi reads its config from `~/.pi/agent`, so point this
  at the home directory (not the `agent` subdir) to reuse existing logins and
  custom providers; or leave it and run `docker compose run --rm pi auth login`
  once. `PI_AGENT_DIR` remains a legacy alias.
- `hf-cache` — named volume caching downloaded HF datasets.
- `./logs` — harness reports: aggregate `logs/<model>.<run_id>.json` plus
  per-instance `logs/run_evaluation/<run_id>/<model>/<instance>/report.json`.

### Notes

- The `evaluate` service mounts `/var/run/docker.sock` because the SWE-bench
  harness builds and runs per-instance images. It is gated behind the `eval`
  profile so it never starts with a plain `docker compose up`.
- On Linux without Docker Desktop, the socket mount requires the daemon to be
  reachable from the container; the container connects as root.
- Build the images individually if you prefer:
  `docker build --target base -t pi-swebench:latest .` and
  `docker build --target swebench -t pi-swebench:eval .`

## Output layout (always `runs/`)

Every `run` invocation writes all of its artifacts into one timestamped
directory, so runs never overwrite each other:

```
runs/
├── mirrors/                      # bare repo mirrors (cloned once, reused across runs)
└── <YYYYmmdd-HHMMSS>/            # one directory per run
    ├── run.json                   # run metadata: model, dataset, instance list
    ├── predictions.jsonl          # the sole interface into the SWE-bench harness
    ├── manifest.json              # per-instance status, durations, cost/token totals
    ├── workspaces/                # per-instance working clones (deleted after run)
    └── logs/
        └── <instance_id>/
            ├── meta.json          # model, task, prompt, status, usage, artifacts
            ├── events.jsonl       # full Pi JSON event stream (all LLM messages,
            │                     #   tool calls, tool results; streaming deltas dropped)
            ├── transcript.md      # human-readable chat history
            ├── patch.diff         # the captured diff
            └── pi.stderr.log      # Pi diagnostics
```

`events.jsonl` is the machine-readable record of everything the agent did:
every user/assistant message (the exact instructions the LLM received and
produced), every tool call with arguments and full results, and per-response
token usage and cost. `transcript.md` is the same data rendered for reading;
large tool results are truncated there but complete in `events.jsonl`.

Predictions JSONL schema (one line per instance):

```json
{"instance_id": "django__django-11099", "model_name_or_path": "pi-agent", "model_patch": "diff --git a/..."}
```

## Design notes

- **Fresh workspace per run**: every instance gets a new clone from a local bare mirror
  (clone-once, fetch-on-demand), so Pi never sees a dirty tree or a later commit that
  spoils the answer.
- **Pi runs headless**: `pi --print --no-session` with the problem statement piped via
  stdin (avoids Windows command-line length limits). Pi's internal loop is a black box;
  only the diff crosses the boundary.
- **Timeouts**: if Pi exceeds `--pi-timeout`, the process is killed but any partial
  edits are still captured as a patch.
- **Empty patches / errors** are recorded in `manifest.json` (statuses: `completed`,
  `no_patch`, `pi_timeout`, `pi_error`, `prep_error`) and skipped from the JSONL.
- **Dataset schema**: evaluation requires a SWE-bench >= 5 dataset (the
  `SWE-bench/...` re-releases), whose rows carry `image`, `eval_type`,
  `eval_script` and `log_parser`. The legacy `princeton-nlp/...` names are
  auto-mapped to their `SWE-bench/...` counterparts.
- **Harness**: evaluation shells out to
  `python -m swebench.harness.run_evaluation --dataset_name SWE-bench/SWE-bench_Lite
  --split test --predictions_path ... --run_id ...`. It builds per-instance Docker images,
  applies the patch on top of `base_commit`, and runs FAIL_TO_PASS / PASS_TO_PASS tests.
  Full harness output (build logs, test logs, per-instance `report.json`) lands
  under `logs/run_evaluation/<run_id>/<model>/<instance>/`, and the aggregate
  report at `logs/<model>.<run_id>.json` (both inside the mounted `./logs`).

## Troubleshooting

- **`pi` not found** — make sure the Pi CLI is on PATH
  (`npm install -g @earendil-works/pi-coding-agent`).
- **HF download blocked** — set `HF_ENDPOINT` / `HF_HOME` as needed for your network.
- **Clone failures** — SWE-bench repos are real GitHub repos; some are large. Mirrors
  are cached under `runs/mirrors/`; delete one to force a re-clone. Submodules are not
  initialized (none of the Lite instances need them for the patch itself).
- **Harness silently skips instances** — always caused by `instance_id` mismatch; this
  script copies it verbatim from the dataset, so check you evaluated the same dataset/split.
