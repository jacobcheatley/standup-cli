from pathlib import Path
from typing import Any, TypedDict

import pytest
import typer

from standup import config
from standup.model import Section
from standup.sources.base import SourceContext


class FakeConfig(TypedDict):
    enabled: bool
    orgs: list[str]


class FakeSource:
    name = "fake"

    def defaults(self) -> FakeConfig:
        return {"enabled": True, "orgs": []}

    def configure(self, current: FakeConfig) -> FakeConfig:
        return current

    def commands(self) -> typer.Typer | None:
        return None

    def fetch(self, ctx: SourceContext[FakeConfig]) -> dict[str, Any]:
        return {}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[FakeConfig]) -> list[Section]:
        return []


SOURCES: list[Any] = [FakeSource()]


def test_paths_follow_xdg_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert config.config_path() == tmp_path / "cfg" / "standup" / "config.toml"
    assert config.state_dir() == tmp_path / "data" / "standup"


def test_missing_file_yields_defaults(tmp_path: Path) -> None:
    cfg, exists = config.load(SOURCES, tmp_path / "config.toml")
    assert exists is False
    assert cfg["report"] == {"max_age_days": 30, "recent_done_days": 3}
    assert cfg["update"] == {"check": True}
    assert cfg["sources"]["fake"] == {"enabled": True, "orgs": []}


def test_save_then_load_round_trips_and_merges_over_defaults(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    cfg = config.defaults(SOURCES)
    cfg["report"]["max_age_days"] = 7
    cfg["sources"]["fake"]["orgs"] = ["a", "b"]
    cfg["sources"]["fake"]["extra"] = "kept"
    config.save(cfg, path)
    loaded, exists = config.load(SOURCES, path)
    assert exists is True
    assert loaded == cfg


def test_source_table_and_is_enabled() -> None:
    cfg = config.defaults(SOURCES)
    source = FakeSource()
    assert config.source_table(cfg, source)["orgs"] == []
    cfg["sources"]["fake"]["enabled"] = False
    assert config.is_enabled(cfg, source) is False


def test_override_prefixes_source_names_and_types_values() -> None:
    cfg = config.defaults(SOURCES)
    out = config.apply_overrides(cfg, ["fake.orgs=[\"x\"]", "fake.enabled=false", "report.max_age_days=0"])
    assert out["sources"]["fake"] == {"enabled": False, "orgs": ["x"]}
    assert out["report"]["max_age_days"] == 0
    assert cfg["report"]["max_age_days"] == 30, "original is not mutated"


def test_override_falls_back_to_string_for_bare_words() -> None:
    cfg = config.defaults(SOURCES)
    out = config.apply_overrides(cfg, ["sources.fake.orgs=plain"])
    assert out["sources"]["fake"]["orgs"] == "plain"


@pytest.mark.parametrize("bad", ["nosuch.key=1", "fake=1", "noequals", "fake.orgs"])
def test_override_rejects_bad_keys(bad: str) -> None:
    with pytest.raises(config.OverrideError):
        config.apply_overrides(config.defaults(SOURCES), [bad])
