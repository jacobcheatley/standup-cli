from datetime import UTC, datetime, timedelta
from typing import Any

from standup.sources import ado
from standup.sources.base import SourceContext

NOW = datetime(2026, 9, 15, tzinfo=UTC)
ORG_URL = "https://dev.azure.com/acme"


def _pr(pr_id: int, role: str, sha: str | None = None) -> dict[str, Any]:
    return {
        "pullRequestId": pr_id,
        "title": f"PR {pr_id}",
        "repository": {"name": "repo", "project": {"name": "Proj A"}},
        "creationDate": "2026-09-14T00:00:00Z",
        "closedDate": "2026-09-14T12:00:00Z",
        "lastMergeCommit": {"commitId": sha} if sha else None,
        "__role": role,
    }


def _build(build_id: int, sha: str, status: str, result: str | None) -> dict[str, Any]:
    return {
        "id": build_id, "buildNumber": str(build_id), "sourceVersion": sha,
        "status": status, "result": result, "queueTime": "2026-09-14T13:00:00Z",
        "definition": {"name": "CI"}, "__project": "Proj A",
        "requestedFor": {"displayName": "Microsoft.VisualStudio.Services.TFS"},
    }


def test_open_rows_merge_roles_and_build_urls() -> None:
    rows = ado.ado_open_rows([_pr(7, "reviewer"), _pr(7, "creator")], ORG_URL)
    assert len(rows) == 1
    assert rows[0].key == "repo !7" and rows[0].meta == "reviewer+creator"
    assert rows[0].url == f"{ORG_URL}/Proj%20A/_git/repo/pullrequest/7"


def test_pipeline_rows_collapse_state_and_system_requester() -> None:
    build = {**_build(1, "abc", "completed", "partiallySucceeded"), "__pr": {"title": "PR 1", "url": "u"}}
    rows = ado.ado_pipeline_rows([build], ORG_URL)
    assert rows[0].key == "CI #1" and rows[0].meta == "partial" and rows[0].activity_by == "auto"
    assert rows[0].url == f"{ORG_URL}/Proj%20A/_build/results?buildId=1"


def test_merged_commits_lowercases_shas_and_skips_unmerged() -> None:
    raw = {"org_url": ORG_URL, "done": [_pr(1, "creator", "ABC123"), _pr(2, "creator", None)]}
    merged = ado.source.merged_commits(raw)
    assert merged == {"abc123": {"title": "PR 1", "url": f"{ORG_URL}/Proj%20A/_git/repo/pullrequest/1"}}


def test_sections_cover_open_done_and_pipelines() -> None:
    raw = {
        "org_url": ORG_URL,
        "open": [_pr(1, "creator")],
        "done": [_pr(2, "creator", "abc")],
        "pipelines": [{**_build(9, "abc", "inProgress", None), "__pr": {"title": "PR 2", "url": "u"}}],
        "open_error": None, "done_error": None, "pipelines_error": None,
    }
    cfg: ado.AdoConfig = {
        "enabled": True, "org": "acme", "projects": ["Proj A"], "pipeline_branches": ["main"],
    }
    ctx = SourceContext(config=cfg, now=NOW, cutoff=NOW - timedelta(days=3), hide_handled=True)
    sections = ado.source.sections(raw, ctx)
    assert [s.title for s in sections] == [
        "Azure DevOps", "Azure DevOps merged", "ADO pipelines not green (main)",
    ]
    assert sections[2].rows[0].meta == "running" and sections[2].summary_label == "pipelines not green"


def test_not_green_truth_table() -> None:
    assert ado.build_not_green({"status": "completed", "result": "succeeded"}) is False
    assert ado.build_not_green({"status": "completed", "result": "failed"}) is True
    assert ado.build_not_green({"status": "inProgress", "result": None}) is True
