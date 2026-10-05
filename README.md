# Tomo Official Plugins

The home for official Tomo plugins. Each directory under `plugins/` is a complete
Tomo SDK v1 package with `tomo-plugin.json`, `plugin.py`, and optional templates,
static assets, and usage skills.

| Plugin | Purpose |
|---|---|
| Money | Ledger, budgets, reports, durable Gmail imports, Sheets exports, Calendar reminders and agent tools |
| Token Monitor | Usage analytics and turn/token history |
| Task Board | Current Kanban page placeholder |

## Install

Refresh **Tomo Official** in Tomo's `/settings/marketplaces`, then install from
Plugins. Or use an administrator API key with the CLI:

```sh
tomo plugins marketplaces refresh tomo-official
tomo plugins install money@tomo-official
tomo plugins enable money
```

Direct repository installation works without any marketplace registration:

```sh
tomo plugins install https://github.com/Alg0rix/tomo-plugins.git --subdirectory plugins/money
tomo plugins enable money
```

All official plugin source and feature tests live here. Tomo core provides the
runtime and SDK. Install the plugins you want from the official catalog;
installed source packages remain available offline.

## Catalog

This repository publishes its own `marketplace.json`, independently of
[Alg0rix/tomo-marketplace](https://github.com/Alg0rix/tomo-marketplace).
**Official plugins never need registration in the community marketplace.**
Adding or refreshing a catalog reads metadata only. Installation downloads a
resolved commit and registers the plugin disabled; enabling executes trusted code.

## Development

Follow [Tomo's plugin guide](https://github.com/Alg0rix/tomo/blob/main/docs/plugins.md).
Update the plugin manifest and its catalog entry together. Install
`requirements-dev.txt`, then run `python scripts/validate.py`. The validator
checks metadata and Python syntax without importing or running plugins.

Open a pull request here for changes to official plugins. Community authors
submit listings to tomo-marketplace or distribute directly from their own repo.

## Icons

Set `"icon": "wallet"` (Money), `"chart-column"` (Token Monitor), or `"columns-3"`
(Task Board) in both the plugin manifest and marketplace listing. Tomo renders
locally vendored open-source Lucide icons on cards, details, and sidebar entries.
Supported icon names are enumerated by `schema/marketplace.schema.json`.

## Ownership and tests

Token Monitor, Task Board, and Money are owned here. Tomo core provides the SDK,
loader, permissions, and agent integration; it does not bundle feature code.
Fresh users install plugins from Discover. Existing bundled registrations migrate
once to official Git sources while keeping enabled state and usage data.

Run runtime integration tests against a Tomo checkout using its environment:

```sh
PYTHONPATH=/path/to/tomo /path/to/tomo/.venv/bin/pytest -n 0 tests -q
```

Tests use temporary Tomo home, work, and database directories.

## Usage skills

Each plugin includes a domain `skills/<name>/SKILL.md`. Tomo discovers these
read-only skills while the plugin is enabled and refreshes entrypoints on reload.
Money contributes `plugin__money__money-management`, Token Monitor contributes
`plugin__token_monitor__token-usage`, and Task Board contributes
`plugin__kanban__task-board`. Tool permissions are configured separately.

## Money connected apps (1.4.0)

Money's Connected apps page supports Gmail imports with today-only, 7-day and
1/3/6/12-month lookbacks, persistent progress, cancel/retry and optional 30-minute checks. Imports
run in Tomo's lifecycle-managed worker, outside web requests, and always queue
candidates for review. Tomo must support `api.background_task`; update Tomo before
installing this version on an older runtime. Jobs resume after restart or reload;
interrupted in-flight units are recovered after a 120-second lease expires.

Create a Google OAuth **web application** client and enable Gmail, Sheets and
Calendar APIs. The page displays all three full callback URLs. Save the client ID
and secret, then consent separately to Gmail read-only, Sheets spreadsheet access,
or Calendar owned-event access. Secrets and tokens use Tomo's at-rest encryption.

For Sheets, enter the ID from a spreadsheet you can edit. Export owns the
`Tomo Money` tab and atomically replaces its cell values with up to 10,000 recorded
transactions, including removal of obsolete rows. It uses typed values so notes
beginning with `=` stay text. The export does not modify other tabs.

For Calendar, enter `primary` or an owned calendar ID, then add bill reminders.
Bills support one-off/weekly/monthly/yearly recurrence, an IANA timezone, and
0–28 days advance notice. Updates and deletions sync through idempotent event IDs.
Calendar recurrence follows RRULE semantics: the 31st skips short months and
February 29 yearly bills occur in leap years. Reminders never create ledger entries.
Changing a destination leaves previously exported copies in the old destination.
Disconnect only removes local tokens and cancels local work; revoke the app from
your Google account to remove its Google permissions entirely.

Each user has a private SQLite queue. Work uses leases and fencing, retries
transient errors with exponential backoff, pauses automatic checks after terminal
failure, and skips disabled users. Processed email bodies are not persisted.
Google account connections are not exercised by the test suite; complete a live
OAuth and sync smoke test in your deployment before using automatic exports.

Validation: `PYTHONPATH=/path/to/tomo python -m pytest tests -q` and
`python scripts/validate.py`. Provider tests replace only remote HTTP boundaries;
SQLite job state, pagination, user isolation and loader/routes run for real.
