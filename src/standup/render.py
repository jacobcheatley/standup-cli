from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from urllib.parse import quote

from rich.console import Console
from rich.table import Table
from rich.text import Text

from standup.model import Row, Section, age_style, relative


def md_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ").strip()


def format_activity(r: Row, now: datetime) -> str:
    """Compact activity blurb: latest commenter + relative time."""
    parts: list[str] = []
    if r.activity_by:
        parts.append(r.activity_by)
    if r.activity_at is not None:
        parts.append(relative(r.activity_at, now))
    return " · ".join(parts) if parts else "—"


def render_section_md(
    out: list[str],
    title: str,
    rows: list[Row],
    err: str | None,
    now: datetime,
    meta_header: str,
    hidden: int = 0,
) -> None:
    if err:
        out.append(f"### {title} — ERROR: {err}")
        out.append("")
        if not rows:
            return
    else:
        suffix = f" · {hidden} hidden" if hidden else ""
        out.append(f"### {title} ({len(rows)}{suffix})")
        out.append("")
    if not rows:
        out.append("_(none)_")
        out.append("")
        return
    out.append(f"| ID | Title | {meta_header} | Updated | Activity |")
    out.append("|----|-------|------|---------|----------|")
    for r in rows:
        id_cell = f"[`{md_escape(r.key)}`]({r.url})" if r.url else f"`{md_escape(r.key)}`"
        title_cell = md_escape(r.title or "(untitled)")
        if r.note:
            title_cell += f" · `{md_escape(r.note)}`"
        out.append(
            f"| {id_cell} | {title_cell} | {md_escape(r.meta)} | {relative(r.updated, now)} "
            f"| {md_escape(format_activity(r, now))} |"
        )
    out.append("")


def render_section_rich(
    console: Console,
    title: str,
    rows: list[Row],
    err: str | None,
    now: datetime,
    source_color: str,
    meta_header: str,
    hidden: int = 0,
) -> None:
    header = Text()
    header.append(title, style=f"bold {source_color}")
    if err:
        header.append(f"  — ERROR: {err}", style="bold red")
    else:
        suffix = f"  ({len(rows)}" + (f" · {hidden} hidden" if hidden else "") + ")"
        header.append(suffix, style="bright_black")
    console.print()
    console.print(header)
    if err and not rows:
        return
    if not rows:
        console.print("  (none)", style="bright_black")
        return
    table = Table(
        show_header=True, header_style="bold bright_black", box=None, pad_edge=False, padding=(0, 1)
    )
    table.add_column("ID", style="bold", no_wrap=True)
    table.add_column("Title", overflow="ellipsis", max_width=64)
    table.add_column(meta_header, no_wrap=True)
    table.add_column("Updated", no_wrap=True)
    table.add_column("Activity", no_wrap=True)
    for r in rows:
        id_text = Text(r.key)
        title_text = Text(r.title or "(untitled)")
        if r.url:
            id_text.stylize(f"link {r.url}")
            title_text.stylize(f"link {r.url}")
        activity_text = Text(format_activity(r, now))
        if r.activity_at is not None:
            activity_text.stylize(age_style(r.activity_at, now))
        table.add_row(
            id_text,
            title_text,
            Text(r.meta, style=r.meta_style),
            Text(relative(r.updated, now), style=age_style(r.updated, now)),
            activity_text,
        )
    console.print(table)
    for r in rows:
        if r.note:
            # copy: links are handled by the copy-uri utility (see README)
            note_text = Text(f"  {r.key}  {r.note}", style="bright_black")
            note_text.stylize(f"link copy:{quote(r.note, safe='')}")
            console.print(note_text, soft_wrap=True)


@dataclass
class RenderOptions:
    now: datetime
    max_age_days: int
    recent_done_days: int
    update_hint: str | None = None


def _header_suffix(opts: RenderOptions) -> str:
    text = f" · open: last {opts.max_age_days}d" if opts.max_age_days > 0 else " · open: all time"
    if opts.recent_done_days > 0:
        text += f" · done: last {opts.recent_done_days}d"
    return text


def _totals(sections: list[Section]) -> list[tuple[str, str]]:
    return [(f"{len(s.rows)} {s.summary_label}", s.color) for s in sections if s.summary_label]


def render_markdown(sections: list[Section], opts: RenderOptions) -> str:
    now = opts.now
    open_sections = [s for s in sections if not s.done]
    done_sections = [s for s in sections if s.done] if opts.recent_done_days > 0 else []
    out: list[str] = [
        f"**Standup** · {now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}{_header_suffix(opts)}", "",
    ]
    out += ["## Open", ""]
    for s in open_sections:
        render_section_md(out, s.title, s.rows, s.error, now, s.meta_header, s.hidden)
    if done_sections:
        out += [f"## Recently merged/done (last {opts.recent_done_days}d) — verify deployments/QA", ""]
        for s in done_sections:
            render_section_md(out, s.title, s.rows, s.error, now, s.meta_header)
    total_hidden = sum(s.hidden for s in open_sections)
    hidden_suffix = (
        f" ({total_hidden} hidden older than {opts.max_age_days}d — use `--all` to show)"
        if total_hidden
        else ""
    )
    out.append("**Total open:** " + " · ".join(label for label, _ in _totals(open_sections)) + hidden_suffix)
    if done_sections:
        out.append(
            f"**Total done (last {opts.recent_done_days}d):** "
            + " · ".join(label for label, _ in _totals(done_sections))
        )
    if opts.update_hint:
        out += ["", opts.update_hint]
    return "\n".join(out)


def render_rich(sections: list[Section], opts: RenderOptions, console: Console | None = None) -> None:
    console = console or Console()
    now = opts.now
    open_sections = [s for s in sections if not s.done]
    done_sections = [s for s in sections if s.done] if opts.recent_done_days > 0 else []
    header = Text()
    header.append("Standup ", style="bold")
    header.append(now.astimezone().strftime("%Y-%m-%d %H:%M %Z") + _header_suffix(opts), style="bright_black")
    console.print(header)
    console.print()
    console.print(Text("Open", style="bold underline"))
    for s in open_sections:
        render_section_rich(console, s.title, s.rows, s.error, now, s.color, s.meta_header, s.hidden)
    if done_sections:
        console.print()
        console.print(
            Text(
                f"Recently merged/done (last {opts.recent_done_days}d) — verify deployments/QA",
                style="bold underline",
            )
        )
        for s in done_sections:
            render_section_rich(console, s.title, s.rows, s.error, now, s.color, s.meta_header)
    console.print()
    console.print(_totals_line("Total open: ", open_sections, _hidden_note(open_sections, opts)))
    if done_sections:
        console.print(_totals_line(f"Total done (last {opts.recent_done_days}d): ", done_sections, ""))
    if opts.update_hint:
        console.print()
        console.print(Text(opts.update_hint, style="bold yellow"))


def _hidden_note(open_sections: list[Section], opts: RenderOptions) -> str:
    total_hidden = sum(s.hidden for s in open_sections)
    if not total_hidden:
        return ""
    return f"  ({total_hidden} hidden older than {opts.max_age_days}d — `--all` to show)"


def _totals_line(prefix: str, sections: list[Section], suffix: str) -> Text:
    line = Text(prefix, style="bold")
    for i, (label, color) in enumerate(_totals(sections)):
        if i:
            line.append(" · ")
        line.append(label, style=color)
    if suffix:
        line.append(suffix, style="bright_black")
    return line
