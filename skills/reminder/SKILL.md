---
name: reminder
description: Personal reminders that surface in the morning `standup` report. Use when the user says "set a reminder", "remind me", "check back on this", "follow up on this tomorrow", "mark that reminder done", "what reminders do I have", or wants a personal TODO that is not worth an issue-tracker ticket.
---

# reminder

Reminders are JSON files in `~/.local/share/standup/reminders/`, managed by
the `standup reminders` CLI and listed at the top of `standup` output.

## Create

```bash
standup reminders add "<short title>" --due <ISO local datetime>
```

- **Title**: a few words, imperative ("Check back on the pipelines change").
- **Due**: convert the user's phrasing to ISO 8601 local time yourself
  (`2026-09-15T10:00`). Run `date` if unsure of today. Omit `--due` when no
  time was given.
- **Click target**: run inside a Claude session, the CLI automatically records
  this session's claude.ai link (`url`) and its resume command (`note`,
  `cd <cwd> && claude --resume <id>`). Do not pass `--url`/`--note` for
  "check back on this work" reminders.
- Reminder about a webpage, PR, or ticket instead of this session: pass
  `--url <link> --no-session`.
- Prints the reminder id (a small integer). Tell the user the id and the due time.

## Complete

```bash
standup reminders list       # open reminders with ids and notes
standup reminders done <id>
```

## Notes

- Never edit the JSON files by hand; the CLI owns the format.
- The report shows overdue reminders in red and today's in yellow; done
  reminders appear under "Recently merged/done" for a few days.
- In the rich report the note line is a `copy:` hyperlink; with the copy-uri
  utility installed (linked from the standup-cli README) a click copies the
  command to the clipboard.
