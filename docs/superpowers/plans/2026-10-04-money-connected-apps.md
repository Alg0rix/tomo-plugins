# Money Connected Apps Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement task by task.

**Goal:** Deliver durable Gmail sync plus Sheets exports and Calendar bill reminders.
**Architecture:** Per-user SQLite jobs run bounded units through Tomo's managed background worker. Scoped provider adapters share job controls and connection UI.
**Tech Stack:** Python, SQLite, httpx, FastAPI, Jinja, vanilla JS.
**Spec:** docs/superpowers/specs/2026-10-04-money-connected-apps-design.md

## Global Constraints
- No external broker; reuse SDK lifecycle and existing user isolation.
- Default automatic checks off; when enabled, every 1800 seconds.
- Gmail lookbacks: 7d, 1m, 3m, 6m, 12m.
- No raw email body persistence; captured candidates require review.
- No live provider writes in tests; encrypted credentials only.

## Review Focus
- Crash during remote write: retry must not duplicate Calendar events.
- Disconnect/reconnect while a worker runs: reject stale commits via fencing.
- Mailbox with more than 60 receipts: paginate and preserve cursors.
- User deletion/disable: skip worker execution.
- Formula-like notes and stale spreadsheet rows: typed values and atomic replacement.

### Task 1: Durable jobs and Gmail
Files: store.py, jobs.py, gmail.py, tests/test_money_jobs.py.
Interfaces: jobs.enqueue(conn, provider, account, options), jobs.claim(conn), jobs.tick(api, stop); gmail.job_step(conn, job, stop).
- [x] Write failing SQLite tests for singleton active jobs, backoff, expiry, cancel fencing, lookback validation, Gmail pagination/dedupe.
- [x] Run tests, verify missing behavior fails.
- [x] Add schema, queue, checkpoints, Gmail adapter and SDK registration.
- [x] Run job tests and existing Money tests.

### Task 2: Google companions
Files: google_apps.py, store.py, tests/test_money_google.py.
Interfaces: begin_oauth/finish_oauth, configure, disconnect, job_step; bill CRUD validation.
- [x] Write failing provider tests for separate scopes, state expiry, encrypted tokens, idempotent writes, formula protection and recurrence.
- [x] Implement scoped OAuth, Sheets export and Calendar reminders.
- [x] Run provider and job tests.

### Task 3: UI, routes and release
Files: plugin.py, connected.py, templates/apps.html, static/apps.js, static/money.css, usage skill, README, manifests.
Interfaces: authenticated routes for settings, jobs, retry/cancel, provider connection, bills; polling safe public job state.
- [x] Write failing integration tests for route authorization, isolation, validation, asynchronous enqueue and template rendering.
- [x] Implement reference-based controls with progressive job progress and accessible states.
- [x] Run complete plugin suite, validator, JS syntax, diff checks; review and fix correctness issues.
- [ ] Commit and open a feature PR. Live OAuth validation remains operator work.

## Verification and review record

- Baseline: 15 tests passed before implementation.
- Final: 41 tests passed, including actual Tomo loader/routes and per-user SQLite.
- Catalog validator, Ruff E4/E7/E9/F and JavaScript syntax checks passed.
- Independent reviewer examined the whole change. Fixed stale refresh writes,
  stale-job schedule updates, mailbox-scoped search dedupe and deleted-message recovery.
- Additional regressions cover opt-in scheduling, active owner discovery, stop during
  fetch, Calendar lost-response retries, Google rate limits and encrypted OAuth exchange.
- Browser visual QA could not run: Chromium download returned a truncated archive.
  Live Google OAuth and destination writes were not exercised.
- Scope ruling: implement Gmail plus the two visible companion cards (Sheets and
  Calendar); Slack/Notion from the screenshot's introduction are outside this release.
