# bench: benchmark testing framework for coding agents

Runs any coding agent against any benchmark, end-to-end, and produces a
predictions file the benchmark's evaluation harness can evaluate. Any agent and
any benchmark can be used, as long as the required wrapper is implemented:

- **benchmark wrapper** (`src/benchmarks/`) — loads instance specs, formats
  predictions, and optionally evaluates them. Bundled: the
  [SWE-bench](https://www.swebench.com/) loader (`swebench:` prefix; any
  SWE-bench-format Hugging Face dataset works).
- **agent runner** (`src/agents/`) — runs the agent on a problem statement
  inside a prepared workspace. Bundled: the [Pi coding agent](https://github.com/earendil-works/pi-coding-agent)
  (`pi`).

## Architecture

The pipeline runs five steps, one instance at a time:

1. **dataset loader** — load the instance spec (instance_id, repo, base_commit,
   problem_statement) via the benchmark wrapper.
2. **repo preparer** — fresh clone of the repo at base_commit into an isolated
   workspace (via a shared local git mirror).
3. **agent runner** — run the selected agent on the problem statement inside
   the workspace, under a timeout.
4. **JSONL formatter** — capture the `git diff` vs base_commit and write the
   predictions file (schema defined by the benchmark wrapper).
5. **evaluation** — evaluate the predictions with the benchmark's own harness
   (implemented by the wrapper; the bundled SWE-bench one needs Docker and runs
   FAIL_TO_PASS / PASS_TO_PASS tests).

Steps 1 and 5 are pluggable via the `benchmarks` package
(`--dataset <prefix>:<name>`), and step 3 via the `agents` package
(`--agent <name>`).

The connection points from the design doc are enforced in code:

- **1→2 no drift**: after checkout, the script asserts `git rev-parse HEAD == base_commit`
  and that `git status --porcelain` is empty.
- **3→4 clean diff**: the patch is `git diff --cached <base_commit>` after staging with
  `git add -A` — so untracked files the agent created are included, ignored files
  (caches, noise) are excluded, binary file sections are dropped, and changes survive even
  if the agent accidentally committed. `core.autocrlf=false` is forced to keep line endings intact.
- **4→5 exact IDs**: `instance_id` is copied verbatim from the dataset row.

## Requirements

- Python 3.10+ with `pip install -r requirements.txt`
- Git on PATH
- An agent runner on PATH (the bundled one: Node.js + Pi,
  `npm install -g @earendil-works/pi-coding-agent`, with a configured
  model/provider via `pi auth login`)
- For evaluation, whatever the benchmark's harness needs (the bundled
  SWE-bench one: a Linux environment with Docker and `pip install swebench`).

## Usage

```bash
# Inspect instances (--dataset is always <benchmark>:<name>; no default)
python src/run_bench.py list --dataset swebench:SWE-bench/SWE-bench_Lite --limit 10

# Run the agent on the first 5 instances
# (--model-name is required — there is no default model name)
python src/run_bench.py run --dataset swebench:SWE-bench/SWE-bench_Lite \
    --limit 5 --agent pi --agent-model sonnet:high --model-name pi-agent

# Instances 11-15, 30 min budget each
python src/run_bench.py run --dataset swebench:SWE-bench/SWE-bench_Lite \
    --skip 10 --limit 5 --agent pi --agent-model sonnet:high --model-name pi-agent \
    --agent-timeout 1800

# Evaluate a predictions file (what this needs depends on the benchmark;
# --predictions defaults to the newest runs/<timestamp>/predictions.jsonl)
python src/run_bench.py evaluate \
    --predictions runs/<timestamp>/predictions.jsonl \
    --dataset swebench:SWE-bench/SWE-bench_Lite

# Summarize harness output (--run-id defaults to the newest report in logs/)
python src/run_bench.py report --run-id <run-id> --dataset swebench:SWE-bench/SWE-bench_Lite
```

To add another benchmark or agent, implement the wrapper interface
(`src/benchmarks/base.py` for benchmarks — load, prediction format, optional
evaluation; `src/agents/base.py` for agents) and register it.

## Docker

The container packages Python, git, Node and the Pi CLI, so the host needs only
Docker. The three pipeline stages are three compose services:

| Service   | Image          | Runs                          | Needs                  |
|-----------|----------------|-------------------------------|------------------------|
| `run`     | `bench:latest` | steps 1-4                     | Pi credentials         |
| `eval`    | `bench:eval`   | step 5 (harness)              | Linux + Docker daemon  |
| `report`  | `bench:latest` | step 5 summary                | —                      |
### Quick start

```bash
cp .env.example .env        # then set DATASET, AGENT_MODEL, MODEL_NAME and a provider key (or PI_HOME)

docker compose up run        # steps 1-4: agent -> runs/<timestamp>/predictions.jsonl
docker compose up eval      # step 5: evaluate the newest predictions
docker compose up report    # summarize the newest report in logs/
```

`eval` auto-detects the newest `runs/<timestamp>/predictions.jsonl` and
`report` the newest report in `logs/`, so the three commands chain without
any extra configuration.

`eval` and `report` sit behind the `eval` profile; naming the service on the
docker compose command line activates its profile automatically (Compose >=
2.5), while a bare `docker compose up` still only runs `run`.

### Everything else: CLI passthrough

`docker compose run` forwards trailing arguments verbatim to the Python CLI,
so the full command-line interface is always available:

```bash
docker compose run --rm run list --limit 10
docker compose run --rm run --skip 10 --limit 5 --agent-timeout 1800
```

Values not in `.env` still have defaults and can be overridden ad hoc:
`AGENT_TIMEOUT=300 LIMIT=3 docker compose up run`.

### Configuration

`.env` holds only what has no default (see `.env.example`):

| Variable       | Default        | Meaning                                            |
|---------------|----------------|----------------------------------------------------|
| `DATASET`     | — (required)   | `benchmark:dataset`, e.g. `swebench:SWE-bench/SWE-bench_Lite` |
| `AGENT_MODEL` | — (required for `run`) | Model for the agent, e.g. `sonnet:high`   |
| `MODEL_NAME`  | — (required for `run`) | Name recorded in predictions and reports   |
| `LIMIT`       | `0`            | Take the next N instances; `0` = all                |

Plus credentials: one provider API key (`ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, ...) or `PI_HOME` pointing at your host `~/.pi`.

Ad-hoc-only knobs (defaults live in `docker-compose.yml`): `AGENT` (`pi`),
`AGENT_TIMEOUT` (`1800`), `AGENT_EXTRA_ARGS` (empty), `SKIP` (`0`),
`EVAL_WORKERS` (`4`).

### Volumes

- `./runs` — pipeline output: one timestamped directory per run
  (`run.json`, `predictions.jsonl`, `manifest.json`, `workspaces/`, `logs/`)
  plus the shared `mirrors/`. Persists on the host.
- `${PI_HOME:-./.pi-home}` — your host Pi home `~/.pi`, mounted at `/root/.pi`.
  Pi reads its config from `~/.pi/agent`, so point this at the home directory
  (not the `agent` subdir) to reuse existing logins and custom providers; or
  leave it and run `docker compose run --rm run auth login` once.
- `hf-cache` — named volume caching downloaded HF datasets.
- `./logs` — harness reports (written by the evaluation harness).

### Notes

- The `eval` service mounts `/var/run/docker.sock` because the bundled
  SWE-bench harness builds and runs per-instance images.
- On Linux without Docker Desktop, the socket mount requires the daemon to be
  reachable from the container; the container connects as root.
- Build the images individually if you prefer:
  `docker build --target base -t bench:latest .` and
  `docker build --target swebench -t bench:eval .`

## Output layout (always `runs/`)

Every `run` invocation writes all of its artifacts into one timestamped
directory, so runs never overwrite each other:

```
runs/
├── mirrors/                      # bare repo mirrors (cloned once, reused across runs)
└── <YYYYmmdd-HHMMSS>/            # one directory per run
    ├── run.json                   # run metadata: model, dataset, instance list
    ├── predictions.jsonl          # the sole interface into the evaluation harness
    ├── manifest.json              # per-instance status, durations, token totals
    ├── workspaces/                # per-instance working clones (deleted after run)
    └── logs/
        └── <instance_id>/
            ├── meta.json          # model, task, prompt, status, usage, artifacts
            ├── events.jsonl       # full agent event stream (all LLM messages,
            │                     #   tool calls, tool results; written by the
            │                     #   bundled pi runner)
            ├── transcript.md      # human-readable chat history
            ├── patch.diff         # the captured diff
            └── pi.stderr.log      # agent diagnostics
```

`events.jsonl` is the machine-readable record of everything the agent did:
every user/assistant message (the exact instructions the LLM received and
produced), every tool call with arguments and full results, and per-response
token usage. `transcript.md` is the same data rendered for reading;
large tool results are truncated there but complete in `events.jsonl`.

Predictions JSONL schema — defined by the benchmark wrapper; shown for the
bundled SWE-bench one (one line per instance, `model_name_or_path` is whatever
`--model-name` was given):

```json
{"instance_id": "django__django-11099", "model_name_or_path": "my-agent", "model_patch": "diff --git a/..."}
```

## Design notes

- **Fresh workspace per run**: every instance gets a new clone from a local bare mirror
  (clone-once, fetch-on-demand), so the agent never sees a dirty tree or a later commit
  that spoils the answer.
- **Agents are pluggable**: the pipeline talks to any `AgentRunner` (see
  `src/agents/base.py`). The built-in `pi` runner runs Pi headless (`--mode json
  --no-session`, problem statement piped via stdin), records the full event
  stream, and condenses it into a transcript; the agent's internal loop is a
  black box — only the diff and the reported status/usage cross the boundary.
  Implement `run()` and call `register_agent()` to add another.
- **Benchmarks are pluggable**: a `Benchmark` (see `src/benchmarks/base.py`)
  provides instances, the prediction record format, and (optionally) evaluation
  and report summaries. Implement `_load()` and `format_prediction()`, call
  `register_benchmark()`, and select it with `--dataset <prefix>:<name>`.
- **No implicit identity**: there is no default benchmark, no default dataset,
  and no default model name — all of them must be supplied explicitly.
- **Timeouts**: if the agent exceeds `--agent-timeout`, the process is killed but any
  partial edits are still captured as a patch.
- **Empty patches / errors** are recorded in `manifest.json` (statuses: `completed`,
  `no_patch`, `agent_timeout`, `agent_error`, `prep_error`) and skipped from the JSONL.
