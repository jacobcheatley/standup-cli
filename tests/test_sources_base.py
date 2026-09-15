from datetime import UTC, datetime
from typing import Any, TypedDict

import typer

from standup.model import Section
from standup.sources.base import FetchesDependent, ProducesMergedCommits, Source, SourceContext


class FakeConfig(TypedDict):
    enabled: bool
    label: str


class FakeSource:
    name = "fake"

    def defaults(self) -> FakeConfig:
        return {"enabled": True, "label": "x"}

    def configure(self, current: FakeConfig) -> FakeConfig:
        return current

    def commands(self) -> typer.Typer | None:
        return None

    def fetch(self, ctx: SourceContext[FakeConfig]) -> dict[str, Any]:
        return {"label": ctx.config["label"]}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[FakeConfig]) -> list[Section]:
        return [Section(title=raw["label"], color="white", meta_header="", rows=[])]


def test_a_plain_class_satisfies_the_protocol() -> None:
    source: Source[FakeConfig] = FakeSource()
    ctx = SourceContext(
        config=source.defaults(), now=datetime.now(UTC), cutoff=None, hide_handled=True
    )
    assert source.sections(source.fetch(ctx), ctx)[0].title == "x"


def test_optional_hooks_are_detected_at_runtime() -> None:
    assert not isinstance(FakeSource(), ProducesMergedCommits)
    assert not isinstance(FakeSource(), FetchesDependent)
