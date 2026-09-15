import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

_VERBOSE = False
_LOG_LOCK = threading.Lock()
_T0: float | None = None


def enable_verbose() -> None:
    global _VERBOSE, _T0
    _VERBOSE = True
    _T0 = time.monotonic()


def _elapsed() -> float:
    return (time.monotonic() - _T0) if _T0 is not None else 0.0


def dbg(msg: str) -> None:
    """Print a timestamped debug line to stderr (no-op unless --verbose)."""
    if not _VERBOSE:
        return
    with _LOG_LOCK:
        print(f"[{_elapsed():8.2f}s] {msg}", file=sys.stderr, flush=True)


@contextmanager
def timed(label: str) -> Iterator[None]:
    """Log label start and elapsed on exit; failed blocks get an x marker."""
    if not _VERBOSE:
        yield
        return
    start = time.monotonic()
    thread = threading.current_thread().name
    dbg(f"→ {label}  [start · {thread}]")
    ok = True
    try:
        yield
    except BaseException:
        ok = False
        raise
    finally:
        mark = "✓" if ok else "✗"
        dbg(f"{mark} {label}  [{time.monotonic() - start:.2f}s]")
