# Tomo Official Plugins

The home for official Tomo plugins. Each directory under `plugins/` is a complete
Tomo SDK v1 package with `tomo-plugin.json`, `plugin.py`, and optional templates
and static assets.

| Plugin | Purpose |
|---|---|
| Money | Income/expense ledger, overview and transactions pages, and agent tools |
| Token Monitor | Usage analytics and turn/token history; bundled with Tomo |
| Task Board | Current Kanban page placeholder; bundled with Tomo |

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

Token Monitor and Task Board ship with Tomo; manage their bundled copies through
Plugins rather than installing a second copy. Official sources are maintained
here; shipped snapshots stay available offline.

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
