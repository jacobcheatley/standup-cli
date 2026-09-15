# standup-cli

One command that lists the PRs waiting on you, the issues assigned to you,
the pipelines that went red on your merges, and your own reminders. Rich
tables when you run it in a terminal, markdown when you pipe it somewhere.

## Install

```bash
uv tool install git+https://github.com/jacobcheatley/standup-cli
standup
```

The first run launches a setup wizard (org names, project lists, which
sources to enable) and writes `~/.config/standup/config.toml`.

If an older `standup` script already lives in `~/.local/bin`, move it aside
first so the new one can take its place:

```bash
mv ~/.local/bin/standup ~/.local/bin/standup.old
```

## Sources and their auth

Each source uses whatever you already have logged in; standup-cli never
stores a credential of its own.

- **GitHub**: `gh auth login`. Reads via `gh api search/issues`.
- **Azure DevOps**: `az login`, then `az extension add --name azure-devops`.
  Reads via `az repos pr list` and `az pipelines runs list`.
- **Linear**: browser OAuth on first use (opens automatically). The token is
  cached at `~/.local/share/standup/linear_oauth.json` and refreshed
  automatically; `standup linear reset-auth` deletes it if you need to
  re-consent.
- **Reminders**: plain JSON files under `~/.local/share/standup/reminders/`,
  no auth needed.

## Config

Path: `$XDG_CONFIG_HOME/standup/config.toml`, default
`~/.config/standup/config.toml`.

```toml
[report]
max_age_days = 30
recent_done_days = 3

[update]
check = true

[sources.github]
enabled = true
orgs = []                 # allowlist of repo owners; [] = no filter

[sources.ado]
enabled = true
org = "myorg"             # https://dev.azure.com/<org>
projects = ["Project A"]
pipeline_branches = ["main", "master"]

[sources.linear]
enabled = true
comments = false          # enrich issues with latest comment (slow)

[sources.reminders]
enabled = true
```

- `standup config` prints the config path and its contents.
- `standup config init` re-runs the wizard, pre-filled from the current file.
- `standup config edit` opens the file in `$EDITOR` (falls back to `vi`).

### `-c KEY=VALUE` overrides

`-c`/`--config` patches the loaded config for one run only; it is repeatable.
KEY is a dotted path (a source name is short for `sources.<name>`), VALUE is
parsed as a TOML value.

```bash
standup -c linear.comments=true
standup -c github.orgs=[]
standup -c ado.projects='["Only This"]'
```

## Commands

```
standup [OPTIONS]                 # the report
  --markdown                      Force markdown output
  --rich                          Force rich (ANSI) output
  --max-age-days N                Hide open items not touched in the last N days (0 = no filter)
  --all                           No age filter on open items
  --include-handled                Keep PRs you already approved and duplicate Linear issues
  --recent-done-days N            Also list items merged/done in the last N days (0 = off)
  -c, --config KEY=VALUE          Override a config value for this run, repeatable
  --envelope PATH                 Render this saved envelope instead of fetching
  --save-envelope PATH            Write the fetched envelope here
  -v, --verbose                   Timestamped stage logs on stderr
  --version

standup config                    Show the config path and contents
standup config init                Interactive setup wizard
standup config edit                Open the config file in $EDITOR
standup update                    Upgrade to the latest commit on main

standup reminders add TITLE [--due ISO] [--url URL] [--note TEXT] [--no-session]
standup reminders done ID
standup reminders list [--all]
standup linear tools               List the tools the Linear MCP server exposes
standup linear reset-auth          Delete the cached Linear OAuth token
```

`--markdown` and `--rich` are mutually exclusive, as are `--all` and
`--max-age-days`.

## Reminders and the Claude Code skill

Reminders are a lightweight personal TODO list, separate from any issue
tracker, that surfaces at the top of the report. Manage them directly with
`standup reminders add|done|list`, or hand that job to Claude Code: this repo
ships a skill at `skills/reminder/` that teaches Claude when and how to use
those commands.

From a clone:

```bash
ln -s "$(pwd)/skills/reminder" ~/.claude/skills/reminder
```

or just copy the `skills/reminder` folder into `~/.claude/skills/`.

## Click-to-copy note lines

In the rich report, each reminder's note line (typically a `claude --resume`
command) is rendered as a `copy:` hyperlink rather than plain text. On its
own, clicking it does nothing special. Install
[copy-uri](https://github.com/jacobcheatley/copy-uri) to register a handler
for that link scheme so a click copies the text straight to your clipboard.

## Updating

Once a day, the report checks `https://api.github.com/repos/jacobcheatley/standup-cli/commits/HEAD`
in the background and, if you are behind, appends a footer line telling you
so. Run `standup update` to upgrade (this runs `uv tool upgrade standup-cli`
under the hood). Set `[update] check = false` in the config to silence the
check entirely.

## Adding a source

Each file under `src/standup/sources/` declares a `TypedDict` for its config
table and one module-level object implementing the `Source` protocol from
`src/standup/sources/base.py`:

- `defaults(self) -> C`: the default `[sources.<name>]` table.
- `configure(self, current: C) -> C`: wizard prompts; returns the new table.
- `commands(self) -> typer.Typer | None`: an optional subcommand group,
  mounted at `standup <name>`.
- `fetch(self, ctx: SourceContext[C]) -> dict`: JSON-serializable raw data;
  must never raise for an expected failure, returning an `error` key instead.
- `sections(self, raw, ctx) -> list[Section]`: turns that raw data into
  report sections.

Two more are optional, for sources that take part in the pipeline-matching
flow: `merged_commits` and `fetch_dependent` (see `src/standup/sources/ado.py`
and `src/standup/sources/github.py`).

Then append the new module's `source` object to `SOURCES` in
`src/standup/sources/__init__.py`.

`src/standup/sources/reminders.py` is the smallest complete example: no
external API, a plain `enabled`-only config, and one subcommand group.

## Development

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check
```

## License

MIT. See [LICENSE](LICENSE).
