import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from standup.sources import github
from standup.sources.base import SourceContext


def _fake_run(calls: list[list[str]], responses: list[dict[str, Any]] | None = None) -> Any:
    queue = list(responses or [])

    def run(cmd: list[str], timeout: float = 30.0) -> str:
        calls.append(cmd)
        body = queue.pop(0) if queue else {"total_count": 0, "items": []}
        return json.dumps(body)

    return run


def _query_of(cmd: list[str]) -> str:
    return next(a for a in cmd if a.startswith("q=")).removeprefix("q=")


def _item(owner: str, repo: str, number: int) -> dict[str, Any]:
    return {
        "html_url": f"https://github.com/{owner}/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/{owner}/{repo}",
        "number": number,
        "title": f"{repo} #{number}",
        "updated_at": "2026-09-14T00:00:00Z",
    }


def test_gh_search_appends_org_qualifier(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(github, "run", _fake_run(calls))
    github.gh_search("is:open is:pr author:@me", ["acme"])
    assert len(calls) == 1
    assert _query_of(calls[0]) == "is:open is:pr author:@me org:acme"


def test_gh_search_empty_orgs_leaves_query_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(github, "run", _fake_run(calls))
    github.gh_search("is:open is:pr author:@me", [])
    assert _query_of(calls[0]) == "is:open is:pr author:@me"


def test_gh_search_multiple_orgs_runs_one_search_each_and_merges(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    a, b = _item("acme", "widgets", 1), _item("other-org", "thing", 2)
    monkeypatch.setattr(github, "run", _fake_run(calls, [
        {"total_count": 1, "items": [a]},
        {"total_count": 1, "items": [b]},
    ]))
    out = github.gh_search("is:pr author:@me", ["acme", "other-org"])
    assert [_query_of(c) for c in calls] == ["is:pr author:@me org:acme", "is:pr author:@me org:other-org"]
    assert out["total_count"] == 2
    assert [i["html_url"] for i in out["items"]] == [a["html_url"], b["html_url"]]


def test_open_rows_drop_items_outside_allowed_orgs() -> None:
    payload = {"author": {"items": [_item("acme", "widgets", 1), _item("someone", "dotfiles", 2)]}}
    rows = github.github_open_rows(payload, ["acme"])
    assert [r.key for r in rows] == ["widgets#1"]


def test_open_rows_owner_match_is_case_insensitive() -> None:
    payload = {"author": {"items": [_item("ACME", "widgets", 1)]}}
    assert [r.key for r in github.github_open_rows(payload, ["acme"])] == ["widgets#1"]


def test_open_rows_keep_everything_when_orgs_empty() -> None:
    payload = {"author": {"items": [_item("acme", "widgets", 1), _item("someone", "dotfiles", 2)]}}
    assert sorted(r.key for r in github.github_open_rows(payload, [])) == ["dotfiles#2", "widgets#1"]


def test_open_rows_merge_roles_for_same_pr() -> None:
    item = _item("acme", "widgets", 1)
    payload = {"author": {"items": [item]}, "reviewer": {"items": [dict(item)]}}
    rows = github.github_open_rows(payload, [])
    assert len(rows) == 1 and rows[0].meta == "author+reviewer"


def test_sections_and_merged_commits() -> None:
    now = datetime(2026, 9, 15, tzinfo=UTC)
    done_item = {
        **_item("acme", "widgets", 3),
        "__merge_sha": "ABCDEF",
        "pull_request": {"merged_at": "2026-09-14T00:00:00Z"},
    }
    raw = {
        "open": {"author": {"items": [_item("acme", "widgets", 1)]}},
        "done": {"author": {"items": [done_item]}},
        "open_error": None,
        "done_error": None,
    }
    ctx: SourceContext[Any] = SourceContext(
        config={"enabled": True, "orgs": []}, now=now, cutoff=now - timedelta(days=3), hide_handled=True
    )
    sections = github.source.sections(raw, ctx)
    assert [s.title for s in sections] == ["GitHub", "GitHub merged"]
    assert [r.key for r in sections[0].rows] == ["widgets#1"]
    assert sections[1].done and [r.key for r in sections[1].rows] == ["widgets#3"]
    assert github.source.merged_commits(raw) == {
        "abcdef": {"title": "widgets #3", "url": done_item["html_url"]}
    }


def test_sections_report_errors_without_rows() -> None:
    now = datetime.now(UTC)
    ctx: SourceContext[Any] = SourceContext(
        config={"enabled": True, "orgs": []}, now=now, cutoff=None, hide_handled=True
    )
    sections = github.source.sections({"open": {}, "open_error": "gh exited 1"}, ctx)
    assert len(sections) == 1 and sections[0].error == "gh exited 1" and sections[0].rows == []
