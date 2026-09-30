"""Git plumbing for benchmark runs: local mirrors and per-instance workspaces."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

from benchmarks import InstanceSpec
from pi_logging import log


def run_cmd(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = run_cmd(["git", *args], cwd=cwd)
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (exit {proc.returncode}):\n"
            f"{proc.stderr.strip()[:2000]}"
        )
    return proc.stdout


def robust_rmtree(path: Path) -> None:
    """rmtree that survives Windows read-only files (git internals)."""

    def force_delete(func, target: str, _exc) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=force_delete)


def run_git_checked(args: list[str]) -> None:
    proc = run_cmd(args)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:4])} ... failed:\n{proc.stderr[:2000]}")


def ensure_mirror(repo: str, mirror_dir: Path, refresh: bool = False) -> Path:
    """Clone-once bare mirror, fetched on demand; keeps workspaces pristine."""
    mirror = mirror_dir / f"{repo.replace('/', '__')}.git"
    if not mirror.exists():
        log(f"  cloning mirror of {repo} (one-time) ...")
        run_git_checked(["git", "clone", "--mirror", f"https://github.com/{repo}.git", str(mirror)])
    elif refresh:
        log(f"  refreshing mirror of {repo} ...")
        proc = run_cmd(["git", "-C", str(mirror), "fetch", "--prune", "origin"])
        if proc.returncode != 0:
            log(f"  WARNING: mirror fetch failed for {repo}: {proc.stderr[:300]}")
    return mirror


def prepare_workspace(spec: InstanceSpec, mirror: Path, workspace: Path) -> None:
    """Fresh clone from the local mirror, checked out at base_commit."""
    if workspace.exists():
        robust_rmtree(workspace)

    run_git_checked(
        [
            "git",
            "-c",
            "core.autocrlf=false",
            "clone",
            "--quiet",
            "--no-local",  # real file copy: workspace never shares refs with mirror
            str(mirror),
            str(workspace),
        ]
    )
    git(workspace, "checkout", "--quiet", "--detach", spec.base_commit)
    git(workspace, "config", "advice.detachedHead", "false")

    head = git(workspace, "rev-parse", "HEAD").strip()
    if head != spec.base_commit:
        raise RuntimeError(f"HEAD {head} != base_commit {spec.base_commit} (drift!)")
    status = git(workspace, "status", "--porcelain")
    if status.strip():
        raise RuntimeError(f"workspace is dirty after clone:\n{status[:500]}")
