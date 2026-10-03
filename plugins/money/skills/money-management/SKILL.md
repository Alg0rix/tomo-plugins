---
name: money-management
description: Record, inspect, and analyze the current user's income, expenses, budgets, spending sources, and captured transactions.
---

Use Tomo's Money plugin for the current user's ledger. All data is private to the current user — never mix users.

**Recording transactions**

- `plugin__money__add_transaction` records income or an expense. `kind` is `income` or `expense`, `amount` is a positive decimal string in the transaction currency (IDR by default), `category`/`note`/`day` (YYYY-MM-DD)/`source` (name) or `source_id`/`method` (payment rail: cash, qris, debit, credit, transfer, e-wallet…)/`external_key` (dedupe key) are optional.
- `plugin__money__update_transaction` and `plugin__money__delete_transaction` modify entries by id.
- When an amount, category, or date is ambiguous, confirm with the user before writing. Report the actual saved result.

**Reading and reporting**

- `plugin__money__summary` — this month's income/expense and all-time balance.
- `plugin__money__list_transactions` — filtered history (kind, category, source_id, method, q, month, start/end, limit).
- `plugin__money__report` — full report for a month (YYYY-MM) or year (YYYY): totals, previous-period delta, per-category shares, per-method totals, daily series, cumulative trend, and budget status.
- Returned amounts are integer minor units — divide by 100 for display. Inputs take major units, not minor units.

**Budgets, sources, categories**

- `plugin__money__set_budget` (category + amount, optional month and alert_pct), `plugin__money__list_budgets`, `plugin__money__delete_budget`.
- `plugin__money__add_source` (name, type: cash|bank|ewallet|card|savings|investment, optional opening_balance) and `plugin__money__list_sources` for balances.
- `plugin__money__add_category` and `plugin__money__list_categories` (name, kind, emoji icon, hex color).

**Capture and inbox**

- `plugin__money__scan_receipt` reads a receipt image (workplace path, `attachment:<id>`, or URL) and records it; pass `save=false` to queue it in the inbox for review instead.
- `plugin__money__ask_extract` parses free text (a pasted receipt, bank SMS, chat message) into a transaction. Confirm details first.
- `plugin__money__list_inbox`, `plugin__money__confirm_inbox` (with optional field overrides), `plugin__money__dismiss_inbox` manage the review queue for Gmail/receipt/CSV captures.
- `plugin__money__gmail_status` shows connection state. `plugin__money__search_emails` searches the connected mailbox with Gmail syntax (e.g. `from:ocbc.co.id after:2026/09/01`) and returns each message's sender, subject, date and body — read them yourself, extract the transactions with your own judgment, then record each via `plugin__money__add_transaction` passing `external_key` `gmail:<id>` so repeats never duplicate. Results flag `recorded`/`queued` items to skip. Users connect Gmail themselves from `/plugins/money/apps` — if not connected, say so and point them there. The Apps-page "Sync now" button can also bulk-queue receipt mail into the inbox without reading it.

**Pages**

- `/plugins/money/` dashboard, `/transactions` history, `/reports` charts, `/budgets`, `/sources`, `/inbox` review queue, `/imports` CSV + receipt upload, `/apps` Gmail connection. Pages and tools share the same data.

Data survives reload, disable, and uninstall. Tool availability requires the plugin to be enabled and the tool permitted for the current agent.
