from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import httpx
import typer

from standup.config import state_dir

REPO = "jacobcheatley/standup-cli"
INSTALL_URL = f"git+https://github.com/{REPO}"
CACHE_TTL = timedelta(hours=24)


def installed_commit() -> str | None:
    try:
        text = distribution("standup-cli").read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    if not text:
        return None
    commit = (json.loads(text).get("vcs_info") or {}).get("commit_id")
    return str(commit) if commit else None


def fetch_latest_commit() -> str | None:
    response = httpx.get(
        f"https://api.github.com/repos/{REPO}/commits/HEAD",
        headers={"Accept": "application/vnd.github.sha"},
        timeout=5.0,
    )
    response.raise_for_status()
    return response.text.strip() or None


def _cache_path() -> Path:
    return state_dir() / "update_check.json"


def _cached_latest(now: datetime) -> str | None:
    try:
        cached = json.loads(_cache_path().read_text())
        checked_at = datetime.fromisoformat(cached["checked_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if now - checked_at > CACHE_TTL:
        return None
    latest = cached.get("latest")
    return str(latest) if latest else None


def check_for_update(
    enabled: bool,
    now: datetime,
    fetch_latest: Callable[[], str | None] = fetch_latest_commit,
    installed: str | None = None,
) -> str | None:
    installed = installed or installed_commit()
    if not enabled or installed is None:
        return None
    latest = _cached_latest(now)
    if latest is None:
        try:
            latest = fetch_latest()
        except Exception:
            return None
        if latest is None:
            return None
        _cache_path().parent.mkdir(parents=True, exist_ok=True)
        _cache_path().write_text(json.dumps({"checked_at": now.isoformat(), "latest": latest}))
    if latest == installed:
        return None
    return f"update available ({installed[:7]} -> {latest[:7]}): run standup update"


def run_upgrade() -> int:
    if shutil.which("uv") is None:
        typer.echo(f"uv not found on PATH; install uv, then: uv tool install {INSTALL_URL}", err=True)
        return 1
    return subprocess.run(["uv", "tool", "upgrade", "standup-cli"]).returncode
