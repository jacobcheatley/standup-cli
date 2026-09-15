from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TypedDict


class MergedCommit(TypedDict):
    title: str
    url: str


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def relative(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "?"
    secs = int((now - dt).total_seconds())
    if secs < 0:
        return "future"
    if secs < 3600:
        return f"{max(secs // 60, 1)}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    days = secs // 86400
    if days < 30:
        return f"{days}d ago"
    if days < 365:
        return f"{days // 30}mo ago"
    return f"{days // 365}y ago"


def age_style(dt: datetime | None, now: datetime) -> str:
    if dt is None:
        return "dim"
    days = (now - dt).days
    if days < 1:
        return "bold green"
    if days < 7:
        return "yellow"
    if days < 30:
        return "white"
    if days < 90:
        return "red"
    return "bright_black"


def short_name(name: str | None) -> str | None:
    """First word of a display name. 'Jessica Sharma' -> 'Jessica'."""
    if not name:
        return None
    parts = name.strip().split()
    return parts[0] if parts else None


@dataclass
class Row:
    key: str
    title: str
    meta: str
    meta_style: str
    updated: datetime | None
    url: str
    activity_at: datetime | None = None
    activity_by: str | None = None
    note: str | None = None


@dataclass
class Section:
    title: str
    color: str
    meta_header: str
    rows: list[Row]
    error: str | None = None
    done: bool = False
    age_filter: bool = True
    summary_label: str | None = None
    hidden: int = 0
