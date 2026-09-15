from datetime import UTC, datetime, timedelta

import pytest
from rich.console import Console

from standup.model import Row, Section
from standup.render import RenderOptions, render_markdown, render_rich

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def _row(key: str, note: str | None = None) -> Row:
    return Row(key=key, title=f"title {key}", meta="author", meta_style="magenta",
               updated=NOW - timedelta(hours=2), url=f"https://x/{key}", note=note)


def _sections() -> list[Section]:
    return [
        Section(title="Reminders", color="yellow", meta_header="Due",
                rows=[_row("1", note="cd /a b && claude --resume x")],
                age_filter=False, summary_label="reminders"),
        Section(title="GitHub", color="magenta", meta_header="Role", rows=[_row("r#2")],
                summary_label="GitHub PRs", hidden=3),
        Section(title="Linear", color="cyan", meta_header="State", rows=[], error="boom",
                summary_label="Linear issues"),
        Section(title="GitHub merged", color="magenta", meta_header="Role", rows=[_row("r#9")],
                done=True, summary_label="GitHub"),
    ]


def test_markdown_has_sections_totals_and_hidden_hint() -> None:
    md = render_markdown(_sections(), RenderOptions(now=NOW, max_age_days=30, recent_done_days=3))
    assert "## Open" in md and "### Reminders (1)" in md and "### GitHub (1 · 3 hidden)" in md
    assert "### Linear — ERROR: boom" in md
    assert "### GitHub merged (1)" in md
    assert "`cd /a b && claude --resume x`" in md
    assert "**Total open:** 1 reminders · 1 GitHub PRs · 0 Linear issues (3 hidden older than 30d" in md
    assert "**Total done (last 3d):** 1 GitHub" in md


def test_markdown_omits_done_block_when_disabled() -> None:
    md = render_markdown(_sections(), RenderOptions(now=NOW, max_age_days=0, recent_done_days=0))
    assert "GitHub merged" not in md and "Total done" not in md and "open: all time" in md


def test_markdown_update_footer() -> None:
    opts = RenderOptions(now=NOW, max_age_days=30, recent_done_days=0, update_hint="update available")
    md = render_markdown([], opts)
    assert md.rstrip().endswith("update available")


def test_rich_note_line_is_a_copy_link(capsys: pytest.CaptureFixture[str]) -> None:
    console = Console(width=100, force_terminal=True)
    render_rich(_sections(), RenderOptions(now=NOW, max_age_days=30, recent_done_days=3), console)
    out = capsys.readouterr().out
    assert "copy:cd%20%2Fa%20b%20%26%26%20claude%20--resume%20x" in out
    assert "GitHub merged" in out and "ERROR: boom" in out
