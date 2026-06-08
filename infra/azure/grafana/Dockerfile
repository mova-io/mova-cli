# movate-dev Grafana with the in-repo dashboards + Azure Monitor datasource
# baked in via file provisioning (#764). grafana-oss has no persistent volume,
# so hand-imported dashboards vanish on redeploy; baking them into the image
# makes a deploy reproducible — Temporal / Voice / Executive / Live Runtime all
# appear under the "MDK" folder on container start.
#
# Built from a STAGED context that deploy.sh assembles (the provisioning configs
# live under infra/, which the repo .dockerignore excludes — staging keeps that
# shared ignore untouched). The staged context has exactly:
#   ./provisioning/...           (the YAML below)
#   ./dashboards/*.json          (dashboards/grafana/azure/*.json — KQL)
#   ./dashboards-prom/*.json     (dashboards/grafana/mdk-*.json — PromQL, ADR 087)
FROM grafana/grafana-oss:11.3.0

# Provisioning config (Azure Monitor + Prometheus datasources + dashboard
# providers) → Grafana's well-known config root. The Azure datasource pins uid
# ffnrfwjnew5xcc and the Prometheus datasource pins uid mdkpromds00001 (the uids
# the respective dashboards reference) so panels bind on first load.
COPY provisioning/ /etc/grafana/provisioning/

# The Azure-Monitor (KQL) dashboards → "MDK" folder.
COPY dashboards/ /var/lib/grafana/dashboards/mdk/

# The PromQL dashboards (ADR 087) → "MDK · Prometheus" folder. deploy.sh rewrites
# their ${DS_PROMETHEUS}/${DS_INSIGHTS} placeholders to the pinned datasource
# uids during staging, so they bind to the provisioned datasources.
COPY dashboards-prom/ /var/lib/grafana/dashboards/prometheus/
