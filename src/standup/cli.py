import typer

from standup import __version__

app = typer.Typer(add_completion=False)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"standup-cli {__version__}")
        raise typer.Exit()


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", callback=_print_version, is_eager=True),
) -> None:
    pass
