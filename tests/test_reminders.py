import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from standup.sources import reminders
from standup.sources.base import SourceContext
from standup.sources.reminders import (
    load_reminders,
    reminder_add,
    reminder_done,
    reminder_done_rows,
    reminder_open_rows,
    reminders_dir,
)


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_BRIDGE_SESSION_ID", raising=False)


def _read(reminder_id: str) -> dict[str, Any]:
    return json.loads((reminders_dir() / f"{reminder_id}.json").read_text())


def test_add_writes_file_with_tz_aware_due() -> None:
    rid = reminder_add("Check back on this", "2026-09-15T10:00", None, None)
    saved = _read(rid)
    assert saved["title"] == "Check back on this"
    assert rid == "1"
    due = datetime.fromisoformat(saved["due"])
    assert due.tzinfo is not None
    assert due.astimezone().hour == 10
    assert saved["done"] is None
    assert saved["url"] is None and saved["note"] is None


def test_add_rejects_bad_due() -> None:
    with pytest.raises(ValueError):
        reminder_add("x", "tomorrow 10am", None, None)
    assert not reminders_dir().exists() or not list(reminders_dir().glob("*.json"))


def test_add_inside_claude_session_fills_resume_and_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    monkeypatch.setenv("CLAUDE_CODE_BRIDGE_SESSION_ID", "session_XYZ")
    saved = _read(reminder_add("resume me", None, None, None))
    assert saved["note"] == f"cd {os.getcwd()} && claude --resume abc-123"
    assert saved["url"] == "https://claude.ai/code/session_XYZ"


def test_add_explicit_url_and_note_win_over_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    saved = _read(reminder_add("look at page", None, "https://example.com/x", "custom"))
    assert saved["url"] == "https://example.com/x"
    assert saved["note"] == "custom"


def test_add_no_session_leaves_url_and_note_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "abc-123")
    saved = _read(reminder_add("plain", None, None, None, use_session=False))
    assert saved["url"] is None and saved["note"] is None


def test_ids_are_sequential_and_survive_done() -> None:
    assert reminder_add("one", None, None, None) == "1"
    assert reminder_add("two", None, None, None) == "2"
    reminder_done("2")
    assert reminder_add("three", None, None, None) == "3"
    assert [it["id"] for it in load_reminders()] == ["1", "2", "3"]


def test_done_marks_by_id() -> None:
    rid = reminder_add("first", None, None, None)
    assert reminder_done(rid) == rid
    assert datetime.fromisoformat(_read(rid)["done"]).tzinfo is not None


def test_done_rejects_unknown_id() -> None:
    reminder_add("only", None, None, None)
    with pytest.raises(ValueError):
        reminder_done("2")


def test_open_rows_sorted_by_due_with_labels() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    items = [
        {"id": "c", "title": "no due", "due": None},
        {"id": "b", "title": "soon", "due": (now + timedelta(hours=3)).isoformat()},
        {"id": "a", "title": "late", "due": (now - timedelta(hours=2)).isoformat()},
        {"id": "d", "title": "done", "due": None, "done": now.isoformat()},
        {"id": "e", "title": "next week", "due": (now + timedelta(days=5)).isoformat()},
    ]
    rows = reminder_open_rows(items, now)
    assert [r.key for r in rows] == ["a", "b", "e", "c"]
    assert rows[0].meta == "overdue 2h" and rows[0].meta_style == "bold red"
    assert rows[1].meta == "in 3h" and rows[1].meta_style == "yellow"
    assert rows[2].meta == "in 5d" and rows[2].meta_style == "white"
    assert rows[3].meta == "no due"


def test_row_keeps_note_separate_from_title() -> None:
    now = datetime.now(UTC)
    item = {"id": "x", "title": "t", "note": "claude --resume 1", "url": "https://u"}
    rows = reminder_open_rows([item], now)
    assert rows[0].title == "t"
    assert rows[0].note == "claude --resume 1"
    assert rows[0].url == "https://u"


def test_done_rows_only_within_cutoff() -> None:
    now = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
    cutoff = now - timedelta(days=3)
    items = [
        {"id": "recent", "title": "r", "done": (now - timedelta(days=1)).isoformat()},
        {"id": "old", "title": "o", "done": (now - timedelta(days=10)).isoformat()},
        {"id": "open", "title": "p", "done": None},
    ]
    rows = reminder_done_rows(items, cutoff, now)
    assert [r.key for r in rows] == ["recent"]
    assert rows[0].meta == "done"


def test_source_sections_split_open_and_done() -> None:
    now = datetime.now(UTC)
    raw = {"raw": [
        {"id": "r1", "title": "open one", "due": None},
        {"id": "r2", "title": "closed one", "done": now.isoformat()},
    ], "error": None}
    ctx: SourceContext[Any] = SourceContext(
        config={"enabled": True}, now=now, cutoff=now - timedelta(days=3), hide_handled=True
    )
    sections = reminders.source.sections(raw, ctx)
    assert [s.title for s in sections] == ["Reminders", "Reminders done"]
    assert [r.key for r in sections[0].rows] == ["r1"]
    assert sections[0].age_filter is False
    assert [r.key for r in sections[1].rows] == ["r2"] and sections[1].done is True


def test_cli_add_list_done() -> None:
    runner = CliRunner()
    app = reminders.source.commands()
    assert app is not None

    def run(*args: str) -> str:
        result = runner.invoke(app, list(args))
        assert result.exit_code == 0, result.output
        return result.output

    rid = run("add", "ping ops", "--due", "2030-01-01T09:00", "--url", "https://example.com").strip()
    assert (reminders_dir() / f"{rid}.json").exists()
    assert "ping ops" in run("list")
    assert run("done", rid).strip() == f"done: {rid}"
    assert "ping ops" not in run("list")
    assert "ping ops" in run("list", "--all")


def test_cli_done_unknown_id_exits_2() -> None:
    app = reminders.source.commands()
    assert app is not None
    result = CliRunner().invoke(app, ["done", "99"])
    assert result.exit_code == 2
