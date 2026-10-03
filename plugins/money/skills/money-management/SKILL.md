---
name: money-management
description: Record and inspect the current user’s income and expenses.
---

Use Tomo’s Money plugin for the current user’s ledger.

- Read totals with `plugin__money__summary`; read entries with `plugin__money__list_transactions`.
- Record income or an expense with `plugin__money__add_transaction`. Use its actual schema: `kind` is `income` or `expense`, `amount` is a positive decimal string in IDR, `category` is required, and `note` and `day` are optional. Resolve an unclear amount, category, or date before writing.
- Returned totals and transaction amounts use integer minor units: divide by 100 for display. Inputs use IDR amounts, not minor units. Do not mix currencies; this ledger currently uses IDR only.
- Open `/plugins/money/` for totals and `/plugins/money/transactions` for entries and the add form. Pages and agent tools share the same user-scoped data.
- The plugin currently supports adding and reading entries. Check available tools before promising edits, deletion, budgets, or bank imports. Report actual saved results.

Data survives reload, disable, and uninstall. Tool availability requires the plugin to be enabled and the tool permitted for the current agent.
