from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Protocol, TypeVar, runtime_checkable

import typer

from standup.model import MergedCommit, Section

C = TypeVar("C", bound=Mapping[str, Any])


@dataclass
class SourceContext(Generic[C]):
    config: C
    now: datetime
    cutoff: datetime | None
    hide_handled: bool


class Source(Protocol[C]):
    name: str

    def defaults(self) -> C: ...

    def configure(self, current: C) -> C: ...

    def commands(self) -> typer.Typer | None: ...

    def fetch(self, ctx: SourceContext[C]) -> dict[str, Any]: ...

    def sections(self, raw: dict[str, Any], ctx: SourceContext[C]) -> list[Section]: ...


@runtime_checkable
class ProducesMergedCommits(Protocol):
    def merged_commits(self, raw: dict[str, Any]) -> dict[str, MergedCommit]: ...


@runtime_checkable
class FetchesDependent(Protocol):
    def fetch_dependent(
        self, ctx: SourceContext[Any], raw: dict[str, Any], merged: dict[str, MergedCommit]
    ) -> dict[str, Any]: ...
