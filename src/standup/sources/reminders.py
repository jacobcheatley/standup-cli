from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, TypedDict

import typer

from standup.config import state_dir
from standup.model import Row, Section, parse_iso, relative
from standup.sources.base import SourceContext


class RemindersConfig(TypedDict):
    enabled: bool


def reminders_dir() -> Path:
    return state_dir() / "reminders"


def load_reminders() -> list[dict[str, Any]]:
    if not reminders_dir().exists():
        return []
    items: list[dict[str, Any]] = []
    for path in sorted(reminders_dir().glob("*.json"), key=lambda p: int(p.stem)):
        item = json.loads(path.read_text())
        item["id"] = path.stem
        items.append(item)
    return items


def _next_reminder_id() -> str:
    taken = [int(p.stem) for p in reminders_dir().glob("*.json")]
    return str(max(taken, default=0) + 1)


def fetch_reminders() -> tuple[list[dict[str, Any]], str | None]:
    try:
        return load_reminders(), None
    except (OSError, ValueError) as e:
        return [], f"{type(e).__name__}: {e}"


def _parse_due(s: str) -> datetime:
    """ISO 8601 date/time; a naive value is local time."""
    dt = datetime.fromisoformat(s)
    return dt.astimezone() if dt.tzinfo is None else dt


def _claude_session_defaults() -> dict[str, str]:
    """Resume command + claude.ai link for the Claude Code session this runs inside, if any."""
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return {}
    out = {"note": f"cd {os.getcwd()} && claude --resume {session_id}"}
    bridge_id = os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID")
    if bridge_id:
        out["url"] = f"https://claude.ai/code/{bridge_id}"
    return out


def reminder_add(
    title: str, due: str | None, url: str | None, note: str | None, use_session: bool = True
) -> str:
    """Write a new reminder file and return its id. url/note default to the Claude session."""
    defaults = _claude_session_defaults() if use_session else {}
    now = datetime.now(UTC)
    reminder = {
        "title": title,
        "due": _parse_due(due).isoformat() if due else None,
        "url": url or defaults.get("url"),
        "note": note or defaults.get("note"),
        "created": now.isoformat(),
        "done": None,
    }
    reminders_dir().mkdir(parents=True, exist_ok=True)
    reminder_id = _next_reminder_id()
    with open(reminders_dir() / f"{reminder_id}.json", "x") as f:
        json.dump(reminder, f, indent=2)
    return reminder_id


def reminder_done(reminder_id: str) -> str:
    """Mark reminder_id done (idempotent); return its id."""
    path = reminders_dir() / f"{reminder_id}.json"
    if not path.exists():
        raise ValueError(f"no reminder with id {reminder_id!r} in {reminders_dir()}")
    reminder = json.loads(path.read_text())
    if not reminder.get("done"):
        reminder["done"] = datetime.now(UTC).isoformat()
        path.write_text(json.dumps(reminder, indent=2))
    return path.stem


def _due_label(due: datetime | None, now: datetime) -> tuple[str, str]:
    if due is None:
        return "no due", "bright_black"
    if due < now:
        return "overdue " + relative(due, now).removesuffix(" ago"), "bold red"
    label = "in " + relative(now, due).removesuffix(" ago")
    return label, ("yellow" if due - now < timedelta(days=1) else "white")


def _reminder_row(it: dict[str, Any], now: datetime) -> Row:
    label, style = _due_label(parse_iso(it.get("due")), now)
    return Row(
        key=it["id"],
        title=it.get("title") or "",
        meta=label,
        meta_style=style,
        updated=parse_iso(it.get("created")),
        url=it.get("url") or "",
        note=it.get("note") or None,
    )


def reminder_open_rows(items: list[dict[str, Any]], now: datetime) -> list[Row]:
    """Undone reminders, soonest due first; undated last."""
    far_future = datetime.max.replace(tzinfo=UTC)
    pending = sorted(
        (it for it in items if not it.get("done")),
        key=lambda it: parse_iso(it.get("due")) or far_future,
    )
    return [_reminder_row(it, now) for it in pending]


def reminder_done_rows(items: list[dict[str, Any]], cutoff: datetime, now: datetime) -> list[Row]:
    """Reminders marked done on/after cutoff, most recent first."""
    rows: list[Row] = []
    for it in items:
        done_at = parse_iso(it.get("done"))
        if done_at is None or done_at < cutoff:
            continue
        row = _reminder_row(it, now)
        row.meta, row.meta_style, row.updated = "done", "green", done_at
        rows.append(row)
    rows.sort(key=lambda r: r.updated or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


commands = typer.Typer(help="Personal reminders, shown at the top of the report.")


@commands.command()
def add(
    title: str,
    due: Annotated[
        str | None, typer.Option(help="ISO 8601 date/time, e.g. 2026-09-15T10:00 (local time)")
    ] = None,
    url: Annotated[
        str | None,
        typer.Option(help="Click target; defaults to the current Claude session's claude.ai page"),
    ] = None,
    note: Annotated[
        str | None,
        typer.Option(help="Shown under the row; defaults to the current Claude session's resume command"),
    ] = None,
    session: Annotated[
        bool, typer.Option("--session/--no-session", help="Default url/note from the current Claude session")
    ] = True,
) -> None:
    """Create a reminder and print its id."""
    try:
        typer.echo(reminder_add(title, due, url, note, session))
    except ValueError as e:
        typer.echo(f"reminder: {e}", err=True)
        raise typer.Exit(2) from e


@commands.command()
def done(reminder_id: Annotated[str, typer.Argument(metavar="ID")]) -> None:
    """Mark a reminder done."""
    try:
        typer.echo(f"done: {reminder_done(reminder_id)}")
    except ValueError as e:
        typer.echo(f"reminder: {e}", err=True)
        raise typer.Exit(2) from e


@commands.command("list")
def list_(
    include_done: Annotated[bool, typer.Option("--all", help="Include done reminders")] = False,
) -> None:
    """List open reminders."""
    now = datetime.now(UTC)
    items = load_reminders()
    rows = reminder_open_rows(items, now)
    if include_done:
        rows += reminder_done_rows(items, datetime.min.replace(tzinfo=UTC), now)
    for r in rows:
        typer.echo(f"{r.key:>4}  {r.meta:<12} {r.title}")
        if r.note:
            typer.echo(f"      {r.note}")


class RemindersSource:
    name = "reminders"

    def defaults(self) -> RemindersConfig:
        return {"enabled": True}

    def configure(self, current: RemindersConfig) -> RemindersConfig:
        return current

    def commands(self) -> typer.Typer:
        return commands

    def fetch(self, ctx: SourceContext[RemindersConfig]) -> dict[str, Any]:
        items, error = fetch_reminders()
        return {"raw": items, "error": error}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[RemindersConfig]) -> list[Section]:
        error = raw.get("error")
        items = raw.get("raw") or []
        result = [
            Section(
                title="Reminders", color="yellow", meta_header="Due",
                rows=reminder_open_rows(items, ctx.now) if not error else [],
                error=error, age_filter=False, summary_label="reminders",
            )
        ]
        if ctx.cutoff is not None:
            result.append(
                Section(
                    title="Reminders done", color="yellow", meta_header="Due",
                    rows=reminder_done_rows(items, ctx.cutoff, ctx.now) if not error else [],
                    error=error, done=True,
                )
            )
        return result


source = RemindersSource()
