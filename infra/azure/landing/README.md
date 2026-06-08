# movate-dev landing page (tile index) — #761

The public landing page (the tile grid at the `movate-dev-landing` URL) used to
exist **only** as that Container App's `HTML_B64` env var, created out-of-band.
Every tile — and every edit (the Temporal UI / Langfuse tiles, the shared
login) — would be **lost on a full redeploy**. This makes it source-controlled +
reproducible.

## Files
- `index.html.tmpl` — the page markup with `__*_URL__` placeholders for the seven
  tile targets (the source of truth for the layout/tiles). Also hosts the
  **Agent Control Plane** in-page view (see below).
- `urls.env` — the tile target URLs (override any via the environment).
- `deploy-landing.sh` — renders the template with the URLs and updates the
  landing Container App's `HTML_B64`.

## Agent Control Plane (ADR 090)
The 🎛️ tile opens an **in-page** view (hash route `#control-plane`) — the landing
Container App serves a single HTML doc, so the control plane lives inside
`index.html.tmpl` rather than a separate file. It calls the runtime API directly
from the browser:
- `GET /api/v1/agents?health=1` — lists agents with status + a live health probe.
- `PATCH /api/v1/agents/{name}/status` — enable / deprecate / disable (needs an
  **admin** bearer token; entered in the UI and kept in `localStorage` only).
- `GET /api/v1/agents/{name}/health?probe=run` — the per-row "Test" button.

**CORS requirement:** the browser calls the API cross-origin, so the API
Container App must allow the landing origin. Set on the API app:
```bash
az containerapp update -n movate-dev-api -g movate-dev-rg \
  --set-env-vars "MDK_CORS_ALLOWED_ORIGINS=https://<landing-host>"   # or '*' for the demo
```
Without it the control plane loads nothing and shows a CORS hint. Bearer auth
needs no cookies, so `allow_credentials=False` (the default) is correct and `*`
is acceptable for the ephemeral demo.

## Edit / deploy
```bash
cd infra/azure/landing
# edit index.html.tmpl (add a tile) and/or urls.env (change a target)
./deploy-landing.sh
```

## Notes
- The two VM-hosted tiles (Temporal UI, Langfuse) point at bare `IP:port` today;
  give them DNS names (#767) and update `urls.env`.
- The Langfuse tile carries the shared demo login (`demo@movate.dev`) — rotate
  before non-demo use.
- Follow-up (#762): fold the landing Container App itself into bicep so the app
  (not just its HTML) is declarative.
