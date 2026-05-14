# Azure credentials — where things go

When you receive Azure access for the Movate subscription (Azure portal,
Tenant ID, App Client ID, Client Secret, Subscription ID, plus Azure
DevOps / GitHub URLs), follow this convention so the secret never ends
up in git, chat, or a shared drive — and so every script that needs
credentials looks in the same place.

## TL;DR — three files, one rule

| File | What goes in it | Tracked? |
|---|---|---|
| `~/.movate/azure.env` (chmod 600) | Tenant ID, Client ID, **Client Secret**, Subscription ID | No (outside repo) |
| `~/.movate/config.yaml` | Public URLs (portal, DevOps, GitHub org) + per-env target names | No (outside repo) |
| `docs/azure-credentials-setup.md` (this file) | The layout + how-to | Yes (committed, no secrets) |

**Rule:** the Client Secret only ever exists in `~/.movate/azure.env`
and (later) in Key Vault. It never appears in chat, in a screenshot,
in a Slack DM, or in any committed file.

## Setup

### 1. Create the secrets file (one time)

```bash
mkdir -p ~/.movate
touch ~/.movate/azure.env
chmod 600 ~/.movate/azure.env
```

### 2. Paste the values into `~/.movate/azure.env`

The file already exists (templated) on a developer's first run of any
movate-cli command. Open it in an editor (NOT in a terminal that echoes
to the screen-recording window) and fill in:

```bash
# Azure AD tenant (the Movate org).
AZURE_TENANT_ID=<paste tenant id>

# Service-principal application (client) ID.
AZURE_CLIENT_ID=<paste client id>

# Service-principal secret. SENSITIVE.
AZURE_CLIENT_SECRET=<paste client secret>

# The subscription this SP is granted Contributor on.
AZURE_SUBSCRIPTION_ID=<paste subscription id>

# Defaults to portal.azure.com — override only for sovereign clouds.
AZURE_PORTAL_URL=https://portal.azure.com
```

### 3. Add the public URLs to `~/.movate/config.yaml`

These are non-secret and convenient to keep alongside the per-env target
names already there:

```yaml
targets:
  dev:
    # ...existing fields...
    azure_portal_url: https://portal.azure.com
    azure_devops_url: https://dev.azure.com/<movate-org>
    github_org_url: https://github.com/<movate-org>
```

### 4. Verify by logging into Azure

```bash
set -a
source ~/.movate/azure.env
set +a

az login --service-principal \
    -u "$AZURE_CLIENT_ID" \
    -p "$AZURE_CLIENT_SECRET" \
    --tenant "$AZURE_TENANT_ID"

az account set --subscription "$AZURE_SUBSCRIPTION_ID"
az account show --query "{name:name, id:id, tenantId:tenantId}" -o table
```

If the last command prints the Movate subscription, you're set. The
`scripts/friday-demo-deploy.sh` and other infra scripts read this state.

## Where each value gets used

| Value | Used by |
|---|---|
| Tenant ID | `az login --service-principal --tenant ...` |
| Client ID | `az login --service-principal -u ...` |
| Client Secret | `az login --service-principal -p ...` (and ONLY there) |
| Subscription ID | `az account set --subscription ...` + `~/.movate/config.yaml: azure_subscription` |
| Portal URL | Documentation links + onboarding bundle |
| DevOps URL | (Future) CI pipeline references; not used by movate-cli today |
| GitHub org URL | The base for `mova-io-agents-<tenant>` repos (ADR 007 / item 81) |

## Rotation

The Client Secret should rotate every 90 days. When ops rotates:

1. Update `~/.movate/azure.env` with the new secret.
2. Re-run `az login --service-principal ...` to refresh your local CLI session.
3. Update any KV-stored copy (`movate-deploy-sp-secret` in `movate-bootstrap-kv`)
   so deploy scripts running off the bootstrap KV pick up the new value.

## What NOT to do

- **Don't** put the Client Secret in any `.bicepparam` file — those
  get committed by accident. Bicep reads secrets from Key Vault via
  the `getSecret()` reference instead (see `main.bicepparam.example`).
- **Don't** put the secret in `~/.zshrc` — that file is plain-text,
  often shared via dotfile repos, and shows up in screen recordings
  every time someone opens a new terminal.
- **Don't** paste the secret into chat (Claude, Slack, anywhere).
  If you accidentally do, rotate the secret immediately via
  `az ad app credential reset`.
