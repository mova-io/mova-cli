# Vendored third-party assets — licenses

The `mdk graph serve` viewer ships these JavaScript libraries **vendored**
(checked into `vendor/`) rather than loaded from a CDN, so the viewer renders
fully **air-gapped** — no network egress beyond the local proxy to your runtime.

These are browser **static assets**, not Python dependencies, so the Python
shipped-dependency license gate (`scripts/check_licenses.py`) does not cover
them. They are recorded here instead. **All three are MIT-licensed.** Each
vendored file carries an MIT banner at the top; this file is the index.

| Asset | Version | License | Upstream |
| --- | --- | --- | --- |
| `vendor/graphology.umd.min.js` | 0.25.4 | MIT | https://github.com/graphology/graphology |
| `vendor/graphology-layout-forceatlas2.umd.js` | 0.10.1 | MIT | https://github.com/graphology/graphology |
| `vendor/sigma.min.js` | 2.4.0 | MIT | https://github.com/jacomyal/sigma.js |

## Notes

- **`graphology`** — the in-memory graph model. Unmodified upstream UMD build
  (`graphology.umd.min.js`), exposes the `graphology` global (the `Graph`
  constructor). We use `graph.import(...)` to load the runtime's graphology JSON.

- **`graphology-layout-forceatlas2`** — the ForceAtlas2 layout + the `FA2Layout`
  web-worker supervisor (so layout runs off the main thread, animating nodes in
  as the graph grows). Upstream publishes this package as CommonJS modules with
  **no browser/UMD bundle**, so we vendor a faithful concatenation of the
  upstream MIT source modules (`index` / `iterate` / `helpers` / `defaults` /
  `worker` / `webworker`, plus `graphology-utils` `is-graph` / `getters`) wrapped
  in a tiny CommonJS-module registry and exposed as the UMD global
  `graphologyLayoutForceAtlas2` (with `.inferSettings` and `.FA2Layout`). **No
  upstream source line was altered** — only a module loader and a final
  `.FA2Layout` re-export were added. This deliberately pulls in **no**
  `crypto` / `object-hash` transitive dependency (which the all-in-one
  `graphology-library` bundle does), so it loads cleanly in a sandboxed /
  air-gapped browser. The build steps are reproducible from the upstream
  package at the version above.

- **`sigma.js`** — the WebGL renderer. Unmodified upstream UMD build
  (`sigma.min.js`), exposes the `Sigma` global. Drives the drillable
  interaction model (search, click-to-focus-neighborhood, node/edge reducers
  for dimming, camera fly-to).

## Refreshing a vendored asset

Re-download the upstream build at the pinned version, re-prepend the MIT banner
(copy the upstream `LICENSE.txt` verbatim into the `/*! ... */` header), and
bump the version in the table above. Never load these from a CDN at runtime —
the air-gapped guarantee is load-bearing for customer / on-prem deploys.
