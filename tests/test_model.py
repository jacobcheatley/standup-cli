from datetime import UTC, datetime, timedelta

from standup.model import Section, age_style, parse_iso, relative, short_name

NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)


def test_parse_iso_accepts_z_suffix_and_naive_as_utc() -> None:
    assert parse_iso("2026-09-15T08:00:00Z") == datetime(2026, 9, 15, 8, tzinfo=UTC)
    assert parse_iso("2026-09-15T08:00:00") == datetime(2026, 9, 15, 8, tzinfo=UTC)
    assert parse_iso(None) is None
    assert parse_iso("not a date") is None


def test_relative_buckets_by_age() -> None:
    assert relative(NOW - timedelta(minutes=5), NOW) == "5m ago"
    assert relative(NOW - timedelta(hours=3), NOW) == "3h ago"
    assert relative(NOW - timedelta(days=2), NOW) == "2d ago"
    assert relative(NOW - timedelta(days=45), NOW) == "1mo ago"
    assert relative(NOW + timedelta(days=1), NOW) == "future"
    assert relative(None, NOW) == "?"


def test_age_style_boundaries() -> None:
    assert age_style(NOW - timedelta(hours=23), NOW) == "bold green"
    assert age_style(NOW - timedelta(days=1), NOW) == "yellow"
    assert age_style(NOW - timedelta(days=100), NOW) == "bright_black"
    assert age_style(None, NOW) == "dim"


def test_short_name_takes_first_word() -> None:
    assert short_name("Jane Q Public") == "Jane"
    assert short_name("  ") is None
    assert short_name(None) is None


def test_section_defaults_are_open_and_age_filtered() -> None:
    section = Section(title="X", color="blue", meta_header="Role", rows=[])
    assert section.done is False
    assert section.age_filter is True
    assert section.hidden == 0
