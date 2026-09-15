from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

import typer

from standup import config, report
from standup.model import MergedCommit, Row, Section
from standup.sources.base import SourceContext

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


class Cfg(TypedDict):
    enabled: bool


def _row(key: str, age_days: int) -> Row:
    return Row(key=key, title=key, meta="", meta_style="", updated=NOW - timedelta(days=age_days), url="")


class Producer:
    name = "producer"

    def defaults(self) -> Cfg:
        return {"enabled": True}

    def configure(self, current: Cfg) -> Cfg:
        return current

    def commands(self) -> typer.Typer | None:
        return None

    def fetch(self, ctx: SourceContext[Cfg]) -> dict[str, Any]:
        return {"shas": ["ABC"]}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[Cfg]) -> list[Section]:
        rows = [_row("new", 1), _row("old", 90)]
        return [Section(title="Producer", color="white", meta_header="", rows=rows)]

    def merged_commits(self, raw: dict[str, Any]) -> dict[str, MergedCommit]:
        return {s.lower(): {"title": s, "url": ""} for s in raw["shas"]}


class Consumer:
    name = "consumer"
    seen: dict[str, MergedCommit] = {}

    def defaults(self) -> Cfg:
        return {"enabled": True}

    def configure(self, current: Cfg) -> Cfg:
        return current

    def commands(self) -> typer.Typer | None:
        return None

    def fetch(self, ctx: SourceContext[Cfg]) -> dict[str, Any]:
        return {"base": True}

    def fetch_dependent(
        self, ctx: SourceContext[Any], raw: dict[str, Any], merged: dict[str, MergedCommit]
    ) -> dict[str, Any]:
        self.seen = merged
        return {**raw, "matched": sorted(merged)}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[Cfg]) -> list[Section]:
        return [Section(title="Consumer", color="white", meta_header="", rows=[], done=True)]


class Exploding:
    name = "exploding"

    def defaults(self) -> Cfg:
        return {"enabled": True}

    def configure(self, current: Cfg) -> Cfg:
        return current

    def commands(self) -> typer.Typer | None:
        return None

    def fetch(self, ctx: SourceContext[Cfg]) -> dict[str, Any]:
        raise RuntimeError("kaboom")

    def sections(self, raw: dict[str, Any], ctx: SourceContext[Cfg]) -> list[Section]:
        raise AssertionError("must not be called for a fatal payload")


SOURCES: list[Any] = [Producer(), Consumer(), Exploding()]


def test_fetch_all_runs_dependent_step_with_union_of_merged_commits() -> None:
    cfg = config.defaults(SOURCES)
    envelope = report.fetch_all(SOURCES, cfg, hide_handled=True, now=NOW)
    assert envelope["sources"]["consumer"]["matched"] == ["abc"]
    assert envelope["recent_done_cutoff"] == (NOW - timedelta(days=3)).isoformat()


def test_fetch_all_skips_dependent_step_without_cutoff() -> None:
    cfg = config.defaults(SOURCES)
    cfg["report"]["recent_done_days"] = 0
    envelope = report.fetch_all(SOURCES, cfg, hide_handled=True, now=NOW)
    assert "matched" not in envelope["sources"]["consumer"]
    assert envelope["recent_done_cutoff"] is None


def test_fetch_all_turns_an_exception_into_a_fatal_payload() -> None:
    envelope = report.fetch_all(SOURCES, config.defaults(SOURCES), hide_handled=True, now=NOW)
    assert envelope["sources"]["exploding"] == {report.FATAL_KEY: "RuntimeError: kaboom"}


def test_disabled_source_is_absent_from_envelope_and_sections() -> None:
    cfg = config.defaults(SOURCES)
    cfg["sources"]["producer"]["enabled"] = False
    envelope = report.fetch_all(SOURCES, cfg, hide_handled=True, now=NOW)
    assert "producer" not in envelope["sources"]
    titles = [s.title for s in report.build_sections(envelope, SOURCES, cfg, True, NOW)]
    assert "Producer" not in titles


def test_build_sections_renders_fatal_as_error_section() -> None:
    cfg = config.defaults(SOURCES)
    envelope = {"generated_at": NOW.isoformat(), "recent_done_cutoff": None,
                "sources": {"exploding": {report.FATAL_KEY: "RuntimeError: kaboom"}}}
    sections = report.build_sections(envelope, SOURCES, cfg, True, NOW)
    assert [(s.title, s.error) for s in sections] == [("exploding", "RuntimeError: kaboom")]


def test_age_filter_drops_old_open_rows_and_counts_them() -> None:
    cfg = config.defaults(SOURCES)
    envelope = report.fetch_all([Producer()], cfg, hide_handled=True, now=NOW)
    sections = report.build_sections(envelope, [Producer()], cfg, True, NOW)
    report.apply_age_filter(sections, NOW, max_age_days=30)
    assert [r.key for r in sections[0].rows] == ["new"] and sections[0].hidden == 1
    report.apply_age_filter(sections, NOW, max_age_days=0)
    assert sections[0].hidden == 1, "0 disables the filter and leaves counts alone"
