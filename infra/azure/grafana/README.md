# movate-dev Grafana — provisioned dashboards (#764)

`grafana-oss` runs as a stock-image Container App with **no persistent volume**,
so dashboards/datasources created via the API or UI **vanish on every redeploy**
— which is why only one dashboard was ever live even though the repo has the
full set. This makes Grafana **reproducible**: a custom image bakes the Azure
Monitor datasource + the in-repo `dashboards/grafana/azure/*.json` in via file
provisioning, so a deploy always shows them under the **MDK** folder.

As of ADR 087 it also provisions a **Prometheus** datasource + the in-repo
**PromQL** dashboards (`dashboards/grafana/mdk-*.json`: golden-signals, cost,
queue-and-pool, runtime-overview, exec-summary, dead-letter) under a separate
**MDK · Prometheus** folder. These are the highest-resolution real-time views we
own; they previously rendered nothing in the cloud because there was no
Prometheus datasource. They light up when the self-hosted Prometheus Container
App is deployed (`enablePrometheus=true` — the dev default).

## Files
- `Dockerfile` — `FROM grafana/grafana-oss:11.3.0`; copies the provisioning YAML
  to `/etc/grafana/provisioning/` and the azure dashboards to
  `/var/lib/grafana/dashboards/mdk/`. Built from a **staged context** that
  `deploy.sh` assembles (the provisioning configs live under `infra/`, which the
  repo `.dockerignore` excludes — staging avoids touching that shared ignore).
- `provisioning/datasources/azure-monitor.yaml` — the Azure Monitor datasource,
  uid **pinned** to `ffnrfwjnew5xcc` (the uid every dashboard references), msi
  auth via the container app's managed identity + `AZURE_SUBSCRIPTION_ID`.
- `provisioning/dashboards/mdk.yaml` — the file provider (folder "MDK", 30s
  re-scan, UI-editable).
- `deploy.sh` — `az acr build` + roll `movate-dev-grafana-oss` to the image.

## Deploy / add a dashboard
```bash
infra/azure/grafana/deploy.sh        # build + roll; dashboards go live, no manual import
# add a dashboard: drop a JSON in dashboards/grafana/azure/, re-run deploy.sh
```

The container app keeps its existing env (`GF_AZURE_MANAGED_IDENTITY_ENABLED`,
`AZURE_SUBSCRIPTION_ID`, anonymous-Viewer, admin creds) — `deploy.sh` only swaps
the image. No secrets are read or written by this setup.

## Notes
- Live dashboards today: Temporal, Voice (#793), Executive, Live Runtime.
- `dashboards/import-azure-grafana.sh` (API import) remains as the one-shot
  bridge for a Grafana you don't want to rebuild; provisioning is the durable path.
