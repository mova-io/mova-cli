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
 *   3. click a node -> focus its neighborhood (reducers dim the rest) AND
 *      open the drill-down side panel: properties + relations grouped by
 *      type + connected entities (tickets/SOPs/docs) as clickable links +
 *      provenance + referencing agents
 *   4. click a connected-entity link -> re-center on it (fly + re-drill);
 *      links for nodes not yet loaded fetch+import them first
 *   5. expand -> fetch neighbors, import, let FA2 settle the new nodes
 *   6. live-growth toggle -> EventSource stream, add nodes/edges as they arrive
 *   7. color-by-type/community, size-by-degree, filter-by-type, zoom/pan
 *   8. analytics (opt-in, ADR 046): size+color by centrality (degree |
 *      betweenness), tint by detected community, and highlight the shortest
 *      path between two nodes — each fetched from /graph/analytics/* via the
 *      same proxy and applied over the loaded graph (no data mutation; toggling
 *      off restores the base styling). Graceful on empty/small graphs.
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
  // Confidence dimming (ADR 046 D2): fade nodes whose stored `confidence`
  // attribute is below this floor so the audience sees the graph is honest
  // about uncertainty. 0 (default) = show everything at full strength —
  // back-compat, no visual change unless an operator drags the slider. A node
  // with no recorded confidence is treated as full-confidence (never dimmed).
  var minConfidence = 0;
  // How long the paced snapshot replay sleeps between frames server-side, so
  // the live-growth toggle makes the graph visibly ASSEMBLE node-by-node even
  // for a graph that was built atomically before the viewer opened.
  var LIVE_REPLAY_PACE_S = 0.12;

  // ---- analytics state (ADR 046) -----------------------------------------
  // All analytics are OFF by default — base render, drill-down, and live-growth
  // behave exactly as before unless an operator opts in. Each toggle fetches
  // from the runtime's /graph/analytics/* endpoints (via the same local proxy)
  // and decorates the already-loaded graph; none of it mutates the graph data,
  // so toggling off restores the base styling. All graceful on an empty graph.
  var centralityOn = false;         // size + color nodes by centrality score
  var centralityMeasure = "degree"; // "degree" | "betweenness"
  var centralityScores = {};        // node id -> normalized score in [0,1]
  var communityOn = false;          // tint nodes by detected community id
  var communityOf = {};             // node id -> community id (int)
  var pathSet = null;               // Set of node ids on a highlighted path
  var pathEdgeKeys = null;          // Set of edge keys on the highlighted path

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

  // Map a centrality score in [0,1] onto a sequential cool→hot ramp (blue →
  // amber → red) so a more central node reads as "hotter". Pure function of the
  // score; no palette state.
  function centralityColor(score) {
    var s = Math.max(0, Math.min(1, score || 0));
    var stops = ["#1f3b73", "#3b6fb0", "#56d4dd", "#e3b341", "#f0883e", "#f85149"];
    var t = s * (stops.length - 1);
    return stops[Math.round(t)];
  }

  // Confidence of a node from its stored `confidence` attribute (ADR 046 D2).
  // Returns a number in [0,1], or null when no score was recorded (the node is
  // then treated as full-confidence — never dimmed).
  function confidenceOf(node) {
    var c = graph.getNodeAttribute(node, "confidence");
    return (typeof c === "number") ? c : null;
  }

  // True iff a node should be dimmed for being below the confidence floor.
  // A node with no recorded confidence is never dimmed (full-confidence
  // assumption); the floor at 0 dims nothing (back-compat default).
  function isLowConfidence(node) {
    if (minConfidence <= 0) return false;
    var c = confidenceOf(node);
    return c !== null && c < minConfidence;
  }

  function styleNode(node, attrs) {
    var degree = graph.degree(node);
    var size, color;
    if (centralityOn) {
      // Centrality wins for both size and color: a more-central node is bigger
      // and hotter. Falls back to a neutral small dot for a node with no score
      // (e.g. a node added by drill-in after centrality was computed).
      var score = centralityScores[node];
      var s = (typeof score === "number") ? score : 0;
      size = Math.max(3, Math.min(20, 4 + s * 16));
      color = centralityColor(s);
    } else if (communityOn && communityOf[node] != null) {
      size = sizeByDegree ? Math.max(3, Math.min(18, 3 + Math.sqrt(degree) * 2.2)) : 6;
      color = colorFor("community:" + communityOf[node]);
    } else {
      size = sizeByDegree ? Math.max(3, Math.min(18, 3 + Math.sqrt(degree) * 2.2)) : 6;
      color = colorFor(paletteKey(attrs));
    }
    graph.mergeNodeAttributes(node, { size: size, color: color, _baseColor: color });
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
    var addedEdges = [];

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
      // Prefer the server-supplied edge key (stable across re-imports / lets
      // the live highlight target it); fall back to graphology's generated
      // key when none is given.
      try {
        var key = (e.key != null && !graph.hasEdge(e.key))
          ? graph.addEdgeWithKey(e.key, s, t, Object.assign({ size: 1, color: "#30363d" }, e.attributes || {}))
          : graph.addEdge(s, t, Object.assign({ size: 1, color: "#30363d" }, e.attributes || {}));
        addedEdges.push(key);
      } catch (_) { /* parallel/dup edge in a non-multi context — ignore */ }
    }

    (data.nodes || []).forEach(ingestNode);
    (data.edges || []).forEach(ingestEdge);

    graph.forEachNode(function (node, attrs) { styleNode(node, attrs); });
    rebuildTypeFilters();
    updateStats();

    return { hadPositions: hadPositions, addedNodes: addedNodes, addedEdges: addedEdges };
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
      // Confidence dimming (ADR 046 D2): a node below the confidence floor
      // fades to a muted grey and shrinks, and drops its label so the eye
      // settles on the confident core of the graph. Computed before the
      // focus/path/live decorations below so those (which the operator is
      // actively driving) can still override it for a dimmed node they click.
      if (isLowConfidence(node)) {
        res.color = "#3a4048";
        res.label = "";
        res.size = (res.size || 4) * 0.6;
        res.zIndex = 0;
      }
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
      // Shortest-path highlight (analytics): nodes ON the path glow + pop above
      // any focus dimming; nodes OFF the path are dimmed while a path is shown.
      if (pathSet) {
        if (pathSet.has(node)) {
          res.color = "#f0883e";
          res.highlighted = true;
          res.size = (res.size || 4) * 1.3;
          res.zIndex = 4;
        } else {
          res.color = "#2a3038";
          res.label = "";
          res.zIndex = 0;
        }
      }
      // Live-growth halo: a freshly-arrived node briefly glows + grows so the
      // eye catches the graph growing. Cleared by highlightNew() after a beat.
      if (graph.getNodeAttribute(node, "_new")) {
        res.color = "#7ee787";
        res.highlighted = true;
        res.size = (res.size || 4) * 1.6;
        res.zIndex = 3;
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
      // Fade an edge whose endpoint is below the confidence floor so a dimmed
      // node doesn't leave a vivid edge dangling into the confident core.
      if (isLowConfidence(s) || isLowConfidence(t)) {
        res.color = "#22272e"; res.zIndex = 0;
      }
      if (selectedNode) {
        if (s === selectedNode || t === selectedNode) {
          res.color = "#58a6ff"; res.zIndex = 1;
        } else { res.hidden = true; }
      }
      // Shortest-path highlight: edges ON the path are drawn thick + amber and
      // never hidden; other edges are hidden while a path is shown so the route
      // reads clearly.
      if (pathEdgeKeys) {
        if (pathEdgeKeys.has(edge)) {
          res.hidden = false; res.color = "#f0883e"; res.size = (res.size || 1) + 2; res.zIndex = 4;
        } else if (!selectedNode) {
          res.hidden = true;
        }
      }
      // Live-growth halo on a freshly-arrived edge (mirrors the node glow).
      if (graph.getEdgeAttribute(edge, "_new")) {
        res.color = "#7ee787"; res.size = (res.size || 1) + 1.5; res.zIndex = 3;
      }
      return res;
    });

    // Click a node -> focus its neighborhood AND open the drill-down panel.
    renderer.on("clickNode", function (e) { focusNode(e.node); openDetail(e.node); });

    // Click empty space -> reset the focus (panel stays — close it via its ×).
    renderer.on("clickStage", function () { clearFocus(); });

    // Double-click still opens the panel but must NOT let sigma zoom.
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

  // ---- detail panel (drill-down) ------------------------------------------
  function openDetail(node) {
    detailPanel.classList.remove("hidden");
    detailPanel.setAttribute("aria-hidden", "false");
    el("detail-label").textContent = graph.getNodeAttribute(node, "label") || node;
    el("detail-type").textContent = graph.getNodeAttribute(node, "kind") || "node";
    el("detail-props").innerHTML = '<dt class="empty">loading…</dt><dd></dd>';
    el("detail-relations").innerHTML = '<div class="empty">loading…</div>';
    el("detail-provenance").innerHTML = '<span class="empty">loading…</span>';
    el("detail-agents").innerHTML = "";
    el("expand-btn").disabled = true;
    el("expand-btn").dataset.node = node;

    api("/api/v1/graph/nodes/" + encodeURIComponent(node))
      .then(function (d) { renderDetail(node, d); })
      .catch(function (err) {
        el("detail-props").innerHTML = '<dt class="empty">' + escapeHtml(err.message) + "</dt><dd></dd>";
        el("detail-relations").innerHTML = '<div class="empty">unavailable</div>';
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

    renderRelations(node, d.neighbors || []);

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

  // ---- connected entities, grouped by relation type -----------------------
  // Renders the node's 1-hop neighbors (the runtime's `neighbors[]`) grouped
  // by relation type. Each connected entity is a clickable link that
  // re-centers the graph on it and re-drills (drillTo). Graceful when the
  // node has no neighbors.
  function renderRelations(node, neighbors) {
    var box = el("detail-relations");
    box.innerHTML = "";
    if (!neighbors.length) {
      box.innerHTML = '<div class="empty">no connected entities</div>';
      return;
    }
    // Group by relation type, preserving the server's (strongest-first) order.
    var groups = {};
    var order = [];
    neighbors.forEach(function (nb) {
      var rel = nb.relation || "related";
      if (!(rel in groups)) { groups[rel] = []; order.push(rel); }
      groups[rel].push(nb);
    });
    order.forEach(function (rel) {
      var group = groups[rel];
      var h = document.createElement("div");
      h.className = "rel-group";
      var head = document.createElement("div");
      head.className = "rel-head";
      head.appendChild(document.createTextNode(rel));
      var cnt = document.createElement("span");
      cnt.className = "rel-count"; cnt.textContent = group.length;
      head.appendChild(cnt);
      h.appendChild(head);
      group.forEach(function (nb) {
        var a = document.createElement("a");
        a.className = "rel-link";
        a.href = "#";
        a.title = (nb.type ? nb.type + " — " : "") +
          (nb.direction === "in" ? "points to this node" : "this node points to it");
        var arrow = document.createElement("span");
        arrow.className = "rel-dir";
        arrow.textContent = nb.direction === "in" ? "←" : "→";
        a.appendChild(arrow);
        var lbl = document.createElement("span");
        lbl.className = "rel-label"; lbl.textContent = nb.label || nb.key;
        a.appendChild(lbl);
        if (nb.type) {
          var ty = document.createElement("span");
          ty.className = "rel-type"; ty.textContent = nb.type;
          a.appendChild(ty);
        }
        a.addEventListener("click", function (ev) {
          ev.preventDefault();
          drillTo(nb.key, nb.label);
        });
        h.appendChild(a);
      });
      box.appendChild(h);
    });
  }

  // Re-center the graph on a connected entity and re-open its drill-down.
  // If the node isn't loaded yet (the detail came from the runtime, the
  // graph window may not include it), fetch+import its neighborhood first
  // so the camera has something to fly to.
  function drillTo(key, label) {
    if (graph.hasNode(key)) { flyTo(key); openDetail(key); return; }
    status("loading " + (label || key) + "…");
    api("/api/v1/graph/nodes/" + encodeURIComponent(key) + "/neighbors")
      .then(function (data) {
        var before = graph.order;
        importGraph(data);
        if (graph.order > before) nudgeLayout();
        if (graph.hasNode(key)) flyTo(key);
        openDetail(key);
      })
      .catch(function (err) {
        status("could not open " + (label || key) + ": " + err.message);
        openDetail(key);  // still show whatever detail the runtime returns
      });
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
      if (disabledTypes.has(attrs.kind)) return;
      if (isLowConfidence(node)) return;  // dimmed by the confidence floor
      visN++;
    });
    el("stat-nodes").textContent = visN;
    el("stat-edges").textContent = graph.size;
  }

  // ---- analytics (centrality / communities / shortest path, ADR 046) ------
  // Each helper hits a project-scoped /graph/analytics/* endpoint via the same
  // local proxy and decorates the loaded graph. The runtime computes over the
  // same windowed + tenant/project-scoped graph the viewer loaded, so results
  // line up with what's on screen. Every helper degrades gracefully: on an
  // empty graph or an older runtime (404/501) it just clears the decoration and
  // notes it in the status bar — the base render stays intact.
  function analyticsPath(sub) {
    return "/api/v1/projects/" + encodeURIComponent(CFG.project) + "/graph/analytics/" + sub;
  }

  function setCentrality(on) {
    centralityOn = on;
    if (!on) { centralityScores = {}; restyleAll(); status("centrality off"); return; }
    if (graph.order === 0) { status("no graph to rank"); el("centrality-toggle").checked = false; centralityOn = false; return; }
    status("computing " + centralityMeasure + " centrality…");
    api(analyticsPath("centrality") + "?measure=" + encodeURIComponent(centralityMeasure))
      .then(function (d) {
        centralityScores = {};
        (d.scores || []).forEach(function (s) { centralityScores[s.key] = s.score; });
        restyleAll();
        renderHubs(d.scores || []);
        status("centrality (" + (d.measure || centralityMeasure) + ") on " + (d.count || 0) + " nodes");
      })
      .catch(function (err) {
        centralityOn = false; el("centrality-toggle").checked = false; restyleAll();
        status("centrality unavailable: " + err.message);
      });
  }

  function setCommunities(on) {
    communityOn = on;
    if (!on) { communityOf = {}; el("stat-communities").textContent = "—"; restyleAll(); status("communities off"); return; }
    if (graph.order === 0) { status("no graph to cluster"); el("community-toggle").checked = false; communityOn = false; return; }
    status("detecting communities…");
    api(analyticsPath("communities"))
      .then(function (d) {
        communityOf = {};
        (d.communities || []).forEach(function (c) {
          (c.members || []).forEach(function (m) { communityOf[m] = c.community_id; });
        });
        el("stat-communities").textContent = (d.count != null ? d.count : (d.communities || []).length);
        restyleAll();
        status((d.count || 0) + " communities detected");
      })
      .catch(function (err) {
        communityOn = false; el("community-toggle").checked = false; el("stat-communities").textContent = "—";
        restyleAll();
        status("communities unavailable: " + err.message);
      });
  }

  // Render the top-hubs list from a centrality response; each row flies to its
  // node on click. Refetches if centrality scores aren't already loaded.
  function renderHubs(scores) {
    var box = el("hubs-list");
    box.innerHTML = "";
    if (!scores || !scores.length) {
      box.innerHTML = '<li class="empty">no hubs (empty graph)</li>';
      return;
    }
    scores.slice(0, 10).forEach(function (s) {
      var li = document.createElement("li");
      var lbl = document.createElement("span");
      lbl.textContent = s.label || s.key;
      var sc = document.createElement("span");
      sc.className = "hub-score"; sc.textContent = fmtVal(s.score);
      li.appendChild(lbl); li.appendChild(sc);
      li.addEventListener("click", function () {
        if (graph.hasNode(s.key)) { flyTo(s.key); openDetail(s.key); }
        else { drillTo(s.key, s.label); }
      });
      box.appendChild(li);
    });
  }

  function loadHubs() {
    if (graph.order === 0) { renderHubs([]); status("no graph to rank"); return; }
    el("hubs-btn").disabled = true;
    status("ranking by " + centralityMeasure + "…");
    api(analyticsPath("centrality") + "?measure=" + encodeURIComponent(centralityMeasure) + "&top_n=10")
      .then(function (d) { renderHubs(d.scores || []); status("top hubs by " + (d.measure || centralityMeasure)); })
      .catch(function (err) { status("hubs unavailable: " + err.message); })
      .finally(function () { el("hubs-btn").disabled = false; });
  }

  // Resolve a user-typed node reference (id OR label) to a node id present in
  // the loaded graph. Exact id wins; otherwise a case-insensitive label match.
  function resolveNodeRef(ref) {
    var raw = (ref || "").trim();
    if (!raw) return null;
    if (graph.hasNode(raw)) return raw;
    var needle = raw.toLowerCase();
    var found = null;
    graph.forEachNode(function (node, attrs) {
      if (found) return;
      if (String(attrs.label || "").toLowerCase() === needle) found = node;
    });
    return found;
  }

  function runShortestPath() {
    var fromRef = el("path-from").value, toRef = el("path-to").value;
    var from = resolveNodeRef(fromRef), to = resolveNodeRef(toRef);
    var result = el("path-result");
    if (!from || !to) {
      result.className = "path-result miss";
      result.textContent = "enter two known nodes (id or label)";
      return;
    }
    el("path-btn").disabled = true;
    result.className = "path-result"; result.textContent = "finding path…";
    api(analyticsPath("path") + "?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to))
      .then(function (d) {
        if (!d.found || !d.nodes || !d.nodes.length) {
          clearPath();
          result.className = "path-result miss";
          result.textContent = "no path between these nodes";
          return;
        }
        highlightPath(d.nodes);
        result.className = "path-result ok";
        result.textContent = "path: " + d.hops + " hop" + (d.hops === 1 ? "" : "s");
      })
      .catch(function (err) {
        clearPath();
        result.className = "path-result miss";
        result.textContent = "path unavailable: " + err.message;
      })
      .finally(function () { el("path-btn").disabled = false; });
  }

  // Highlight a path (list of node ids) by recording the on-path node set + the
  // edge keys connecting consecutive nodes, then refreshing so the reducers
  // light them up. Edge keys are resolved from the graph (any edge between two
  // consecutive path nodes, either direction).
  function highlightPath(nodeIds) {
    pathSet = new Set(nodeIds);
    pathEdgeKeys = new Set();
    for (var i = 0; i + 1 < nodeIds.length; i++) {
      var a = nodeIds[i], b = nodeIds[i + 1];
      if (!graph.hasNode(a) || !graph.hasNode(b)) continue;
      graph.forEachEdge(a, function (edge, attrs, src, tgt) {
        if ((src === a && tgt === b) || (src === b && tgt === a)) pathEdgeKeys.add(edge);
      });
    }
    if (renderer) renderer.refresh();
    if (nodeIds.length && graph.hasNode(nodeIds[0])) flyTo(nodeIds[0]);
  }

  function clearPath() {
    pathSet = null; pathEdgeKeys = null;
    el("path-result").textContent = "";
    el("path-result").className = "path-result";
    if (renderer) renderer.refresh();
  }

  // ---- live growth (Server-Sent Events) -----------------------------------
  // Subscribes to the runtime's graph-growth SSE stream with ?live=true so
  // the server, after replaying the current graph, KEEPS THE CONNECTION OPEN
  // and pushes node.added / edge.added frames as KB ingest persists new
  // nodes/edges (ADR 046 D6). Each new element gets a brief highlight halo so
  // the eye catches the graph growing. The static viewer is independent of
  // this: the initial snapshot loads in boot(); if the stream is unavailable
  // (older runtime, network) the graph still renders and stays interactive.
  var HIGHLIGHT_MS = 2600;          // how long a freshly-added element glows

  function setLive(on) {
    el("live-toggle").parentElement.classList.toggle("on", on);
    if (on) {
      if (liveSource) return;
      // ?live=true engages the live-tail (without it the stream is a one-shot
      // snapshot). The {project} path param is the agent the snapshot used, so
      // the live-tail is scoped to the SAME graph the viewer is showing.
      // &pace= sleeps that many seconds between snapshot frames server-side so
      // the graph visibly ASSEMBLES node-by-node when the toggle flips — the
      // demo "watch it grow" moment even for an already-built graph.
      var url = "/api/v1/projects/" + encodeURIComponent(CFG.project) +
        "/graph/stream?live=true&pace=" + encodeURIComponent(LIVE_REPLAY_PACE_S);
      liveSource = new EventSource(url, { withCredentials: true });
      liveSource.addEventListener("node.added", function (ev) { onLiveNode(ev); });
      liveSource.addEventListener("edge.added", function (ev) { onLiveEdge(ev); });
      // Generic message fallback for runtimes that don't set an event name.
      liveSource.onmessage = function (ev) { onLiveGeneric(ev); };
      // Graceful degrade: a stream error never touches the already-rendered
      // static graph — we just note it. EventSource auto-reconnects; on
      // reconnect the server re-replays the snapshot to reconcile.
      liveSource.onerror = function () { status("live stream interrupted — retrying… (graph still interactive)"); };
      status("live growth ON — watching ingest for new nodes/edges");
    } else if (liveSource) {
      liveSource.close(); liveSource = null;
      status("live growth OFF");
    }
  }

  // Replay growth: clear the loaded graph, then open a PACED snapshot stream so
  // the graph visibly re-assembles node-by-node from the server — the demo's
  // "watch it grow" moment, decoupled from whether a real ingest is running.
  // Uses the snapshot stream (no ?live), which still honors ?pace and ends with
  // a `done` frame; each re-imported node/edge is genuinely new after the
  // clear, so the existing highlight-halo path fires. Read-only: it only
  // re-reads what the server already serves.
  var replaySource = null;
  function replayGrowth() {
    if (replaySource) { replaySource.close(); replaySource = null; }
    // Tearing the live-tail down first (if any) avoids two streams racing to
    // import the same keys; the operator can re-enable live afterward.
    if (liveSource) { liveSource.close(); liveSource = null; el("live-toggle").checked = false; el("live-toggle").parentElement.classList.remove("on"); }
    clearFocus();
    graph.clear();
    if (renderer) renderer.refresh();
    rebuildTypeFilters();
    updateStats();
    var url = "/api/v1/projects/" + encodeURIComponent(CFG.project) +
      "/graph/stream?pace=" + encodeURIComponent(LIVE_REPLAY_PACE_S);
    replaySource = new EventSource(url, { withCredentials: true });
    replaySource.addEventListener("node.added", function (ev) { onLiveNode(ev); });
    replaySource.addEventListener("edge.added", function (ev) { onLiveEdge(ev); });
    replaySource.addEventListener("done", function () {
      if (replaySource) { replaySource.close(); replaySource = null; }
      startLayout(2500);
      status("replay complete — " + graph.order + " nodes");
    });
    replaySource.onerror = function () {
      // A snapshot stream closes the connection after `done`; some browsers
      // surface that as an error. Only warn if we never finished assembling.
      if (replaySource && graph.order === 0) status("replay stream unavailable (graph still interactive)");
    };
    status("replaying growth…");
  }

  function parseEvent(ev) { try { return JSON.parse(ev.data); } catch (_) { return null; } }

  function onLiveNode(ev) {
    var n = parseEvent(ev); if (!n) return;
    var before = graph.order;
    var res = importGraph({ nodes: [n], edges: [] });
    if (graph.order > before) {
      highlightNew(res.addedNodes, []);
      nudgeLayout();
      status("+ node " + (n.attributes && n.attributes.label || n.key || n.id));
    }
  }
  function onLiveEdge(ev) {
    var e = parseEvent(ev); if (!e) return;
    var before = graph.size;
    var res = importGraph({ nodes: [], edges: [e] });
    if (graph.size > before) {
      highlightNew([], res.addedEdges);
      nudgeLayout();
      status("+ edge");
    }
  }
  function onLiveGeneric(ev) {
    var payload = parseEvent(ev); if (!payload) return;
    if (payload.nodes || payload.edges) {
      var res = importGraph(payload);
      highlightNew(res.addedNodes, res.addedEdges);
      nudgeLayout();
    }
  }

  // Briefly flag freshly-arrived elements so the node/edge reducers render
  // them with a glow halo, then clear the flag so they settle into the graph.
  // Purely cosmetic + transient — the `_new` attribute is never persisted or
  // serialized, and clearing it can't fail the live loop.
  function highlightNew(nodes, edges) {
    (nodes || []).forEach(function (key) {
      if (graph.hasNode(key)) graph.setNodeAttribute(key, "_new", true);
    });
    (edges || []).forEach(function (key) {
      if (graph.hasEdge(key)) graph.setEdgeAttribute(key, "_new", true);
    });
    if ((nodes && nodes.length) || (edges && edges.length)) {
      if (renderer) renderer.refresh();
      setTimeout(function () {
        (nodes || []).forEach(function (key) {
          if (graph.hasNode(key)) graph.removeNodeAttribute(key, "_new");
        });
        (edges || []).forEach(function (key) {
          if (graph.hasEdge(key)) graph.removeEdgeAttribute(key, "_new");
        });
        if (renderer) renderer.refresh();
      }, HIGHLIGHT_MS);
    }
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
    el("min-confidence").addEventListener("input", function (e) {
      minConfidence = parseFloat(e.target.value) || 0;
      el("min-confidence-val").textContent = minConfidence.toFixed(2);
      // Pure viewer-side dimming: just refresh the reducers + the stat count.
      // No re-fetch, no data mutation — dragging back to 0 fully restores.
      if (renderer) renderer.refresh();
      updateStats();
    });
    el("replay-btn").addEventListener("click", function () { replayGrowth(); });
    el("live-toggle").addEventListener("change", function (e) { setLive(e.target.checked); });
    el("detail-close").addEventListener("click", function () {
      detailPanel.classList.add("hidden"); detailPanel.setAttribute("aria-hidden", "true");
    });
    el("expand-btn").addEventListener("click", function () {
      var node = el("expand-btn").dataset.node; if (node) expand(node);
    });

    // ---- analytics controls (ADR 046) ----
    el("centrality-toggle").addEventListener("change", function (e) { setCentrality(e.target.checked); });
    el("centrality-measure").addEventListener("change", function (e) {
      centralityMeasure = e.target.value;
      if (centralityOn) setCentrality(true);   // recompute under the new measure
      else loadHubs();                          // keep the hubs list in step
    });
    el("community-toggle").addEventListener("change", function (e) { setCommunities(e.target.checked); });
    el("hubs-btn").addEventListener("click", function () { loadHubs(); });
    el("path-btn").addEventListener("click", function () { runShortestPath(); });
    el("path-clear").addEventListener("click", function () { clearPath(); });
    // Enter in either path input runs the search.
    ["path-from", "path-to"].forEach(function (id) {
      el(id).addEventListener("keydown", function (e) { if (e.key === "Enter") runShortestPath(); });
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
