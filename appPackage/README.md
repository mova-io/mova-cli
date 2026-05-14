# Teams App Package

This directory is the source for the **Teams app package** uploaded to
Azure Bot Service / Teams Admin Center.

## Layout

```
appPackage/
├── manifest.json    # Teams app manifest (v1.16)
├── icons/
│   ├── color.png    # 192×192 — main app catalog icon
│   └── outline.png  # 32×32  — Teams tray / mention badge
└── README.md        # you are here
```

## Build the .zip

```bash
./scripts/teams-package.sh
# → dist/movate-teams.zip ready to upload
```

The zipper script substitutes a few manifest fields from env vars at
build time:

| Env var | Manifest field | Default |
|---|---|---|
| `MOVATE_TEAMS_BOT_APP_ID` | `id` + `bots[0].botId` | `00000000-0000-0000-0000-000000000000` |
| `MOVATE_TEAMS_BOT_VERSION` | `version` | `0.7.0` |
| `MOVATE_TEAMS_VALID_DOMAINS` | `validDomains` | empty array |

For local-dev builds the defaults are fine; the placeholder app id is
a "fix-before-upload" sentinel the Teams Admin Center will reject if
you forget to swap it.

## Before publishing

The icons committed here are **placeholder solid-color PNGs**. Replace
them with real Movate-branded artwork before publishing to the Teams
app catalog:

* `color.png` — 192×192 PNG, full Movate logo on the Movate accent
  background (`#1F2937`).
* `outline.png` — 32×32 PNG, white-on-transparent silhouette. This is
  what shows in the Teams sidebar tray.

Teams' rejection rules: icons must be exactly the declared sizes,
PNG format, under 30KB each. The placeholder generation script in
`scripts/teams-package.sh` will warn (not fail) if it detects the
placeholder bytes.

## Manifest field references

Schema: https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json

* `bots[0].scopes`: `personal` (DM) + `team` + `groupchat`. Identity
  commands (`/movate connect`, `/whoami`, `/disconnect`) are
  hard-rejected outside `personal` by the handler.
* `bots[0].supportsFiles: true`: lets users drag agent.zip /
  dataset.jsonl in (slice 3.1.d).
* `commandLists`: surfaces the slash-commands in Teams' compose-box
  autocomplete. Cosmetic but high-leverage operator-friendliness —
  users discover commands without reading docs.
* `validDomains`: empty by default; populate with the Langfuse host
  + any external URLs the bot links to so Teams doesn't warn users
  on click-through.

## Production checklist

Before flipping the Teams app catalog from sideload to publish:

- [ ] Bot Service registered in the production Azure tenant
- [ ] Real PNG icons replace the placeholders
- [ ] `MOVATE_TEAMS_BOT_APP_ID` env set to the registered AAD app id
- [ ] JWT validation lands (see [issue #70](https://github.com/mova-io/mova-cli/issues/70))
- [ ] `validDomains` lists every URL the bot's cards can deep-link to
- [ ] Privacy + terms URLs updated to the Movate-internal pages
