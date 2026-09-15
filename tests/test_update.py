import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from standup import update
from standup.config import state_dir

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def test_reports_hint_when_remote_differs() -> None:
    hint = update.check_for_update(True, NOW, fetch_latest=lambda: "b" * 40, installed="a" * 40)
    assert hint == "update available (aaaaaaa -> bbbbbbb): run standup update"


def test_no_hint_when_same_or_unknown() -> None:
    assert update.check_for_update(True, NOW, fetch_latest=lambda: "a" * 40, installed="a" * 40) is None
    assert update.check_for_update(True, NOW, fetch_latest=lambda: "b" * 40, installed=None) is None
    assert update.check_for_update(False, NOW, fetch_latest=lambda: "b" * 40, installed="a" * 40) is None


def test_network_failure_is_no_update() -> None:
    def boom() -> str | None:
        raise OSError("offline")

    assert update.check_for_update(True, NOW, fetch_latest=boom, installed="a" * 40) is None


def test_cache_is_reused_within_a_day_and_refreshed_after() -> None:
    calls: list[int] = []

    def fetch() -> str | None:
        calls.append(1)
        return "b" * 40

    update.check_for_update(True, NOW, fetch_latest=fetch, installed="a" * 40)
    update.check_for_update(True, NOW + timedelta(hours=23), fetch_latest=fetch, installed="a" * 40)
    assert len(calls) == 1
    cached = json.loads((state_dir() / "update_check.json").read_text())
    assert cached["latest"] == "b" * 40
    hint = update.check_for_update(True, NOW + timedelta(hours=25), fetch_latest=fetch, installed="a" * 40)
    assert len(calls) == 2 and hint is not None
