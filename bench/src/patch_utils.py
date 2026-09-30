"""Patch capture and cleaning: git diff -> harness-compatible patch text."""

from __future__ import annotations

from pathlib import Path

from benchmarks import InstanceSpec
from repo_prep import git


def clean_diff(raw_diff: str) -> str:
    """Drop binary-file sections so the harness can `git apply` the patch."""
    if not raw_diff.strip():
        return ""
    sections: list[list[str]] = []
    current: list[str] = []
    for line in raw_diff.splitlines(keepends=False):
        if line.startswith("diff --git "):
            if current:
                sections.append(current)
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append(current)
    kept = [sec for sec in sections if not in_binary_of(sec)]
    return "\n".join("\n".join(sec) for sec in kept).strip() + "\n" if kept else ""


def in_binary_of(section: list[str]) -> bool:
    return any(
        "GIT binary patch" in line or line.startswith("Binary files ")
        for line in section
    )


def capture_patch(spec: InstanceSpec, workspace: Path) -> str:
    """Diff working tree (incl. untracked, incl. accidental commits) vs base."""
    git(workspace, "add", "-A")  # stage everything so untracked files appear
    raw = git(workspace, "diff", "--cached", spec.base_commit)
    git(workspace, "reset", "--quiet", check=False)
    return clean_diff(raw)
