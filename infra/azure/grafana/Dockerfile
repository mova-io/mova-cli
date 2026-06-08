# movate-dev Grafana with the in-repo dashboards + Azure Monitor datasource
# baked in via file provisioning (#764). grafana-oss has no persistent volume,
# so hand-imported dashboards vanish on redeploy; baking them into the image
# makes a deploy reproducible — Temporal / Voice / Executive / Live Runtime all
# appear under the "MDK" folder on container start.
#
# Built from a STAGED context that deploy.sh assembles (the provisioning configs
# live under infra/, which the repo .dockerignore excludes — staging keeps that
# shared ignore untouched). The staged context has exactly:
#   ./provisioning/...        (the YAML below)
#   ./dashboards/*.json       (dashboards/grafana/azure/*.json)
FROM grafana/grafana-oss:11.3.0

# Provisioning config (datasource + dashboard provider) → Grafana's well-known
# config root. The datasource pins uid ffnrfwjnew5xcc (the uid every dashboard
# references) with managed-identity auth.
COPY provisioning/ /etc/grafana/provisioning/

# The dashboards themselves — the azure/ set (they target the Azure Monitor
# datasource above). Adding one + re-running deploy.sh makes it appear next deploy.
COPY dashboards/ /var/lib/grafana/dashboards/mdk/
