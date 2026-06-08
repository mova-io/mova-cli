# movate-dev landing page (tile index) — #761

The public landing page (the tile grid at the `movate-dev-landing` URL) used to
exist **only** as that Container App's `HTML_B64` env var, created out-of-band.
Every tile — and every edit (the Temporal UI / Langfuse tiles, the shared
login) — would be **lost on a full redeploy**. This makes it source-controlled +
reproducible.

## Files
- `index.html.tmpl` — the page markup with `__*_URL__` placeholders for the six
  tile targets (the source of truth for the layout/tiles).
- `urls.env` — the tile target URLs (override any via the environment).
- `deploy-landing.sh` — renders the template with the URLs and updates the
  landing Container App's `HTML_B64`.

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
