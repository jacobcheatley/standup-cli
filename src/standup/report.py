from __future__ import annotations

import concurrent.futures
from datetime import datetime, timedelta
from typing import Any

from standup import config as config_mod
from standup.config import Config
from standup.log import dbg, timed
from standup.model import MergedCommit, Section, parse_iso
from standup.sources.base import FetchesDependent, ProducesMergedCommits, Source, SourceContext

FATAL_KEY = "fatal_error"


def cutoff_for(cfg: Config, now: datetime) -> datetime | None:
    days = cfg["report"]["recent_done_days"]
    return now - timedelta(days=days) if days > 0 else None


def _context(
    source: Source[Any], cfg: Config, now: datetime, cutoff: datetime | None, hide_handled: bool
) -> SourceContext[Any]:
    return SourceContext(
        config=config_mod.source_table(cfg, source), now=now, cutoff=cutoff, hide_handled=hide_handled
    )


def _guarded(label: str, call: Any) -> dict[str, Any]:
    with timed(label):
        try:
            result: dict[str, Any] = call()
            return result
        except Exception as e:
            dbg(f"{label}: ERROR {type(e).__name__}: {e}")
            return {FATAL_KEY: f"{type(e).__name__}: {e}"}


def fetch_all(sources: list[Source[Any]], cfg: Config, hide_handled: bool, now: datetime) -> dict[str, Any]:
    cutoff = cutoff_for(cfg, now)
    enabled = [s for s in sources if config_mod.is_enabled(cfg, s)]
    contexts = {s.name: _context(s, cfg, now, cutoff, hide_handled) for s in enabled}
    raw: dict[str, dict[str, Any]] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=max(len(enabled), 1))
    with timed("fetch_all: all sources"), pool as ex:
        futures = {
            s.name: ex.submit(_guarded, f"{s.name}: fetch", lambda s=s: s.fetch(contexts[s.name]))
            for s in enabled
        }
        for name, future in futures.items():
            raw[name] = future.result()

        if cutoff is not None:
            merged: dict[str, MergedCommit] = {}
            for s in enabled:
                if isinstance(s, ProducesMergedCommits) and FATAL_KEY not in raw[s.name]:
                    merged.update(s.merged_commits(raw[s.name]))
            dbg(f"fetch_all: {len(merged)} merged commit(s) to match")
            dependents = [
                s for s in enabled if isinstance(s, FetchesDependent) and FATAL_KEY not in raw[s.name]
            ]
            futures = {
                s.name: ex.submit(_guarded, f"{s.name}: fetch_dependent",
                                  lambda s=s: s.fetch_dependent(contexts[s.name], raw[s.name], merged))
                for s in dependents
            }
            for name, future in futures.items():
                raw[name] = future.result()

    return {
        "generated_at": now.isoformat(),
        "recent_done_cutoff": cutoff.isoformat() if cutoff else None,
        "sources": raw,
    }


def build_sections(
    envelope: dict[str, Any], sources: list[Source[Any]], cfg: Config, hide_handled: bool, now: datetime
) -> list[Section]:
    cutoff = parse_iso(envelope.get("recent_done_cutoff"))
    payloads: dict[str, dict[str, Any]] = envelope.get("sources") or {}
    sections: list[Section] = []
    for s in sources:
        if s.name not in payloads or not config_mod.is_enabled(cfg, s):
            continue
        raw = payloads[s.name]
        if FATAL_KEY in raw:
            sections.append(Section(title=s.name, color="red", meta_header="", rows=[], error=raw[FATAL_KEY]))
            continue
        sections.extend(s.sections(raw, _context(s, cfg, now, cutoff, hide_handled)))
    return sections


def apply_age_filter(sections: list[Section], now: datetime, max_age_days: int) -> None:
    if max_age_days <= 0:
        return
    max_age = timedelta(days=max_age_days)
    for section in sections:
        if section.done or not section.age_filter:
            continue
        kept = [r for r in section.rows if r.updated is None or now - r.updated <= max_age]
        section.hidden = len(section.rows) - len(kept)
        section.rows = kept
