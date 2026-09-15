from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any, TypedDict, cast

import tomli_w

TOP_LEVEL_TABLES = ("report", "update", "sources")


class ReportConfig(TypedDict):
    max_age_days: int
    recent_done_days: int


class UpdateConfig(TypedDict):
    check: bool


class Config(TypedDict):
    report: ReportConfig
    update: UpdateConfig
    sources: dict[str, dict[str, Any]]


class OverrideError(ValueError):
    pass


def config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "standup" / "config.toml"


def state_dir() -> Path:
    base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "standup"


from standup.sources.base import C, Source  # noqa: E402


def defaults(sources: list[Source[Any]]) -> Config:
    return {
        "report": {"max_age_days": 30, "recent_done_days": 3},
        "update": {"check": True},
        "sources": {s.name: dict(s.defaults()) for s in sources},
    }


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def load(sources: list[Source[Any]], path: Path | None = None) -> tuple[Config, bool]:
    path = path or config_path()
    base = cast(dict[str, Any], defaults(sources))
    if not path.exists():
        return cast(Config, base), False
    with path.open("rb") as f:
        on_disk = tomllib.load(f)
    return cast(Config, _deep_merge(base, on_disk)), True


def save(cfg: Config, path: Path | None = None) -> None:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        tomli_w.dump(cast(dict[str, Any], cfg), f)


def source_table(cfg: Config, source: Source[C]) -> C:
    return cast(C, cfg["sources"][source.name])


def is_enabled(cfg: Config, source: Source[Any]) -> bool:
    return bool(cfg["sources"][source.name].get("enabled", True))


def _parse_value(text: str) -> Any:
    try:
        return tomllib.loads(f"v = {text}")["v"]
    except tomllib.TOMLDecodeError:
        return text


def _resolve_key(cfg: Config, key: str) -> list[str]:
    parts = key.split(".")
    if parts[0] not in TOP_LEVEL_TABLES:
        parts = ["sources", *parts]
    if (len(parts) < 3 and parts[0] == "sources") or len(parts) < 2:
        raise OverrideError(f"config key {key!r} must name a table and a key, like linear.comments")
    if parts[0] == "sources" and parts[1] not in cfg["sources"]:
        raise OverrideError(f"unknown source {parts[1]!r} in config key {key!r}")
    return parts


def apply_overrides(cfg: Config, overrides: list[str]) -> Config:
    out = copy.deepcopy(cast(dict[str, Any], cfg))
    for text in overrides:
        key, sep, value = text.partition("=")
        if not sep:
            raise OverrideError(f"config override {text!r} must look like KEY=VALUE")
        parts = _resolve_key(cfg, key)
        table = out
        for part in parts[:-1]:
            table = table.setdefault(part, {})
        table[parts[-1]] = _parse_value(value)
    return cast(Config, out)
