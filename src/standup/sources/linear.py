from __future__ import annotations

import asyncio
import http.server
import json
import os
import socket
import sys
import threading
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypedDict
from urllib.parse import parse_qs, urlparse

import typer
from mcp import ClientSession
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamablehttp_client
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
from mcp.types import CallToolResult

from standup.config import state_dir
from standup.log import dbg, timed
from standup.model import Row, Section, parse_iso, short_name
from standup.sources.base import SourceContext

LINEAR_MCP_URL = "https://mcp.linear.app/mcp"
LINEAR_PAGE_SIZE = 50


def token_path() -> Path:
    return state_dir() / "linear_oauth.json"


class FileTokenStorage(TokenStorage):
    """OAuth token + dynamic-client-registration cache, persisted to one JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text())
            except Exception:
                self._data = {}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2))
        os.chmod(self.path, 0o600)

    async def get_tokens(self) -> OAuthToken | None:
        t = self._data.get("tokens")
        return OAuthToken.model_validate(t) if t else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._save()

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        c = self._data.get("client")
        if not c:
            return None
        # Linear's token endpoint rejects HTTP Basic auth when client_id is
        # also in the body ("multiple authentication methods"), and the mcp
        # SDK always puts client_id in the body. Force secret-in-body instead.
        if c.get("token_endpoint_auth_method") == "client_secret_basic":
            c["token_endpoint_auth_method"] = "client_secret_post"
        return OAuthClientInformationFull.model_validate(c)

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self._data["client"] = client_info.model_dump(mode="json", exclude_none=True)
        self._save()


def _pick_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class _OAuthCallbackServer:
    """One-shot HTTP server that captures Linear's OAuth redirect on localhost.

    Started by the redirect_handler, queried by the callback_handler.
    """

    def __init__(self, port: int) -> None:
        self.port = port
        self.event = threading.Event()
        self.code: str | None = None
        self.state: str | None = None
        self.error: str | None = None
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_a: Any, **_k: Any) -> None:
                return

            def do_GET(self) -> None:
                qs = parse_qs(urlparse(self.path).query)
                if "error" in qs:
                    outer.error = qs.get("error_description", qs["error"])[0]
                    body = b"Linear OAuth failed. Check terminal."
                else:
                    outer.code = qs.get("code", [None])[0]
                    outer.state = qs.get("state", [None])[0]
                    body = b"Linear OAuth complete. You can close this tab."
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                outer.event.set()

        self._server = http.server.HTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()


async def _linear_session() -> tuple[Any, _OAuthCallbackServer]:
    port = _pick_free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    callback_server = _OAuthCallbackServer(port)

    async def redirect_handler(authorization_url: str) -> None:
        callback_server.start()
        dbg("linear: no valid cached token, starting OAuth browser consent flow")
        print(f"Linear OAuth: opening {authorization_url}", file=sys.stderr)
        try:
            webbrowser.open(authorization_url)
        except Exception:
            print("  (couldn't open browser, paste this URL into one)", file=sys.stderr)

    async def callback_handler() -> tuple[str, str | None]:
        dbg("linear: waiting for OAuth redirect (up to 5 min, complete consent in browser)")
        await asyncio.get_running_loop().run_in_executor(None, callback_server.event.wait, 300)
        callback_server.stop()
        if callback_server.error:
            raise RuntimeError(f"Linear OAuth error: {callback_server.error}")
        if not callback_server.code:
            raise RuntimeError("Linear OAuth timed out (no code received within 5 min)")
        return callback_server.code, callback_server.state

    auth = OAuthClientProvider(
        server_url=LINEAR_MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="standup-cli",
            redirect_uris=[redirect_uri],  # type: ignore[list-item]
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            scope="read",
        ),
        storage=FileTokenStorage(token_path()),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    return streamablehttp_client(LINEAR_MCP_URL, auth=auth), callback_server


async def _linear_fetch_comment(session: ClientSession, identifier: str) -> dict[str, Any] | None:
    try:
        result = await session.call_tool(
            "list_comments",
            {"issueId": identifier, "orderBy": "updatedAt", "limit": 1},
        )
    except Exception:
        return None
    for part in result.content:
        text = getattr(part, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        comments = payload.get("comments") or []
        if not comments:
            return None
        c = comments[0]
        user = c.get("user")
        if isinstance(user, dict):
            by = user.get("displayName") or user.get("name")
        else:
            by = user
        return {"at": c.get("updatedAt") or c.get("createdAt"), "by": by}
    return None


async def _linear_fetch_async(enrich_comments: bool = False) -> list[dict[str, Any]]:
    cached = token_path().exists()
    dbg(f"linear: opening MCP transport to {LINEAR_MCP_URL} (cached token: {cached})")
    transport_cm, _ = await _linear_session()
    async with transport_cm as (read, write, _close):
        async with ClientSession(read, write) as session:
            with timed("linear: session.initialize() (handshake + auth)"):
                await session.initialize()
            with timed(f"linear: list_issues (assignee=me, limit={LINEAR_PAGE_SIZE})"):
                result = await session.call_tool(
                    "list_issues",
                    {
                        "assignee": "me",
                        "orderBy": "updatedAt",
                        "limit": LINEAR_PAGE_SIZE,
                        "includeArchived": False,
                    },
                )
            issues = _extract_linear_issues(result)
            dbg(f"linear: list_issues returned {len(issues)} issue(s)")

            # Off by default: one list_comments call per issue is slow for little standup value.
            if not enrich_comments:
                dbg("linear: skipping comment enrichment "
                    "(set linear.comments = true in config or pass -c linear.comments=true)")
                return issues
            ids: list[str] = [it["id"] for it in issues if it.get("id")]
            if ids:
                with timed(f"linear: enrich {len(ids)} issue(s) with latest comment"):
                    results = await asyncio.gather(
                        *[_linear_fetch_comment(session, ident) for ident in ids],
                        return_exceptions=True,
                    )
                for it, res in zip([it for it in issues if it.get("id")], results, strict=True):
                    if isinstance(res, dict) and res.get("at"):
                        it["__last_comment"] = res
            return issues


async def _linear_list_tools_async() -> list[dict[str, Any]]:
    transport_cm, _ = await _linear_session()
    async with transport_cm as (read, write, _close):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return [
                {"name": t.name, "description": (t.description or "")[:120]}
                for t in tools.tools
            ]


def _extract_linear_issues(call_tool_result: CallToolResult) -> list[dict[str, Any]]:
    """The Linear MCP returns its payload as one TextContent JSON blob.

    Normalize to a list of issue dicts with keys our normalizer expects:
    identifier, title, url, updatedAt, state.{name,type}.
    """
    for part in call_tool_result.content:
        text = getattr(part, "text", None)
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            issues = payload.get("issues") or payload.get("nodes") or payload.get("data") or []
        elif isinstance(payload, list):
            issues = payload
        else:
            issues = []
        return list(issues)
    return []


def fetch_linear(enrich_comments: bool = False) -> tuple[list[dict[str, Any]], str | None]:
    try:
        with timed("linear: full fetch (connect + list + enrich)"):
            issues = asyncio.run(_linear_fetch_async(enrich_comments))
        return issues, None
    except Exception as e:
        dbg(f"linear: ERROR {type(e).__name__}: {e}")
        return [], f"{type(e).__name__}: {e}"


_LINEAR_STATE_STYLES = {
    "In Progress": "bold green",
    "In Review": "bold cyan",
    "Todo": "yellow",
    "Backlog": "bright_black",
    "Triage": "magenta",
    "Done": "green",
    "Canceled": "bright_black",
    "Duplicate": "bright_black",
}


def _linear_row(it: dict[str, Any], time_field: str) -> Row:
    state_name = it.get("status") or "?"
    last = it.get("__last_comment") or {}
    return Row(
        key=it.get("id") or it.get("identifier") or "?",
        title=it.get("title") or "",
        meta=state_name,
        meta_style=_LINEAR_STATE_STYLES.get(state_name, "white"),
        updated=parse_iso(it.get(time_field) or it.get("updatedAt")),
        url=it.get("url") or "",
        activity_at=parse_iso(last.get("at")),
        activity_by=short_name(last.get("by")),
    )


def linear_open_rows(issues: list[dict[str, Any]], hide_handled: bool = True) -> list[Row]:
    rows: list[Row] = []
    for it in issues:
        stype = (it.get("statusType") or "").lower()
        if stype in ("completed", "canceled", "cancelled"):
            continue
        if hide_handled and (it.get("status") or "").strip().lower() == "duplicate":
            continue
        rows.append(_linear_row(it, "updatedAt"))
    rows.sort(key=lambda r: r.updated or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


def linear_done_rows(issues: list[dict[str, Any]], cutoff: datetime) -> list[Row]:
    rows: list[Row] = []
    for it in issues:
        stype = (it.get("statusType") or "").lower()
        if stype not in ("completed", "canceled", "cancelled"):
            continue
        completed_at = parse_iso(it.get("completedAt") or it.get("canceledAt") or it.get("updatedAt"))
        if completed_at is None or completed_at < cutoff:
            continue
        state_name = it.get("status") or "?"
        last = it.get("__last_comment") or {}
        rows.append(Row(
            key=it.get("id") or it.get("identifier") or "?",
            title=it.get("title") or "",
            meta=state_name,
            meta_style=_LINEAR_STATE_STYLES.get(state_name, "white"),
            updated=completed_at,
            url=it.get("url") or "",
            activity_at=parse_iso(last.get("at")),
            activity_by=short_name(last.get("by")),
        ))
    rows.sort(key=lambda r: r.updated or datetime.min.replace(tzinfo=UTC), reverse=True)
    return rows


class LinearConfig(TypedDict):
    enabled: bool
    comments: bool


commands = typer.Typer(help="Linear MCP connection helpers.")


@commands.command()
def tools() -> None:
    """List the tools the Linear MCP server exposes."""
    try:
        listed = asyncio.run(_linear_list_tools_async())
    except Exception as e:
        typer.echo(f"Linear MCP error: {type(e).__name__}: {e}", err=True)
        raise typer.Exit(1) from e
    for t in listed:
        typer.echo(f"{t['name']:40s}  {t['description']}")


@commands.command("reset-auth")
def reset_auth() -> None:
    """Delete the cached OAuth token so the next report re-consents in the browser."""
    path = token_path()
    if path.exists():
        path.unlink()
        typer.echo(f"Deleted {path}", err=True)
    else:
        typer.echo(f"No cached token at {path}", err=True)


class LinearSource:
    name = "linear"

    def defaults(self) -> LinearConfig:
        return {"enabled": True, "comments": False}

    def configure(self, current: LinearConfig) -> LinearConfig:
        comments = typer.confirm(
            "Fetch the latest comment for each Linear issue? (slow, one call per issue)",
            default=current["comments"],
        )
        return {"enabled": True, "comments": comments}

    def commands(self) -> typer.Typer:
        return commands

    def fetch(self, ctx: SourceContext[LinearConfig]) -> dict[str, Any]:
        issues, error = fetch_linear(ctx.config["comments"])
        return {"raw": issues, "error": error}

    def sections(self, raw: dict[str, Any], ctx: SourceContext[LinearConfig]) -> list[Section]:
        error = raw.get("error")
        issues = raw.get("raw") or []
        result = [
            Section(
                title="Linear", color="cyan", meta_header="State",
                rows=linear_open_rows(issues, ctx.hide_handled) if not error else [],
                error=error, summary_label="Linear issues",
            )
        ]
        if ctx.cutoff is not None:
            result.append(
                Section(
                    title="Linear done", color="cyan", meta_header="State",
                    rows=linear_done_rows(issues, ctx.cutoff) if not error else [],
                    done=True, summary_label="Linear",
                )
            )
        return result


source = LinearSource()
