# syntax=docker/dockerfile:1
#
# Pi x SWE-bench runner.
#
#   base     -> steps 1-4: dataset loader, repo preparer, Pi agent, predictions
#   swebench -> base + the SWE-bench evaluation harness + Docker CLI (step 5)
#
# Build the runner:   docker build --target base     -t pi-swebench:latest .
# Build the evaluator: docker build --target swebench -t pi-swebench:eval   .
#
# Both are wired up in docker-compose.yml; you normally do not build by hand.

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
# build-essential is needed to compile wheels for the (optional) swebench stage.
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
