---
name: token-usage
description: Inspect Tomo agent token usage and turn history.
---

Open `/plugins/token_monitor/` to inspect the server’s recorded agent usage and turn history. Use the page’s existing filters to compare the same time range and agent when available.

Distinguish input, output, cached tokens, and total tokens according to the displayed fields. A missing counter is not proof of zero usage. Attribute differences to the records shown; do not invent prices or infer cost without a configured rate and currency.

This plugin observes completed turns and presents usage history. It does not register dedicated domain agent tools. Use an available authenticated browser capability for the page, or guide the user to it. Do not claim to have read the dashboard without inspecting it.

Reload and uninstall preserve stored usage data. Disabling stops new turn observations until enabled again.
