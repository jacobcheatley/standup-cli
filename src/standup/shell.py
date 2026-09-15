import subprocess
import time

from standup.log import dbg


class FetchError(Exception):
    pass


def _cmd_summary(cmd: list[str]) -> str:
    """Compact one-line repr of a subprocess command for debug logs."""
    # Keep it short: tool + the meaningful verb/path bits, capped.
    return " ".join(cmd)[:160]


def run(cmd: list[str], timeout: float = 30.0) -> str:
    summary = _cmd_summary(cmd)
    start = time.monotonic()
    dbg(f"  $ {summary}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        dbg(f"  ⏱ TIMEOUT after {timeout:.0f}s: {summary}")
        raise
    dur = time.monotonic() - start
    if proc.returncode != 0:
        dbg(f"  ✗ rc={proc.returncode} ({dur:.2f}s): {summary}")
        raise FetchError(f"{cmd[0]} exited {proc.returncode}: {(proc.stderr or proc.stdout).strip()[:400]}")
    dbg(f"  ✓ {dur:.2f}s ({len(proc.stdout)}B): {summary}")
    return proc.stdout
