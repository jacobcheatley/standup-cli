from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from standup.sources import linear
from standup.sources.base import SourceContext

NOW = datetime(2026, 9, 15, tzinfo=timezone.utc)  # noqa: UP017


def _issue(ident: str, status: str, status_type: str, **extra: Any) -> dict[str, Any]:
    return {"id": ident, "title": ident, "status": status, "statusType": status_type,
            "updatedAt": "2026-09-14T00:00:00Z", "url": f"https://linear.app/x/{ident}", **extra}


def test_open_rows_skip_completed_and_duplicates() -> None:
    issues = [
        _issue("A-1", "In Progress", "started"),
        _issue("A-2", "Done", "completed"),
        _issue("A-3", "Duplicate", "canceled"),
        _issue("A-4", "Duplicate", "unstarted"),
    ]
    assert [r.key for r in linear.linear_open_rows(issues, hide_handled=True)] == ["A-1"]
    assert [r.key for r in linear.linear_open_rows(issues, hide_handled=False)] == ["A-1", "A-4"]


def test_done_rows_within_cutoff_only() -> None:
    cutoff = NOW - timedelta(days=3)
    issues = [
        _issue("A-1", "Done", "completed", completedAt=(NOW - timedelta(days=1)).isoformat()),
        _issue("A-2", "Done", "completed", completedAt=(NOW - timedelta(days=9)).isoformat()),
    ]
    assert [r.key for r in linear.linear_done_rows(issues, cutoff)] == ["A-1"]


def test_sections_and_error() -> None:
    cfg: linear.LinearConfig = {"enabled": True, "comments": False}
    ctx = SourceContext(config=cfg, now=NOW, cutoff=NOW - timedelta(days=3), hide_handled=True)
    sections = linear.source.sections({"raw": [_issue("A-1", "Todo", "unstarted")], "error": None}, ctx)
    assert [s.title for s in sections] == ["Linear", "Linear done"]
    errored = linear.source.sections({"raw": [], "error": "boom"}, ctx)
    assert errored[0].error == "boom" and errored[0].rows == []


def test_reset_auth_deletes_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    linear.token_path().parent.mkdir(parents=True)
    linear.token_path().write_text("{}")
    app = linear.source.commands()
    assert app is not None
    result = CliRunner().invoke(app, ["reset-auth"])
    assert result.exit_code == 0 and not linear.token_path().exists()
    assert CliRunner().invoke(app, ["reset-auth"]).exit_code == 0
