from __future__ import annotations

import concurrent.futures
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, TypedDict

import typer

from standup.log import dbg, timed
from standup.model import MergedCommit, Row, Section, parse_iso, short_name
from standup.shell import run
from standup.sources.base import SourceContext

GITHUB_PER_PAGE = 50

_GH_ROLE_STYLES = {
    "assignee": "magenta",
    "reviewer": "cyan",
    "author": "magenta",
}


def _gh_search_raw(q: str) -> dict[str, Any]:
    out = run([
        "gh", "api", "-X", "GET", "search/issues",
        "-f", f"q={q}",
        "-f", "sort=updated",
        "-f", "order=desc",
        "-f", f"per_page={GITHUB_PER_PAGE}",
    ])
    return json.loads(out)  # type: ignore[no-any-return]


def gh_search(q: str, orgs: list[str]) -> dict[str, Any]:
    """Search PRs, scoped to orgs.

    GitHub ANDs repeated `org:` qualifiers, so multiple orgs mean one search
    per org, merged (deduped by html_url). Negated owner qualifiers are NOT
    used: `-user:x` silently drops private-org results from the search API.
    """
    if not orgs:
        return _gh_search_raw(q)
    merged: dict[str, Any] = {"total_count": 0, "incomplete_results": False, "items": []}
    seen: set[str] = set()
    for org in orgs:
        res = _gh_search_raw(f"{q} org:{org}")
        merged["total_count"] += res.get("total_count") or 0
        merged["incomplete_results"] = merged["incomplete_results"] or bool(res.get("incomplete_results"))
        for it in res.get("items") or []:
            key = it.get("html_url") or f"{it.get('repository_url')}#{it.get('number')}"
            if key in seen:
                continue
            seen.add(key)
            merged["items"].append(it)
    return merged


def _gh_pr_last_comment(repo_url: str, number: int) -> dict[str, Any] | None:
    """Return {at, by} for the latest *human* comment on the PR, or None.

    Bot integrations (linear-app, dependabot, etc.) get filtered out - they're
    noise when triaging what to look at. We fetch a small page and pick the
    newest non-bot. Falls back to the newest bot comment if that's all there is.
    """
    owner_repo = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else None
    if not owner_repo:
        return None
    try:
        out = run([
            "gh", "api",
            "-X", "GET",
            f"/repos/{owner_repo}/issues/{number}/comments",
            "-f", "sort=created",
            "-f", "direction=desc",
            "-f", "per_page=30",
        ], timeout=15.0)
        items = json.loads(out) if out.strip() else []
    except Exception:
        return None
    if not items:
        return None

    def is_bot(c: dict[str, Any]) -> bool:
        user = c.get("user") or {}
        if user.get("type") == "Bot":
            return True
        login = (user.get("login") or "").lower()
        return login.endswith("[bot]") or login.endswith("-bot")

    human = next((c for c in items if not is_bot(c)), None)
    chosen = human or items[0]
    return {"at": chosen.get("created_at"), "by": (chosen.get("user") or {}).get("login")}


def _gh_me_login() -> str | None:
    try:
        return run(["gh", "api", "user", "--jq", ".login"], timeout=10.0).strip() or None
    except Exception:
        return None


def _gh_pr_i_approved(repo_url: str, number: int, me: str) -> bool:
    """True if my *latest* review on the PR is APPROVED.

    Once approved, a PR I was asked to review isn't something I need to attend
    to - drop it from the reviewer bucket. Uses my most recent review state, so
    a later COMMENTED / CHANGES_REQUESTED keeps the PR visible again.
    """
    owner_repo = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else None
    if not owner_repo or not me:
        return False
    try:
        out = run([
            "gh", "api", "-X", "GET",
            f"/repos/{owner_repo}/pulls/{number}/reviews",
            "-f", "per_page=100",
        ], timeout=15.0)
        reviews = json.loads(out) if out.strip() else []
    except Exception:
        return False
    mine = [r for r in reviews if ((r.get("user") or {}).get("login") or "").lower() == me.lower()]
    return bool(mine) and mine[-1].get("state") == "APPROVED"


def _gh_pr_merge_sha(repo_url: str, number: int) -> str | None:
    """merge_commit_sha for a PR, or None (unmerged / fetch failure).

    The search payload doesn't carry it, so each done PR costs one API call.
    """
    owner_repo = "/".join(repo_url.rstrip("/").split("/")[-2:]) if repo_url else None
    if not owner_repo or not number:
        return None
    try:
        out = run([
            "gh", "api", f"/repos/{owner_repo}/pulls/{number}",
            "--jq", ".merge_commit_sha // empty",
        ], timeout=15.0)
    except Exception:
        return None
    return out.strip() or None


def _gh_enrich_merge_shas(payload: dict[str, Any]) -> None:
    targets: dict[str, dict[str, Any]] = {}
    for bucket in payload.values():
        for it in (bucket.get("items") if isinstance(bucket, dict) else None) or []:
            key = it.get("html_url") or f"{it.get('repository_url')}#{it.get('number')}"
            if key and key not in targets:
                targets[key] = it
    if not targets:
        return
    with timed(f"github: enrich {len(targets)} done PR(s) with merge SHA"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(targets), 12)) as ex:
            for it, sha in zip(
                targets.values(),
                ex.map(lambda i: _gh_pr_merge_sha(i.get("repository_url") or "", i.get("number") or 0),
                       targets.values()),
                strict=True,
            ):
                if sha:
                    it["__merge_sha"] = sha


def _gh_drop_approved_reviews(payload: dict[str, Any]) -> None:
    """In-place: drop PRs I've already approved from the assignee/reviewer buckets.

    An approved PR isn't something I need to attend to - whether it landed in my
    list via a review request OR an assignment. The `author` bucket is left
    alone: those are my own PRs, approval doesn't make them go away.
    """
    buckets = [payload.get("assignee"), payload.get("reviewer")]
    targets: dict[str, dict[str, Any]] = {}
    for bucket in buckets:
        for it in (bucket.get("items") if isinstance(bucket, dict) else None) or []:
            key = it.get("html_url") or f"{it.get('repository_url')}#{it.get('number')}"
            targets.setdefault(key, it)
    if not targets:
        return
    dbg(f"github: checking approval state on {len(targets)} PR(s) (assignee/reviewer)")
    me = _gh_me_login()
    if not me:
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(targets), 12)) as ex:
        approved = {
            key
            for key, ok in zip(
                targets.keys(),
                ex.map(
                    lambda it: _gh_pr_i_approved(it.get("repository_url") or "", it.get("number") or 0, me),
                    targets.values(),
                ),
                strict=True,
            )
            if ok
        }

    def survives(it: dict[str, Any]) -> bool:
        key = it.get("html_url") or f"{it.get('repository_url')}#{it.get('number')}"
        return key not in approved

    dbg(f"github: dropping {len(approved)} already-approved PR(s) from open buckets")
    for bucket in buckets:
        if isinstance(bucket, dict):
            kept = [it for it in (bucket.get("items") or []) if survives(it)]
            bucket["items"] = kept
            if "total_count" in bucket:
                bucket["total_count"] = len(kept)


def _enrich_github_payload(payload: dict[str, Any]) -> None:
    targets: dict[str, dict[str, Any]] = {}
    for bucket in payload.values():
        for it in (bucket.get("items") if isinstance(bucket, dict) else None) or []:
            key = it.get("html_url") or f"{it.get('repository_url')}#{it.get('number')}"
            if key and key not in targets:
                targets[key] = it

    if not targets:
        return

    def task(it: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return it, _gh_pr_last_comment(it.get("repository_url") or "", it.get("number") or 0)

    with timed(f"github: enrich {len(targets)} PR(s) with latest comment"):
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(targets), 12)) as ex:
            for it, result in ex.map(lambda i: task(i), targets.values()):
                if result and result.get("at"):
                    it["__last_comment"] = result


def fetch_github_open(hide_handled: bool, orgs: list[str]) -> tuple[dict[str, Any], str | None]:
    try:
        with timed("github: open PR search (author/assignee/reviewer)"):
            payload = {
                "author": gh_search("is:open is:pr archived:false author:@me", orgs),
                "assignee": gh_search("is:open is:pr archived:false assignee:@me", orgs),
                "reviewer": gh_search("is:open is:pr archived:false review-requested:@me", orgs),
            }
        if hide_handled:
            with timed("github: drop already-approved reviews"):
                _gh_drop_approved_reviews(payload)
        _enrich_github_payload(payload)
        return payload, None
    except Exception as e:
        dbg(f"github open: ERROR {type(e).__name__}: {e}")
        return {}, str(e)


def fetch_github_done(cutoff: datetime, orgs: list[str]) -> tuple[dict[str, Any], str | None]:
    cutoff_str = cutoff.strftime("%Y-%m-%d")
    try:
        with timed(f"github: done PR search (merged >= {cutoff_str})"):
            payload = {
                "author": gh_search(f"is:pr is:merged author:@me merged:>={cutoff_str}", orgs),
                "reviewer": gh_search(f"is:pr is:merged reviewed-by:@me merged:>={cutoff_str}", orgs),
            }
        _enrich_github_payload(payload)
        _gh_enrich_merge_shas(payload)
        return payload, None
    except Exception as e:
        dbg(f"github done: ERROR {type(e).__name__}: {e}")
        return {}, str(e)


def _gh_owner_allowed(repo_url: str, orgs: list[str]) -> bool:
    if not orgs:
        return True
    parts = repo_url.rstrip("/").split("/")
    owner = parts[-2].lower() if len(parts) >= 2 else ""
    return owner in {o.lower() for o in orgs}


def _gh_collect(
    payload: dict[str, Any],
    bucket_roles: list[tuple[str, str]],
    time_picker: Callable[[dict[str, Any]], datetime | None],
    orgs: list[str],
) -> list[Row]:
    by_url: dict[str, Row] = {}

    def absorb(items: list[dict[str, Any]], role: str) -> None:
        for it in items or []:
            url = it.get("html_url") or ""
            repo_url = it.get("repository_url") or ""
            if not _gh_owner_allowed(repo_url, orgs):
                continue
            repo = repo_url.rsplit("/", 1)[-1] if repo_url else "?"
            key = f"{repo}#{it.get('number')}"
            existing = by_url.get(url)
            if existing:
                if role not in existing.meta:
                    existing.meta = f"{existing.meta}+{role}"
                continue
            last = it.get("__last_comment") or {}
            by_url[url] = Row(
                key=key,
                title=it.get("title") or "",
                meta=role,
                meta_style=_GH_ROLE_STYLES.get(role, "white"),
                updated=time_picker(it),
                url=url,
                activity_at=parse_iso(last.get("at")),
                activity_by=short_name(last.get("by")),
            )

    for bucket, role in bucket_roles:
        absorb((payload.get(bucket) or {}).get("items") or [], role)
    rows = list(by_url.values())
    rows.sort(key=lambda r: r.updated or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


def github_open_rows(payload: dict[str, Any], orgs: list[str]) -> list[Row]:
    return _gh_collect(
        payload,
        [("author", "author"), ("assignee", "assignee"), ("reviewer", "reviewer")],
        lambda it: parse_iso(it.get("updated_at")),
        orgs,
    )


def github_done_rows(payload: dict[str, Any], orgs: list[str]) -> list[Row]:
    def merged_or_closed(it: dict[str, Any]) -> datetime | None:
        pr = it.get("pull_request") or {}
        return parse_iso(pr.get("merged_at") or it.get("closed_at") or it.get("updated_at"))

    return _gh_collect(
        payload,
        [("author", "author"), ("reviewer", "reviewer")],
        merged_or_closed,
        orgs,
    )


class GithubConfig(TypedDict):
    enabled: bool
    orgs: list[str]


def _merge_shas(payload: dict[str, Any]) -> dict[str, MergedCommit]:
    out: dict[str, MergedCommit] = {}
    for bucket in payload.values():
        for it in (bucket.get("items") if isinstance(bucket, dict) else None) or []:
            sha = it.get("__merge_sha")
            if sha:
                out.setdefault(sha.lower(), {"title": it.get("title") or "", "url": it.get("html_url") or ""})
    return out


class GithubSource:
    name = "github"

    def defaults(self) -> GithubConfig:
        return {"enabled": True, "orgs": []}

    def configure(self, current: GithubConfig) -> GithubConfig:
        text = typer.prompt(
            "GitHub owner allowlist, comma-separated (empty = every repo you can see)",
            default=", ".join(current["orgs"]),
            show_default=bool(current["orgs"]),
        )
        return {"enabled": True, "orgs": [o.strip() for o in text.split(",") if o.strip()]}

    def commands(self) -> typer.Typer | None:
        return None

    def fetch(self, ctx: SourceContext[GithubConfig]) -> dict[str, Any]:
        orgs = ctx.config["orgs"]
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
            open_future = ex.submit(fetch_github_open, ctx.hide_handled, orgs)
            done_future = ex.submit(fetch_github_done, ctx.cutoff, orgs) if ctx.cutoff else None
            open_raw, open_error = open_future.result()
            done_raw, done_error = done_future.result() if done_future else ({}, None)
        return {"open": open_raw, "done": done_raw, "open_error": open_error, "done_error": done_error}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[GithubConfig]) -> list[Section]:
        orgs = ctx.config["orgs"]
        open_error = raw.get("open_error")
        result = [
            Section(
                title="GitHub", color="magenta", meta_header="Role",
                rows=github_open_rows(raw.get("open") or {}, orgs) if not open_error else [],
                error=open_error, summary_label="GitHub PRs",
            )
        ]
        if ctx.cutoff is not None:
            done_error = raw.get("done_error")
            result.append(
                Section(
                    title="GitHub merged", color="magenta", meta_header="Role",
                    rows=github_done_rows(raw.get("done") or {}, orgs) if not done_error else [],
                    error=done_error, done=True, summary_label="GitHub",
                )
            )
        return result

    def merged_commits(self, raw: dict[str, Any]) -> dict[str, MergedCommit]:
        return _merge_shas(raw.get("done") or {})


source = GithubSource()
