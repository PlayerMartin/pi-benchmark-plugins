# syntax=docker/dockerfile:1
#
# bench pipeline images (wired up in docker-compose.yml; no need to build by hand).
#
#   base     -> `docker compose up run` / `report`: steps 1-4 + report summaries
#   swebench -> base + the SWE-bench harness + Docker CLI: `docker compose up eval`
#
# Build manually if you prefer:
#   docker build --target base     -t bench:latest .
#   docker build --target swebench -t bench:eval   .

# ---------------------------------------------------------------------------
# Stage 0: grab a static Docker CLI so the evaluator can talk to the host daemon
# ---------------------------------------------------------------------------
FROM docker:cli AS dockercli

# ---------------------------------------------------------------------------
# Stage 1 (base): Python pipeline + git + Node + Pi
# ---------------------------------------------------------------------------
FROM node:22-bookworm AS base

ARG DEBIAN_FRONTEND=noninteractive
# Version of the Pi coding agent to install (npm dist-tag or exact version).
ARG PI_VERSION=latest

# Python drives the pipeline; git drives the workspaces; bash drives the entrypoint.
# build-essential is needed to compile wheels for the (optional) evaluation stage.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 \
      python3-pip \
      python3-venv \
      python3-dev \
      git \
      ca-certificates \
      curl \
      bash \
      build-essential \
 && rm -rf /var/lib/apt/lists/*

# Debian bookworm marks the system env as externally managed; this is a container.
ENV PIP_BREAK_SYSTEM_PACKAGES=1

# Pi coding agent. Node >= 22.19 is guaranteed by the node:22 base image.
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}"

WORKDIR /app

# Python deps first (cached layer), then the script.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Pipeline sources live in src/ on the host; they are copied flat into /app
# so the in-container paths (and the entrypoint) stay unchanged.
COPY src/ ./
COPY docker-entrypoint.sh ./
RUN chmod +x /app/docker-entrypoint.sh

ENV PIPELINE_CMD=run \
    HF_HOME=/root/.cache/huggingface \
    PI_SKIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

# Pipeline output (predictions.jsonl, manifest.json, mirrors, workspaces, logs).
VOLUME ["/app/runs"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD []

# ---------------------------------------------------------------------------
# Stage 2 (swebench): base + evaluation harness + Docker CLI
# ---------------------------------------------------------------------------
FROM base AS swebench

# Static CLI copied from the official image; talks to the mounted host socket.
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# The official harness (FAIL_TO_PASS / PASS_TO_PASS). Needs a reachable Docker
# daemon at runtime (see docker-compose.yml: the `evaluate` service mounts the
# host socket).
RUN pip install --no-cache-dir swebench
