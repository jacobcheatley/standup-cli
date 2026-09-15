from typer.testing import CliRunner

from standup.cli import app


def test_version_flag_prints_distribution_version() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0
    assert result.output.startswith("standup-cli 0.1.0")
