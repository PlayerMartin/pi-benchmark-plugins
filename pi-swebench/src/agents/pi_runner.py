"""Runs the pi CLI headless (``--mode json``) inside a prepared workspace."""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from benchmarks import InstanceSpec

from agents.base import AgentOutcome, AgentRunner, register_agent


def resolve_pi_cmd() -> str:
    pi = shutil.which("pi")
    if not pi:
        raise FileNotFoundError(
            "Could not find the 'pi' CLI on PATH. Install it with "
            "`npm install -g @earendil-works/pi-coding-agent`."
        )
    return pi


class PiAgentRunner(AgentRunner):
    """Writes events.jsonl (condensed), transcript.md and pi.stderr.log to log_dir."""

    def __init__(self, name: str, model: str, extra_args=None) -> None:
        super().__init__(name, model, extra_args)
        self.cmd = resolve_pi_cmd()

    def run(
        self, spec: InstanceSpec, workspace: Path, log_dir: Path, timeout: int
    ) -> AgentOutcome:
        prompt = self.build_prompt(spec)
        args = [
            self.cmd,
            "--mode",
            "json",
            "--no-session",
            "--model",
            self.model,
            *self.extra_args,
            "Fix the GitHub issue described below. Work directly in this repository.",
        ]
        log_dir.mkdir(parents=True, exist_ok=True)
        events_file = log_dir / "events.jsonl"
        stderr_file = log_dir / "pi.stderr.log"
        start = time.time()
        with (
            open(events_file, "w", encoding="utf-8") as events_fh,
            open(stderr_file, "w", encoding="utf-8") as stderr_fh,
        ):
            try:
                proc = subprocess.run(
                    args,
                    cwd=str(workspace),
                    input=prompt,
                    stdout=events_fh,
                    stderr=stderr_fh,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout,
                )
                status = (
                    "ok"
                    if proc.returncode == 0
                    else f"agent_error(exit={proc.returncode})"
                )
                detail = ""
            except subprocess.TimeoutExpired:
                status = "agent_timeout"
                detail = f"Pi did not finish within {timeout}s; partial work kept."
            except Exception as e:  # noqa: BLE001
                status = "agent_error"
                detail = f"{type(e).__name__}: {e}"

            duration = time.time() - start
            if not detail:
                tail = stderr_file.read_text(encoding="utf-8", errors="replace").strip()
                detail = tail[-4000:]

        usage = self._condense_events(events_file)
        self._render_transcript(
            events_file, log_dir / "transcript.md", spec, status, duration, usage
        )
        return AgentOutcome(
            status=status, detail=detail, duration_s=duration, usage=usage
        )

    def _condense_events(self, events_file: Path) -> dict:
        """Drop streaming message_update deltas (message_end carries the full
        messages), aggregate token usage, rewrite the file in place.
        """
        kept: list[str] = []
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "responses": 0,
        }
        pending_usage: dict | None = None
        text = events_file.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # truncated tail after a timeout/kill
            if rec.get("type") == "message_update":
                if rec.get("usage"):
                    pending_usage = rec["usage"]
                continue
            if rec.get("type") == "message_end":
                msg = rec.get("message") or {}
                if msg.get("role") == "assistant":
                    u = msg.get("usage") or pending_usage
                    if u:
                        usage["input_tokens"] += u.get("input", 0)
                        usage["output_tokens"] += u.get("output", 0)
                        usage["cache_read_tokens"] += u.get("cacheRead", 0)
                        usage["cache_write_tokens"] += u.get("cacheWrite", 0)
                        usage["responses"] += 1
                    pending_usage = None
            kept.append(json.dumps(rec))
        events_file.write_text(
            "\n".join(kept) + ("\n" if kept else ""), encoding="utf-8"
        )
        return usage

    def _render_transcript(
        self,
        events_file: Path,
        out_path: Path,
        spec: InstanceSpec,
        status: str,
        duration_s: float,
        usage: dict,
    ) -> None:
        """Render a human-readable chat history (markdown) from the event stream."""

        def render_content(content) -> str:
            if isinstance(content, str):
                return content
            parts: list[str] = []
            for block in content or []:
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "thinking":
                    thinking = "\n> ".join(block.get("thinking", "").splitlines())
                    parts.append(f"> [thinking]\n> {thinking}")
                elif btype == "toolCall":
                    call = json.dumps(block.get("arguments", {}), indent=2)
                    parts.append(
                        f"**tool call `{block.get('name')}`**\n```json\n{call}\n```"
                    )
            return "\n\n".join(p for p in parts if p)

        def render_tool_result(result) -> str:
            chunks = [
                block.get("text", "")
                for block in (result or {}).get("content") or []
                if block.get("type") == "text"
            ]
            text = "\n".join(chunks)
            if len(text) > 4000:
                text = (
                    text[:2000]
                    + "\n... [truncated; full result in events.jsonl] ...\n"
                    + text[-1000:]
                )
            return text

        lines = [
            f"# Transcript: {spec.instance_id}",
            "",
            f"- **repo**: {spec.repo} @ `{spec.base_commit[:12]}`",
            f"- **model**: `{self.model}`",
            f"- **status**: {status}",
            f"- **duration**: {duration_s:.0f}s",
            f"- **usage**: {usage['input_tokens']} in / {usage['output_tokens']} out tokens, "
            f"{usage['responses']} responses",
            "",
            "---",
            "",
        ]
        text = events_file.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            if rtype == "message_end":
                msg = rec.get("message") or {}
                lines.append(f"## {msg.get('role', '?').title()}\n")
                lines.append(render_content(msg.get("content")))
                lines.append("")
            elif rtype == "tool_execution_end":
                name = rec.get("toolName", "?")
                flag = "error" if rec.get("isError") else "ok"
                lines.append(f"### tool result `{name}` ({flag})\n")
                lines.append(f"```\n{render_tool_result(rec.get('result'))}\n```")
                lines.append("")
        out_path.write_text("\n".join(lines), encoding="utf-8")


# Built-in: addressable as "pi".
register_agent("pi", PiAgentRunner)
