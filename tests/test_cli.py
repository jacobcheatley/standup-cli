import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from standup import config
from standup.cli import app
from standup.sources import SOURCES

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


def _write_config() -> None:
    cfg = config.defaults(SOURCES)
    config.save(cfg)


def _envelope(tmp_path: Path) -> Path:
    now = datetime.now(UTC)
    envelope: dict[str, Any] = {
        "generated_at": now.isoformat(),
        "recent_done_cutoff": None,
        "sources": {
            "reminders": {"raw": [{"id": "1", "title": "ping", "due": None}], "error": None},
            "github": {"open": {}, "done": {}, "open_error": "gh exited 1", "done_error": None},
        },
    }
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(envelope))
    return path


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.output.startswith("standup-cli 0.1.0")


def test_source_groups_are_mounted() -> None:
    assert runner.invoke(app, ["reminders", "--help"]).exit_code == 0
    assert runner.invoke(app, ["linear", "--help"]).exit_code == 0
    assert runner.invoke(app, ["github", "--help"]).exit_code != 0


def test_envelope_renders_markdown_without_fetching(tmp_path: Path) -> None:
    _write_config()
    result = runner.invoke(app, ["--markdown", "--envelope", str(_envelope(tmp_path))])
    assert result.exit_code == 0, result.output
    assert "### Reminders (1)" in result.output and "ping" in result.output
    assert "### GitHub — ERROR: gh exited 1" in result.output
    assert "Azure DevOps" not in result.output, "sources absent from the envelope render nothing"


def test_config_override_disables_a_source(tmp_path: Path) -> None:
    _write_config()
    result = runner.invoke(
        app, ["--markdown", "--envelope", str(_envelope(tmp_path)), "-c", "reminders.enabled=false"]
    )
    assert result.exit_code == 0, result.output
    assert "Reminders" not in result.output


def test_bad_config_override_is_a_usage_error(tmp_path: Path) -> None:
    _write_config()
    result = runner.invoke(app, ["--envelope", str(_envelope(tmp_path)), "-c", "nosuch.key=1"])
    assert result.exit_code == 2 and "unknown source" in result.output


def test_missing_config_non_interactive_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(app, ["--markdown", "--envelope", str(_envelope(tmp_path))])
    assert result.exit_code == 1 and "standup config init" in result.output


def test_config_show_and_init(tmp_path: Path) -> None:
    assert "does not exist" in runner.invoke(app, ["config"]).output
    answers = "\n".join([
        "y",            # reminders enabled
        "y", "acme",    # github enabled, orgs
        "n",            # ado disabled
        "y", "n",       # linear enabled, comments
        "14", "5",      # report max_age_days, recent_done_days
    ]) + "\n"
    result = runner.invoke(app, ["config", "init"], input=answers)
    assert result.exit_code == 0, result.output
    cfg, exists = config.load(SOURCES)
    assert exists
    assert cfg["sources"]["github"] == {"enabled": True, "orgs": ["acme"]}
    assert cfg["sources"]["ado"]["enabled"] is False
    assert cfg["report"] == {"max_age_days": 14, "recent_done_days": 5}
    assert "acme" in runner.invoke(app, ["config"]).output
