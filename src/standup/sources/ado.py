from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict
from urllib.parse import quote

import typer

from standup.log import dbg, timed
from standup.model import MergedCommit, Row, Section, parse_iso, short_name
from standup.shell import FetchError, run
from standup.sources.base import SourceContext

ADO_PIPELINE_TOP = 50

_ADO_ROLE_STYLES = {"reviewer": "cyan", "creator": "magenta"}


def _ado_list_prs(
    email: str, project: str, filter_flag: str, role: str, status: str, org_url: str
) -> list[dict[str, Any]]:
    out = run([
        "az", "repos", "pr", "list",
        "--org", org_url,
        "--project", project,
        filter_flag, email,
        "--status", status,
        "-o", "json",
    ], timeout=60.0)
    items: list[dict[str, Any]] = json.loads(out) if out.strip() else []
    for it in items:
        it["__role"] = role
    return items


def _ado_fan_out(status: str, org_url: str, projects: list[str]) -> list[dict[str, Any]]:
    if not projects:
        raise FetchError("set org and projects in config (standup config init)")
    email = run(["az", "account", "show", "--query", "user.name", "-o", "tsv"]).strip()
    if not email:
        raise FetchError("az account show returned empty user.name (run `az login`)")
    all_prs: list[dict[str, Any]] = []
    with timed(f"ado: list '{status}' PRs across {len(projects)} project(s) x 2 roles"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(projects) * 2) as ex:
            futures = []
            for project in projects:
                futures.append(
                    ex.submit(_ado_list_prs, email, project, "--reviewer", "reviewer", status, org_url)
                )
                futures.append(
                    ex.submit(_ado_list_prs, email, project, "--creator", "creator", status, org_url)
                )
            for fut in futures:
                all_prs.extend(fut.result())
    dbg(f"ado: '{status}' fan-out returned {len(all_prs)} PR row(s) (pre-dedup)")
    return all_prs


_ADO_REST_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"  # ADO resource id for az rest --resource

# ADO reviewer votes: 10 approved, 5 approved with suggestions, 0 none, -5 waiting, -10 rejected
_ADO_VOTE_APPROVED_WITH_SUGGESTIONS = 5


def _ado_pr_last_activity(pr: dict[str, Any], org_url: str) -> dict[str, Any] | None:
    """Return {at, by} for the freshest thread on the PR, or None.

    Threads cover comments AND system events (status changes, push refs); we
    take the latest by `lastUpdatedDate` and grab the most recent non-system
    commenter on that thread as the `by`.
    """
    repository = pr.get("repository")
    repo = (repository or {}).get("name") if isinstance(repository, dict) else repository
    project = (
        ((repository or {}).get("project") or {}).get("name")
        if isinstance(repository, dict)
        else pr.get("project")
    )
    pr_id = pr.get("pullRequestId")
    if not (repo and project and pr_id):
        return None
    url = (
        f"{org_url}/{quote(project)}/_apis/git/repositories/{quote(repo)}"
        f"/pullRequests/{pr_id}/threads?api-version=7.1"
    )
    try:
        out = run(["az", "rest", "-u", url, "--resource", _ADO_REST_RESOURCE, "-o", "json"], timeout=30.0)
        data: dict[str, Any] = json.loads(out) if out.strip() else {}
    except Exception:
        return None
    threads = data.get("value") or []
    if not threads:
        return None
    threads = sorted(
        [t for t in threads if not t.get("isDeleted") and t.get("lastUpdatedDate")],
        key=lambda t: t["lastUpdatedDate"],
        reverse=True,
    )
    if not threads:
        return None
    newest = threads[0]
    by = None
    for c in reversed(newest.get("comments") or []):
        if c.get("commentType") in ("text", "codeChange"):
            by = (c.get("author") or {}).get("displayName")
            if by:
                break
    return {"at": newest["lastUpdatedDate"], "by": by}


def _ado_i_approved(pr: dict[str, Any], email: str) -> bool:
    for rv in pr.get("reviewers") or []:
        vote_ok = (rv.get("vote") or 0) >= _ADO_VOTE_APPROVED_WITH_SUGGESTIONS
        if (rv.get("uniqueName") or "").lower() == email.lower() and vote_ok:
            return True
    return False


def fetch_ado_open(
    hide_handled: bool, org_url: str, projects: list[str]
) -> tuple[list[dict[str, Any]], str | None]:
    try:
        prs = _ado_fan_out("active", org_url, projects)
        if hide_handled and prs:
            email = run(["az", "account", "show", "--query", "user.name", "-o", "tsv"]).strip()
            before = len(prs)
            prs = [
                pr for pr in prs
                if not (pr.get("__role") == "reviewer" and _ado_i_approved(pr, email))
            ]
            dbg(f"ado: dropped {before - len(prs)} approved reviewer PR(s)")
        if prs:
            with timed(f"ado: enrich {len(prs)} PR(s) with latest thread activity"):
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(prs), 8)) as ex:
                    activities = ex.map(lambda p: _ado_pr_last_activity(p, org_url), prs)
                    for pr, last_activity in zip(prs, activities, strict=True):
                        if last_activity:
                            pr["__last_activity"] = last_activity
        return prs, None
    except Exception as e:
        dbg(f"ado open: ERROR {type(e).__name__}: {e}")
        return [], str(e)


def fetch_ado_done(
    cutoff: datetime, org_url: str, projects: list[str]
) -> tuple[list[dict[str, Any]], str | None]:
    """All completed PRs across the configured projects; filter to merged-after-cutoff in-process.

    `az repos pr list` doesn't accept a date filter, so we ask for everything
    Completed and drop anything older client-side. The active set is the only
    thing kept in memory long-term; this is bounded by how much you've shipped.
    """
    try:
        items = _ado_fan_out("completed", org_url, projects)
        kept: list[dict[str, Any]] = []
        for pr in items:
            ts = parse_iso(pr.get("closedDate") or pr.get("completionQueueTime") or pr.get("creationDate"))
            if ts is None:
                continue
            if ts >= cutoff:
                kept.append(pr)
        dbg(f"ado done: kept {len(kept)}/{len(items)} completed PR(s) after cutoff")
        return kept, None
    except Exception as e:
        dbg(f"ado done: ERROR {type(e).__name__}: {e}")
        return [], str(e)


def _ado_list_runs(project: str, branch: str, org_url: str) -> list[dict[str, Any]]:
    out = run([
        "az", "pipelines", "runs", "list",
        "--org", org_url,
        "--project", project,
        "--branch", branch,
        "--top", str(ADO_PIPELINE_TOP),
        "-o", "json",
    ], timeout=60.0)
    items: list[dict[str, Any]] = json.loads(out) if out.strip() else []
    for it in items:
        it["__project"] = project
    return items


def build_not_green(b: dict[str, Any]) -> bool:
    if (b.get("status") or "") != "completed":
        return True
    return (b.get("result") or "") != "succeeded"


def fetch_ado_pipelines(
    sha_to_pr: dict[str, MergedCommit],
    cutoff: datetime,
    org_url: str,
    projects: list[str],
    branches: list[str],
) -> tuple[list[dict[str, Any]], str | None]:
    """Not-green pipeline runs on the given branches built from one of my merge commits.

    Matching is by sourceVersion only, trigger-agnostic, so manually queued
    runs of a merged commit count too. Batched CI runs report only the newest
    commit, so older merges folded into a batch go unattributed (accepted).
    """
    if not projects:
        return [], "set org and projects in config (standup config init)"
    if not sha_to_pr:
        return [], None
    try:
        combos = [(p, b) for p in projects for b in branches]
        runs: list[dict[str, Any]] = []
        with timed(f"ado: list pipeline runs across {len(combos)} (project x branch) combo(s)"):
            with concurrent.futures.ThreadPoolExecutor(max_workers=len(combos)) as ex:
                for chunk in ex.map(lambda pb: _ado_list_runs(pb[0], pb[1], org_url), combos):
                    runs.extend(chunk)
        seen: set[Any] = set()
        kept: list[dict[str, Any]] = []
        for b in runs:
            bid = b.get("id")
            if bid in seen:
                continue
            seen.add(bid)
            pr = sha_to_pr.get((b.get("sourceVersion") or "").lower())
            if not pr:
                continue
            qt = parse_iso(b.get("queueTime"))
            if qt is None or qt < cutoff:
                continue
            if not build_not_green(b):
                continue
            b["__pr"] = pr
            kept.append(b)
        dbg(f"ado pipelines: kept {len(kept)}/{len(runs)} run(s)")
        return kept, None
    except Exception as e:
        dbg(f"ado pipelines: ERROR {type(e).__name__}: {e}")
        return [], str(e)


def pr_url(pr: dict[str, Any], org_url: str) -> str:
    repository = pr.get("repository")
    if isinstance(repository, dict):
        repo = repository.get("name") or "?"
        project = (repository.get("project") or {}).get("name") or ""
    else:
        repo = repository or "?"
        project = pr.get("project") or ""
    return f"{org_url}/{quote(project)}/_git/{quote(repo)}/pullrequest/{pr.get('pullRequestId')}"


def _ado_collect(
    prs: list[dict[str, Any]],
    time_picker: Callable[[dict[str, Any]], datetime | None],
    org_url: str,
) -> list[Row]:
    by_id: dict[int, Row] = {}
    for pr in prs:
        pr_id = pr.get("pullRequestId")
        if pr_id is None:
            continue
        repository = pr.get("repository")
        repo = repository.get("name") or "?" if isinstance(repository, dict) else repository or "?"
        role = pr.get("__role") or "reviewer"
        existing = by_id.get(pr_id)
        if existing:
            if role not in existing.meta:
                existing.meta = f"{existing.meta}+{role}"
            continue
        last = pr.get("__last_activity") or {}
        by_id[pr_id] = Row(
            key=f"{repo} !{pr_id}",
            title=pr.get("title") or "",
            meta=role,
            meta_style=_ADO_ROLE_STYLES.get(role, "cyan"),
            updated=time_picker(pr),
            url=pr_url(pr, org_url),
            activity_at=parse_iso(last.get("at")),
            activity_by=short_name(last.get("by")),
        )
    rows = list(by_id.values())
    rows.sort(
        key=lambda r: r.activity_at or r.updated or datetime.min.replace(tzinfo=UTC),
        reverse=True,
    )
    return rows


def ado_open_rows(prs: list[dict[str, Any]], org_url: str) -> list[Row]:
    return _ado_collect(prs, lambda pr: parse_iso(pr.get("creationDate")), org_url)


def ado_done_rows(prs: list[dict[str, Any]], org_url: str) -> list[Row]:
    return _ado_collect(
        prs,
        lambda pr: parse_iso(pr.get("closedDate") or pr.get("completionQueueTime") or pr.get("creationDate")),
        org_url,
    )


_ADO_BUILD_STATE_STYLES = {
    "running": "yellow",
    "queued": "bright_black",
    "failed": "bold red",
    "canceled": "bright_black",
    "partial": "red",
}


def _build_state(b: dict[str, Any]) -> str:
    """Collapse ADO build status/result into one standup-relevant label."""
    status = (b.get("status") or "").lower()
    if status in ("notstarted", "postponed", "none"):
        return "queued"
    if status != "completed":
        return "running"
    result = (b.get("result") or "").lower()
    if result == "canceled":
        return "canceled"
    if result == "partiallysucceeded":
        return "partial"
    return "failed"


# CI-triggered builds report this identity; collapsed to "auto" in the Activity column.
_ADO_SYSTEM_REQUESTER = "Microsoft.VisualStudio.Services.TFS"


def ado_pipeline_rows(builds: list[dict[str, Any]], org_url: str) -> list[Row]:
    rows: list[Row] = []
    for b in builds:
        definition = (b.get("definition") or {}).get("name") or "?"
        project = b.get("__project") or (
            (b.get("project") or {}).get("name") if isinstance(b.get("project"), dict) else ""
        ) or ""
        pr = b.get("__pr") or {}
        state = _build_state(b)
        requester = (b.get("requestedFor") or {}).get("displayName")
        if requester == _ADO_SYSTEM_REQUESTER:
            requester = "auto"
        rows.append(Row(
            key=f"{definition} #{b.get('buildNumber') or b.get('id')}",
            title=pr.get("title") or (b.get("sourceVersion") or "")[:8],
            meta=state,
            meta_style=_ADO_BUILD_STATE_STYLES.get(state, "white"),
            updated=parse_iso(b.get("finishTime") or b.get("startTime") or b.get("queueTime")),
            url=f"{org_url}/{quote(project)}/_build/results?buildId={b.get('id')}",
            activity_by=short_name(requester),
        ))
    rows.sort(key=lambda r: r.updated or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


class AdoConfig(TypedDict):
    enabled: bool
    org: str
    projects: list[str]
    pipeline_branches: list[str]


def org_url(org: str) -> str:
    return f"https://dev.azure.com/{org}"


def _csv(text: str) -> list[str]:
    return [p.strip() for p in text.split(",") if p.strip()]


class AdoSource:
    name = "ado"

    def defaults(self) -> AdoConfig:
        return {"enabled": True, "org": "", "projects": [], "pipeline_branches": ["main", "master"]}

    def configure(self, current: AdoConfig) -> AdoConfig:
        org = typer.prompt(
            "Azure DevOps organisation (the <org> in dev.azure.com/<org>)",
            default=current["org"] or None,
        )
        projects = typer.prompt(
            "Projects to scan, comma-separated",
            default=", ".join(current["projects"]) or None,
        )
        branches = typer.prompt(
            "Pipeline branches to watch, comma-separated",
            default=", ".join(current["pipeline_branches"]),
        )
        return {
            "enabled": True,
            "org": org.strip(),
            "projects": _csv(projects),
            "pipeline_branches": _csv(branches),
        }

    def commands(self) -> typer.Typer | None:
        return None

    def fetch(self, ctx: SourceContext[AdoConfig]) -> dict[str, Any]:
        url = org_url(ctx.config["org"])
        projects = ctx.config["projects"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            open_future = ex.submit(fetch_ado_open, ctx.hide_handled, url, projects)
            done_future = ex.submit(fetch_ado_done, ctx.cutoff, url, projects) if ctx.cutoff else None
            open_raw, open_error = open_future.result()
            done_raw, done_error = done_future.result() if done_future else ([], None)
        return {
            "org_url": url,
            "open": open_raw, "done": done_raw, "pipelines": [],
            "open_error": open_error, "done_error": done_error, "pipelines_error": None,
        }

    def merged_commits(self, raw: dict[str, Any]) -> dict[str, MergedCommit]:
        url = raw.get("org_url") or ""
        out: dict[str, MergedCommit] = {}
        for pr in raw.get("done") or []:
            sha = (pr.get("lastMergeCommit") or {}).get("commitId")
            if sha:
                out.setdefault(sha.lower(), {"title": pr.get("title") or "", "url": pr_url(pr, url)})
        return out

    def fetch_dependent(
        self, ctx: SourceContext[AdoConfig], raw: dict[str, Any], merged: dict[str, MergedCommit]
    ) -> dict[str, Any]:
        if ctx.cutoff is None:
            return raw
        runs, error = fetch_ado_pipelines(
            merged, ctx.cutoff, raw["org_url"], ctx.config["projects"], ctx.config["pipeline_branches"]
        )
        return {**raw, "pipelines": runs, "pipelines_error": error}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[AdoConfig]) -> list[Section]:
        url = raw.get("org_url") or org_url(ctx.config["org"])
        open_error = raw.get("open_error")
        result = [
            Section(
                title="Azure DevOps", color="blue", meta_header="Role",
                rows=ado_open_rows(raw.get("open") or [], url) if not open_error else [],
                error=open_error, summary_label="Azure DevOps PRs",
            )
        ]
        if ctx.cutoff is None:
            return result
        done_error = raw.get("done_error")
        pipelines_error = raw.get("pipelines_error")
        branches = "/".join(ctx.config["pipeline_branches"])
        result.append(
            Section(
                title="Azure DevOps merged", color="blue", meta_header="Role",
                rows=ado_done_rows(raw.get("done") or [], url) if not done_error else [],
                error=done_error, done=True, summary_label="Azure DevOps",
            )
        )
        result.append(
            Section(
                title=f"ADO pipelines not green ({branches})", color="blue", meta_header="State",
                rows=ado_pipeline_rows(raw.get("pipelines") or [], url) if not pipelines_error else [],
                error=pipelines_error, done=True, summary_label="pipelines not green",
            )
        )
        return result


source = AdoSource()
