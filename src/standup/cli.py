from __future__ import annotations

import concurrent.futures
import copy
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from standup import __version__, config, report, update
from standup.config import Config
from standup.log import dbg, enable_verbose, timed
from standup.render import RenderOptions, render_markdown, render_rich
from standup.sources import SOURCES
from standup.sources.base import Source

app = typer.Typer(add_completion=False, rich_markup_mode=None)
config_app = typer.Typer(help="Show, create or edit the config file.")
app.add_typer(config_app, name="config")

for _source in SOURCES:
    _group = _source.commands()
    if _group is not None:
        app.add_typer(_group, name=_source.name)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"standup-cli {__version__} ({(update.installed_commit() or 'unknown')[:7]})")
        raise typer.Exit()


def run_wizard(sources: list[Source[Any]], current: Config) -> Config:
    cfg = copy.deepcopy(current)
    for source in sources:
        table = dict(cfg["sources"][source.name])
        enabled = typer.confirm(
            f"Enable the {source.name} source?", default=bool(table.get("enabled", True))
        )
        if enabled:
            cfg["sources"][source.name] = dict(source.configure(config.source_table(cfg, source)))
        else:
            table["enabled"] = False
            cfg["sources"][source.name] = table
    cfg["report"]["max_age_days"] = typer.prompt(
        "Hide open items untouched for more than N days (0 = never)",
        default=cfg["report"]["max_age_days"],
        type=int,
    )
    cfg["report"]["recent_done_days"] = typer.prompt(
        "List items merged or done in the last N days (0 = off)",
        default=cfg["report"]["recent_done_days"],
        type=int,
    )
    return cfg


def _load_or_init() -> Config:
    cfg, exists = config.load(SOURCES)
    if exists:
        return cfg
    if not sys.stdout.isatty():
        typer.echo(f"No config at {config.config_path()}; run: standup config init", err=True)
        raise typer.Exit(1)
    typer.echo(f"No config at {config.config_path()}; let's create one.", err=True)
    cfg = run_wizard(SOURCES, cfg)
    config.save(cfg)
    return cfg


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    markdown: Annotated[bool, typer.Option("--markdown", help="Force markdown output")] = False,
    rich: Annotated[bool, typer.Option("--rich", help="Force rich (ANSI) output")] = False,
    max_age_days: Annotated[
        int | None,
        typer.Option(metavar="N", help="Hide open items not touched in the last N days (0 = no filter)"),
    ] = None,
    show_all: Annotated[bool, typer.Option("--all", help="No age filter on open items")] = False,
    include_handled: Annotated[
        bool, typer.Option(help="Keep PRs you already approved and duplicate Linear issues")
    ] = False,
    recent_done_days: Annotated[
        int | None,
        typer.Option(metavar="N", help="Also list items merged/done in the last N days (0 = off)"),
    ] = None,
    overrides: Annotated[
        list[str] | None,
        typer.Option(
            "-c",
            "--config",
            metavar="KEY=VALUE",
            help="Override a config value for this run, e.g. -c linear.comments=true",
        ),
    ] = None,
    envelope: Annotated[
        Path | None, typer.Option(help="Render this saved envelope instead of fetching")
    ] = None,
    save_envelope: Annotated[Path | None, typer.Option(help="Write the fetched envelope here")] = None,
    verbose: Annotated[
        bool, typer.Option("-v", "--verbose", help="Timestamped stage logs on stderr")
    ] = False,
    version: Annotated[bool, typer.Option("--version", callback=_print_version, is_eager=True)] = False,
) -> None:
    """Morning standup: open PRs, issues, not-green pipelines and reminders."""
    if ctx.invoked_subcommand is not None:
        return
    if markdown and rich:
        raise typer.BadParameter("--markdown and --rich are mutually exclusive")
    if show_all and max_age_days is not None:
        raise typer.BadParameter("--all and --max-age-days are mutually exclusive")
    if verbose:
        enable_verbose()

    cfg = _load_or_init()
    try:
        cfg = config.apply_overrides(cfg, overrides or [])
    except config.OverrideError as e:
        raise typer.BadParameter(str(e), param_hint="--config") from e
    if max_age_days is not None:
        cfg["report"]["max_age_days"] = max_age_days
    if show_all:
        cfg["report"]["max_age_days"] = 0
    if recent_done_days is not None:
        cfg["report"]["recent_done_days"] = max(recent_done_days, 0)
    hide_handled = not include_handled
    now = datetime.now(UTC)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        update_future = ex.submit(update.check_for_update, cfg["update"]["check"], now)
        if envelope is not None:
            dbg(f"loading saved envelope from {envelope}")
            data: dict[str, Any] = json.loads(envelope.read_text())
        else:
            data = report.fetch_all(SOURCES, cfg, hide_handled, now)
        update_hint = update_future.result()

    if save_envelope is not None:
        save_envelope.write_text(json.dumps(data, indent=2, default=str))

    with timed("build sections"):
        sections = report.build_sections(data, SOURCES, cfg, hide_handled, now)
    report.apply_age_filter(sections, now, cfg["report"]["max_age_days"])

    opts = RenderOptions(
        now=now,
        max_age_days=cfg["report"]["max_age_days"],
        recent_done_days=cfg["report"]["recent_done_days"],
        update_hint=update_hint,
    )
    use_markdown = markdown or (not rich and not sys.stdout.isatty())
    if use_markdown:
        typer.echo(render_markdown(sections, opts))
    else:
        render_rich(sections, opts)


@config_app.callback(invoke_without_command=True)
def config_show(ctx: typer.Context) -> None:
    """Print the config path and contents."""
    if ctx.invoked_subcommand is not None:
        return
    path = config.config_path()
    if not path.exists():
        typer.echo(f"{path} does not exist; run: standup config init")
        return
    typer.echo(f"# {path}")
    typer.echo(path.read_text())


@config_app.command("init")
def config_init() -> None:
    """Interactive setup; re-running pre-fills from the existing file."""
    current, _ = config.load(SOURCES)
    config.save(run_wizard(SOURCES, current))
    typer.echo(f"Wrote {config.config_path()}")


@config_app.command("edit")
def config_edit() -> None:
    """Open the config file in $EDITOR."""
    path = config.config_path()
    if not path.exists():
        cfg, _ = config.load(SOURCES)
        config.save(cfg)
    raise typer.Exit(subprocess.run([os.environ.get("EDITOR", "vi"), str(path)]).returncode)


@app.command("update")
def update_cmd() -> None:
    """Upgrade to the latest commit on main (uv tool upgrade standup-cli)."""
    raise typer.Exit(update.run_upgrade())
