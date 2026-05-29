/*
 * mdk graph serve — read-only knowledge graph viewer.
 *
 * Talks ONLY to the local `mdk graph serve` proxy at /api/* (same origin).
 * The proxy injects the runtime bearer server-side and forwards to the
 * deployed runtime's graph query API — so the bearer key NEVER appears in
 * this file, in the page, or in any browser-visible request header. This
 * viewer is strictly read-only: it never POST/PUT/DELETEs graph data.
 *
 * Interaction model (mirrors the sigma.js flagship demo):
 *   1. initial load + ForceAtlas2 layout (in a web worker)
 *   2. search-box autocomplete -> fly camera to node
 *   3. click a node -> focus its neighborhood (reducers dim the rest)
 *   4. double-click a node -> side panel w/ properties + provenance + agents
 *   5. expand -> fetch neighbors, import, let FA2 settle the new nodes
 *   6. live-growth toggle -> EventSource stream, add nodes/edges as they arrive
 *   7. color-by-type/community, size-by-degree, filter-by-type, zoom/pan
 */
(function () {
  "use strict";

  var Graph = window.graphology;
  var FA2 = window.graphologyLayoutForceAtlas2;
  var CFG = window.MDK_GRAPH_CONFIG || { project: null, target: "" };

  // ---- elements -----------------------------------------------------------
  var el = function (id) { return document.getElementById(id); };
  var container = el("graph-container");
  var searchInput = el("search");
  var suggestions = el("suggestions");
  var typeFilters = el("type-filters");
  var detailPanel = el("detail-panel");
  var statusBar = el("status-bar");
  var banner = el("banner");

  el("target-badge").textContent = CFG.target ? CFG.target + (CFG.project ? " / " + CFG.project : "") : "";

  // ---- state --------------------------------------------------------------
  var graph = new Graph({ multi: true, type: "directed" });
  var renderer = null;
  var fa2Layout = null;             // FA2Layout worker supervisor
  var selectedNode = null;          // node id for click-focus
  var hoveredNeighbors = null;      // Set of neighbor ids of selectedNode
  var disabledTypes = new Set();    // node types hidden by the filter checkboxes
  var colorMode = "type";           // "type" | "community"
  var sizeByDegree = true;
  var liveSource = null;            // EventSource for live growth
  var palette = {};                 // type/community -> color
  var paletteIdx = 0;

  var COLORS = [
    "#58a6ff", "#3fb950", "#f0883e", "#bc8cff", "#f85149",
    "#56d4dd", "#e3b341", "#db61a2", "#7ee787", "#a5d6ff",
    "#ff9492", "#d2a8ff", "#79c0ff", "#ffa657", "#aff5b4"
  ];

  function colorFor(key) {
    var k = key == null ? "·unknown·" : String(key);
    if (!(k in palette)) { palette[k] = COLORS[paletteIdx % COLORS.length]; paletteIdx++; }
    return palette[k];
  }

  function status(msg) {
    statusBar.textContent = msg;
    statusBar.classList.add("show");
    if (status._t) clearTimeout(status._t);
    status._t = setTimeout(function () { statusBar.classList.remove("show"); }, 2400);
  }

  function showBanner(html) {
    banner.innerHTML = html;
    banner.classList.remove("hidden");
  }

  // ---- API (all via the local proxy, same origin) -------------------------
  function api(path) {
    return fetch(path, { headers: { Accept: "application/json" }, credentials: "same-origin" })
      .then(function (r) {
        if (r.status === 501 || r.status === 502 || r.status === 404) {
          return r.json().catch(function () { return {}; }).then(function (b) {
            var e = new Error(b && b.error ? b.error : "graph API unavailable (HTTP " + r.status + ")");
            e.code = r.status; e.hint = b && b.hint; throw e;
          });
        }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
  }

  // ---- node/edge styling helpers ------------------------------------------
  function paletteKey(attrs) {
    return colorMode === "community" ? attrs.community : (attrs.kind || attrs.label);
  }

  function styleNode(node, attrs) {
    var degree = graph.degree(node);
    graph.mergeNodeAttributes(node, {
      size: sizeByDegree ? Math.max(3, Math.min(18, 3 + Math.sqrt(degree) * 2.2)) : 6,
      color: colorFor(paletteKey(attrs)),
      _baseColor: colorFor(paletteKey(attrs))
    });
  }

  function restyleAll() {
    graph.forEachNode(function (node, attrs) { styleNode(node, attrs); });
    if (renderer) renderer.refresh();
  }

  // ---- import graphology JSON from the runtime ----------------------------
  function importGraph(data, opts) {
    opts = opts || {};
    var hadPositions = true;
    var addedNodes = [];

    function ingestNode(n) {
      var key = n.key != null ? n.key : n.id;
      if (key == null || graph.hasNode(key)) {
        if (key != null && graph.hasNode(key)) {
          // Merge fresh attrs but never clobber sigma's reserved `type` program.
          var merge = Object.assign({}, n.attributes || {});
          if (merge.kind == null && merge.type != null) merge.kind = merge.type;
          delete merge.type;
          graph.mergeNodeAttributes(key, merge);
        }
        return;
      }
      var a = Object.assign({}, n.attributes || {});
      if (a.label == null) a.label = String(key);
      // Sigma 2.x RESERVES the node `type` attribute for its render program
      // ("circle"/"image"/…). The runtime sends a DOMAIN type ("org"/"policy"/…)
      // under `type`, which sigma would try (and fail) to resolve to a WebGL
      // program. Move the domain type to `kind` and pin sigma's `type` to the
      // built-in "circle" program. `kind` is what all our styling/filtering reads.
      if (a.kind == null && a.type != null) a.kind = a.type;
      a.type = "circle";
      if (typeof a.x !== "number" || typeof a.y !== "number") {
        hadPositions = false;
        a.x = (Math.random() - 0.5) * 100;
        a.y = (Math.random() - 0.5) * 100;
      }
      graph.addNode(key, a);
      addedNodes.push(key);
    }

    function ingestEdge(e) {
      var s = e.source, t = e.target;
      if (s == null || t == null || !graph.hasNode(s) || !graph.hasNode(t)) return;
      try { graph.addEdge(s, t, Object.assign({ size: 1, color: "#30363d" }, e.attributes || {})); }
      catch (_) { /* parallel/dup edge in a non-multi context — ignore */ }
    }

    (data.nodes || []).forEach(ingestNode);
    (data.edges || []).forEach(ingestEdge);

    graph.forEachNode(function (node, attrs) { styleNode(node, attrs); });
    rebuildTypeFilters();
    updateStats();

    return { hadPositions: hadPositions, addedNodes: addedNodes };
  }

  // ---- ForceAtlas2 layout (web worker) ------------------------------------
  function startLayout(transientMs) {
    if (graph.order === 0) return;
    if (!fa2Layout) {
      var settings = FA2.inferSettings(graph);
      // FA2Layout runs ForceAtlas2 inside a web worker (off the main thread),
      // so the UI stays responsive while the layout settles + animates.
      fa2Layout = new FA2.FA2Layout(graph, { settings: settings });
    }
    fa2Layout.start();
    status("laying out " + graph.order + " nodes…");
    if (transientMs) {
      setTimeout(function () { if (fa2Layout) fa2Layout.stop(); status("layout settled"); }, transientMs);
    }
  }

  function nudgeLayout() {
    // Re-run a short FA2 burst so newly added nodes settle into place.
    if (!fa2Layout) { startLayout(2500); return; }
    fa2Layout.start();
    setTimeout(function () { if (fa2Layout) fa2Layout.stop(); }, 2200);
  }

  // ---- sigma renderer + reducers ------------------------------------------
  function render() {
    renderer = new Sigma(graph, container, {
      defaultEdgeColor: "#30363d",
      labelColor: { color: "#e6edf3" },
      labelDensity: 0.6,
      labelGridCellSize: 80,
      renderEdgeLabels: false,
      minCameraRatio: 0.05,
      maxCameraRatio: 14
    });

    // Node reducer: when a node is selected (clicked), keep it + its
    // neighbors vivid and dim everything else. Also hides filtered types.
    renderer.setSetting("nodeReducer", function (node, data) {
      var res = Object.assign({}, data);
      var kind = graph.getNodeAttribute(node, "kind");
      if (disabledTypes.has(kind)) { res.hidden = true; return res; }
      if (selectedNode) {
        if (node === selectedNode) {
          res.highlighted = true;
          res.zIndex = 2;
        } else if (hoveredNeighbors && hoveredNeighbors.has(node)) {
          res.zIndex = 1;
        } else {
          res.color = "#2a3038";
          res.label = "";
          res.zIndex = 0;
        }
      }
      return res;
    });

    // Edge reducer: dim edges not touching the focused neighborhood.
    renderer.setSetting("edgeReducer", function (edge, data) {
      var res = Object.assign({}, data);
      var s = graph.source(edge), t = graph.target(edge);
      if (disabledTypes.has(graph.getNodeAttribute(s, "kind")) ||
          disabledTypes.has(graph.getNodeAttribute(t, "kind"))) {
        res.hidden = true; return res;
      }
      if (selectedNode) {
        if (s === selectedNode || t === selectedNode) {
          res.color = "#58a6ff"; res.zIndex = 1;
        } else { res.hidden = true; }
      }
      return res;
    });

    // Click a node -> focus its neighborhood.
    renderer.on("clickNode", function (e) { focusNode(e.node); });

    // Click empty space -> reset the focus.
    renderer.on("clickStage", function () { clearFocus(); });

    // Double-click a node -> open the detail panel (and DON'T let sigma zoom).
    renderer.on("doubleClickNode", function (e) {
      e.preventSigmaDefault();   // suppress sigma's default double-click zoom
      openDetail(e.node);
    });
    renderer.on("doubleClickStage", function (e) { e.preventSigmaDefault(); });
  }

  function focusNode(node) {
    selectedNode = node;
    hoveredNeighbors = new Set(graph.neighbors(node));
    renderer.refresh();
    status("focused " + (graph.getNodeAttribute(node, "label") || node) +
           " (" + hoveredNeighbors.size + " neighbors)");
  }

  function clearFocus() {
    selectedNode = null;
    hoveredNeighbors = null;
    if (renderer) renderer.refresh();
  }

  function flyTo(node) {
    if (!graph.hasNode(node)) return;
    var disp = renderer.getNodeDisplayData(node);
    if (!disp) return;
    renderer.getCamera().animate(
      { x: disp.x, y: disp.y, ratio: 0.35 },
      { duration: 500 }
    );
    focusNode(node);
  }

  // ---- detail panel (double-click) ----------------------------------------
  function openDetail(node) {
    detailPanel.classList.remove("hidden");
    detailPanel.setAttribute("aria-hidden", "false");
    el("detail-label").textContent = graph.getNodeAttribute(node, "label") || node;
    el("detail-type").textContent = graph.getNodeAttribute(node, "kind") || "node";
    el("detail-props").innerHTML = '<dt class="empty">loading…</dt><dd></dd>';
    el("detail-provenance").innerHTML = '<span class="empty">loading…</span>';
    el("detail-agents").innerHTML = "";
    el("expand-btn").disabled = true;
    el("expand-btn").dataset.node = node;

    api("/api/v1/graph/nodes/" + encodeURIComponent(node))
      .then(function (d) { renderDetail(node, d); })
      .catch(function (err) {
        el("detail-props").innerHTML = '<dt class="empty">' + escapeHtml(err.message) + "</dt><dd></dd>";
        el("detail-provenance").innerHTML = '<span class="empty">unavailable</span>';
        el("expand-btn").disabled = false;
      });
  }

  function renderDetail(node, d) {
    el("detail-label").textContent = d.label || graph.getNodeAttribute(node, "label") || node;
    el("detail-type").textContent = d.type || "node";

    var props = d.properties || d.attributes || {};
    var keys = Object.keys(props).filter(function (k) { return k[0] !== "_" && k !== "x" && k !== "y"; });
    el("detail-props").innerHTML = keys.length
      ? keys.map(function (k) {
          return "<dt>" + escapeHtml(k) + "</dt><dd>" + escapeHtml(fmtVal(props[k])) + "</dd>";
        }).join("")
      : '<dt class="empty">no properties</dt><dd></dd>';

    var prov = d.provenance || [];
    el("detail-provenance").innerHTML = prov.length
      ? prov.map(function (p) {
          var url = p.url || p.source_url;
          var src = p.source || p.source_chunk_id || url || "source";
          var conf = (p.confidence != null) ? p.confidence : p.extraction_confidence;
          return '<div class="src">' +
            (url ? '<a href="' + escapeAttr(url) + '" target="_blank" rel="noopener">' + escapeHtml(src) + "</a>"
                 : "<strong>" + escapeHtml(src) + "</strong>") +
            (conf != null ? ' <span class="conf">conf ' + escapeHtml(fmtVal(conf)) + "</span>" : "") +
            (p.chunk || p.text ? '<span class="chunk">' + escapeHtml(p.chunk || p.text) + "</span>" : "") +
            "</div>";
        }).join("")
      : '<span class="empty">no provenance recorded</span>';

    var agents = d.referenced_by_agents || d.agents || [];
    el("detail-agents").innerHTML = agents.length
      ? agents.map(function (a) { return "<li>" + escapeHtml(typeof a === "string" ? a : (a.name || a.id)) + "</li>"; }).join("")
      : '<li class="empty">none</li>';

    el("expand-btn").disabled = false;
  }

  // ---- expand: fetch + import neighbors ------------------------------------
  function expand(node) {
    el("expand-btn").disabled = true;
    status("expanding " + (graph.getNodeAttribute(node, "label") || node) + "…");
    api("/api/v1/graph/nodes/" + encodeURIComponent(node) + "/neighbors")
      .then(function (data) {
        var before = graph.order;
        var res = importGraph(data);
        var added = graph.order - before;
        if (added > 0) { nudgeLayout(); status("added " + added + " node(s)"); }
        else status("no new neighbors");
        focusNode(node);
      })
      .catch(function (err) { status("expand failed: " + err.message); })
      .finally(function () { el("expand-btn").disabled = false; });
  }

  // ---- search autocomplete ------------------------------------------------
  var sugIndex = -1;
  function runSearch(q) {
    suggestions.innerHTML = "";
    sugIndex = -1;
    var needle = q.trim().toLowerCase();
    if (!needle) { suggestions.classList.remove("open"); return; }
    var matches = [];
    graph.forEachNode(function (node, attrs) {
      var label = String(attrs.label || node);
      if (label.toLowerCase().indexOf(needle) !== -1) {
        matches.push({ node: node, label: label, type: attrs.kind || "" });
      }
    });
    matches.sort(function (a, b) { return a.label.length - b.label.length; });
    matches.slice(0, 12).forEach(function (m) {
      var li = document.createElement("li");
      li.textContent = m.label;
      if (m.type) { var s = document.createElement("span"); s.className = "stype"; s.textContent = m.type; li.appendChild(s); }
      li.addEventListener("mousedown", function (ev) { ev.preventDefault(); pickSuggestion(m.node, m.label); });
      suggestions.appendChild(li);
    });
    suggestions.classList.toggle("open", matches.length > 0);
  }

  function pickSuggestion(node, label) {
    searchInput.value = label;
    suggestions.classList.remove("open");
    flyTo(node);
  }

  // ---- type filters -------------------------------------------------------
  function rebuildTypeFilters() {
    var counts = {};
    graph.forEachNode(function (node, attrs) {
      var t = attrs.kind || "(untyped)";
      counts[t] = (counts[t] || 0) + 1;
    });
    var types = Object.keys(counts).sort();
    typeFilters.innerHTML = "";
    types.forEach(function (t) {
      var key = t === "(untyped)" ? undefined : t;
      var label = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox"; cb.checked = !disabledTypes.has(key);
      cb.addEventListener("change", function () {
        if (cb.checked) disabledTypes.delete(key); else disabledTypes.add(key);
        if (renderer) renderer.refresh();
        updateStats();
      });
      var sw = document.createElement("span");
      sw.className = "swatch";
      sw.style.background = colorMode === "type" ? colorFor(key != null ? key : "(untyped)") : "#6e7681";
      var txt = document.createElement("span"); txt.textContent = t;
      var cnt = document.createElement("span"); cnt.className = "tcount"; cnt.textContent = counts[t];
      label.appendChild(cb); label.appendChild(sw); label.appendChild(txt); label.appendChild(cnt);
      typeFilters.appendChild(label);
    });
  }

  function updateStats() {
    var visN = 0;
    graph.forEachNode(function (node, attrs) {
      if (!disabledTypes.has(attrs.kind)) visN++;
    });
    el("stat-nodes").textContent = visN;
    el("stat-edges").textContent = graph.size;
  }

  // ---- live growth (Server-Sent Events) -----------------------------------
  function setLive(on) {
    el("live-toggle").parentElement.classList.toggle("on", on);
    if (on) {
      if (liveSource) return;
      var url = "/api/v1/projects/" + encodeURIComponent(CFG.project) + "/graph/stream";
      liveSource = new EventSource(url, { withCredentials: true });
      liveSource.addEventListener("node.added", function (ev) { onLiveNode(ev); });
      liveSource.addEventListener("edge.added", function (ev) { onLiveEdge(ev); });
      // Generic message fallback for runtimes that don't set an event name.
      liveSource.onmessage = function (ev) { onLiveGeneric(ev); };
      liveSource.onerror = function () { status("live stream error — retrying…"); };
      status("live growth ON — watching for ingest events");
    } else if (liveSource) {
      liveSource.close(); liveSource = null;
      status("live growth OFF");
    }
  }

  function parseEvent(ev) { try { return JSON.parse(ev.data); } catch (_) { return null; } }

  function onLiveNode(ev) {
    var n = parseEvent(ev); if (!n) return;
    var before = graph.order;
    importGraph({ nodes: [n], edges: [] });
    if (graph.order > before) { nudgeLayout(); status("+ node " + (n.attributes && n.attributes.label || n.key || n.id)); }
  }
  function onLiveEdge(ev) {
    var e = parseEvent(ev); if (!e) return;
    var before = graph.size;
    importGraph({ nodes: [], edges: [e] });
    if (graph.size > before) { nudgeLayout(); status("+ edge"); }
  }
  function onLiveGeneric(ev) {
    var payload = parseEvent(ev); if (!payload) return;
    if (payload.nodes || payload.edges) { importGraph(payload); nudgeLayout(); }
  }

  // ---- helpers ------------------------------------------------------------
  function fmtVal(v) {
    if (v == null) return "";
    if (typeof v === "object") { try { return JSON.stringify(v); } catch (_) { return String(v); } }
    if (typeof v === "number") return (Math.round(v * 1000) / 1000).toString();
    return String(v);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function escapeAttr(s) { return escapeHtml(s).replace(/`/g, "&#96;"); }

  // ---- wiring -------------------------------------------------------------
  function wireControls() {
    searchInput.addEventListener("input", function () { runSearch(searchInput.value); });
    searchInput.addEventListener("keydown", function (e) {
      var items = suggestions.querySelectorAll("li");
      if (e.key === "ArrowDown") { e.preventDefault(); sugIndex = Math.min(sugIndex + 1, items.length - 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sugIndex = Math.max(sugIndex - 1, 0); }
      else if (e.key === "Enter") {
        if (items.length) { e.preventDefault(); var i = sugIndex >= 0 ? sugIndex : 0; items[i].dispatchEvent(new MouseEvent("mousedown")); }
        return;
      } else if (e.key === "Escape") { suggestions.classList.remove("open"); return; }
      items.forEach(function (li, i) { li.classList.toggle("active", i === sugIndex); });
    });
    document.addEventListener("click", function (e) {
      if (!suggestions.contains(e.target) && e.target !== searchInput) suggestions.classList.remove("open");
    });

    el("color-mode").addEventListener("change", function (e) {
      colorMode = e.target.value;
      palette = {}; paletteIdx = 0;
      restyleAll(); rebuildTypeFilters();
    });
    el("size-by-degree").addEventListener("change", function (e) {
      sizeByDegree = e.target.checked; restyleAll();
    });
    el("live-toggle").addEventListener("change", function (e) { setLive(e.target.checked); });
    el("detail-close").addEventListener("click", function () {
      detailPanel.classList.add("hidden"); detailPanel.setAttribute("aria-hidden", "true");
    });
    el("expand-btn").addEventListener("click", function () {
      var node = el("expand-btn").dataset.node; if (node) expand(node);
    });
  }

  // ---- bootstrap ----------------------------------------------------------
  function boot() {
    wireControls();
    if (!CFG.project) {
      showBanner("No project id configured. Re-run <code>mdk graph serve --target &lt;env&gt; --project &lt;id&gt;</code>.");
      return;
    }
    status("loading graph for " + CFG.project + "…");
    api("/api/v1/projects/" + encodeURIComponent(CFG.project) + "/graph?mode=knowledge")
      .then(function (data) {
        var res = importGraph(data);
        render();
        if (!res.hadPositions && graph.order > 0) {
          startLayout(Math.min(8000, 2500 + graph.order * 4));
        } else {
          status("loaded " + graph.order + " nodes");
        }
      })
      .catch(function (err) {
        if (err.code === 404 || err.code === 501) {
          showBanner(
            "This runtime does not expose the graph query API " +
            "(<code>/api/v1/.../graph</code>). It likely predates ADR 046 / the graph " +
            "query API — upgrade the runtime and check <code>mdk capabilities</code>." +
            (err.hint ? "<br><small>" + escapeHtml(err.hint) + "</small>" : "")
          );
        } else {
          showBanner("Failed to load graph: " + escapeHtml(err.message));
        }
      });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
