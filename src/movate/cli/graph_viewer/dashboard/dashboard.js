/*
 * mdk graph dashboard — hosted knowledge graph explorer.
 *
 * Extends the base mdk graph viewer (app.js) with:
 *   (a) Entity search — full-text search via the runtime's graph/search API,
 *       highlighting matching nodes in the graph. Click a result to focus.
 *   (b) Analytics sidebar — centrality leaderboard, shortest-path finder,
 *       community browser with color swatches.
 *   (c) Growth timeline — sparkline at the bottom showing entity/relation
 *       additions over time, rendered on a canvas element.
 *   (d) Project switcher — dropdown to switch between projects, reloading
 *       the graph on selection.
 *   (e) KB provenance view — on node drill-down, shows which source documents
 *       contributed an entity (inherits from existing detail panel).
 *   (f) Visual polish — Movate brand colors, dark-mode, smooth animations,
 *       watermark, responsive layout.
 *
 * Talks ONLY to the local proxy at /api/* (same origin). The bearer key
 * NEVER appears in this file, in the page, or in any browser-visible header.
 * This viewer is strictly read-only.
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
  var fa2Layout = null;
  var selectedNode = null;
  var hoveredNeighbors = null;
  var disabledTypes = new Set();
  var colorMode = "type";
  var sizeByDegree = true;
  var liveSource = null;
  var palette = {};
  var paletteIdx = 0;
  var minConfidence = 0;
  var LIVE_REPLAY_PACE_S = 0.12;

  // ---- analytics state (ADR 046) -----------------------------------------
  var centralityOn = false;
  var centralityMeasure = "degree";
  var centralityScores = {};
  var communityOn = false;
  var communityOf = {};
  var pathSet = null;
  var pathEdgeKeys = null;

  // ---- dashboard-specific state -------------------------------------------
  var searchDebounce = null;
  var highlightedSearchNodes = new Set();
  var communityData = [];     // cached community list for the sidebar
  var activeCommunityId = null;
  var growthHistory = [];     // [{date, nodes, edges}, ...] for sparkline

  // ---- Movate brand palette (from dashboards/grafana/theme/palette.json) --
  var COLORS = [
    "#5BC0EB", "#2BB673", "#F2A93B", "#D64550", "#2D6CDF",
    "#7EE787", "#BC8CFF", "#F0883E", "#56D4DD", "#A5D6FF",
    "#FF9492", "#D2A8FF", "#79C0FF", "#FFA657", "#AFF5B4"
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

  function centralityColor(score) {
    var s = Math.max(0, Math.min(1, score || 0));
    var stops = ["#1B3A8C", "#2D6CDF", "#5BC0EB", "#F2A93B", "#D64550"];
    var t = s * (stops.length - 1);
    return stops[Math.round(t)];
  }

  function confidenceOf(node) {
    var c = graph.getNodeAttribute(node, "confidence");
    return (typeof c === "number") ? c : null;
  }

  function isLowConfidence(node) {
    if (minConfidence <= 0) return false;
    var c = confidenceOf(node);
    return c !== null && c < minConfidence;
  }

  function styleNode(node, attrs) {
    var degree = graph.degree(node);
    var size, color;
    if (centralityOn) {
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
          var merge = Object.assign({}, n.attributes || {});
          if (merge.kind == null && merge.type != null) merge.kind = merge.type;
          delete merge.type;
          graph.mergeNodeAttributes(key, merge);
        }
        return;
      }
      var a = Object.assign({}, n.attributes || {});
      if (a.label == null) a.label = String(key);
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
      try {
        var key = (e.key != null && !graph.hasEdge(e.key))
          ? graph.addEdgeWithKey(e.key, s, t, Object.assign({ size: 1, color: "#2A3340" }, e.attributes || {}))
          : graph.addEdge(s, t, Object.assign({ size: 1, color: "#2A3340" }, e.attributes || {}));
        addedEdges.push(key);
      } catch (_) { /* parallel/dup edge */ }
    }

    (data.nodes || []).forEach(ingestNode);
    (data.edges || []).forEach(ingestEdge);

    graph.forEachNode(function (node, attrs) { styleNode(node, attrs); });
    rebuildTypeFilters();
    updateStats();
    recordGrowthSnapshot();

    return { hadPositions: hadPositions, addedNodes: addedNodes, addedEdges: addedEdges };
  }

  // ---- ForceAtlas2 layout (web worker) ------------------------------------
  function startLayout(transientMs) {
    if (graph.order === 0) return;
    if (!fa2Layout) {
      var settings = FA2.inferSettings(graph);
      fa2Layout = new FA2.FA2Layout(graph, { settings: settings });
    }
    fa2Layout.start();
    status("laying out " + graph.order + " nodes…");
    if (transientMs) {
      setTimeout(function () { if (fa2Layout) fa2Layout.stop(); status("layout settled"); }, transientMs);
    }
  }

  function nudgeLayout() {
    if (!fa2Layout) { startLayout(2500); return; }
    fa2Layout.start();
    setTimeout(function () { if (fa2Layout) fa2Layout.stop(); }, 2200);
  }

  // ---- sigma renderer + reducers ------------------------------------------
  function render() {
    renderer = new Sigma(graph, container, {
      defaultEdgeColor: "#2A3340",
      labelColor: { color: "#E6EAF0" },
      labelDensity: 0.6,
      labelGridCellSize: 80,
      renderEdgeLabels: false,
      minCameraRatio: 0.05,
      maxCameraRatio: 14
    });

    renderer.setSetting("nodeReducer", function (node, data) {
      var res = Object.assign({}, data);
      var kind = graph.getNodeAttribute(node, "kind");
      if (disabledTypes.has(kind)) { res.hidden = true; return res; }

      // Confidence dimming
      if (isLowConfidence(node)) {
        res.color = "#3a4048";
        res.label = "";
        res.size = (res.size || 4) * 0.6;
        res.zIndex = 0;
      }

      // Community filter (from sidebar)
      if (activeCommunityId !== null && communityOf[node] !== activeCommunityId) {
        res.color = "#2a3038";
        res.label = "";
        res.size = (res.size || 4) * 0.5;
        res.zIndex = 0;
      }

      // Search highlight
      if (highlightedSearchNodes.size > 0) {
        if (highlightedSearchNodes.has(node)) {
          res.color = "#5BC0EB";
          res.highlighted = true;
          res.size = (res.size || 4) * 1.4;
          res.zIndex = 3;
        } else if (!selectedNode) {
          res.color = "#2a3038";
          res.label = "";
          res.zIndex = 0;
        }
      }

      if (selectedNode) {
        if (node === selectedNode) {
          res.highlighted = true;
          res.zIndex = 2;
        } else if (hoveredNeighbors && hoveredNeighbors.has(node)) {
          res.zIndex = 1;
        } else if (!highlightedSearchNodes.has(node)) {
          res.color = "#2a3038";
          res.label = "";
          res.zIndex = 0;
        }
      }

      // Shortest-path highlight
      if (pathSet) {
        if (pathSet.has(node)) {
          res.color = "#F2A93B";
          res.highlighted = true;
          res.size = (res.size || 4) * 1.3;
          res.zIndex = 4;
        } else {
          res.color = "#2a3038";
          res.label = "";
          res.zIndex = 0;
        }
      }

      // Live-growth halo
      if (graph.getNodeAttribute(node, "_new")) {
        res.color = "#2BB673";
        res.highlighted = true;
        res.size = (res.size || 4) * 1.6;
        res.zIndex = 3;
      }
      return res;
    });

    renderer.setSetting("edgeReducer", function (edge, data) {
      var res = Object.assign({}, data);
      var s = graph.source(edge), t = graph.target(edge);
      if (disabledTypes.has(graph.getNodeAttribute(s, "kind")) ||
          disabledTypes.has(graph.getNodeAttribute(t, "kind"))) {
        res.hidden = true; return res;
      }
      if (isLowConfidence(s) || isLowConfidence(t)) {
        res.color = "#1A2129"; res.zIndex = 0;
      }
      if (activeCommunityId !== null) {
        if (communityOf[s] !== activeCommunityId || communityOf[t] !== activeCommunityId) {
          res.hidden = true;
        }
      }
      if (selectedNode) {
        if (s === selectedNode || t === selectedNode) {
          res.color = "#5BC0EB"; res.zIndex = 1;
        } else { res.hidden = true; }
      }
      if (pathEdgeKeys) {
        if (pathEdgeKeys.has(edge)) {
          res.hidden = false; res.color = "#F2A93B"; res.size = (res.size || 1) + 2; res.zIndex = 4;
        } else if (!selectedNode) {
          res.hidden = true;
        }
      }
      if (graph.getEdgeAttribute(edge, "_new")) {
        res.color = "#2BB673"; res.size = (res.size || 1) + 1.5; res.zIndex = 3;
      }
      return res;
    });

    renderer.on("clickNode", function (e) { focusNode(e.node); openDetail(e.node); });
    renderer.on("clickStage", function () { clearFocus(); clearSearchHighlight(); });
    renderer.on("doubleClickNode", function (e) {
      e.preventSigmaDefault();
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

  // ---- detail panel (drill-down) with KB provenance -----------------------
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

    // KB provenance view (enhanced with card-style layout)
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
            (p.chunk || p.text || p.snippet
              ? '<span class="chunk">' + escapeHtml(p.chunk || p.text || p.snippet) + "</span>"
              : "") +
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
  function renderRelations(node, neighbors) {
    var box = el("detail-relations");
    box.innerHTML = "";
    if (!neighbors.length) {
      box.innerHTML = '<div class="empty">no connected entities</div>';
      return;
    }
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
        openDetail(key);
      });
  }

  // ---- expand: fetch + import neighbors -----------------------------------
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

  // ---- (a) Entity search — full-text via runtime API ----------------------
  var sugIndex = -1;

  function runSearch(q) {
    suggestions.innerHTML = "";
    sugIndex = -1;
    var needle = q.trim();
    if (!needle) {
      suggestions.classList.remove("open");
      clearSearchHighlight();
      return;
    }

    // First, local search (instant, for already-loaded nodes)
    var localMatches = [];
    var needleLower = needle.toLowerCase();
    graph.forEachNode(function (node, attrs) {
      var label = String(attrs.label || node);
      if (label.toLowerCase().indexOf(needleLower) !== -1) {
        localMatches.push({ node: node, label: label, type: attrs.kind || "" });
      }
    });

    // Debounced API search for full-text across the entire graph
    if (searchDebounce) clearTimeout(searchDebounce);
    searchDebounce = setTimeout(function () {
      if (!CFG.project) return;
      api("/api/v1/projects/" + encodeURIComponent(CFG.project) +
          "/graph/search?q=" + encodeURIComponent(needle))
        .then(function (hits) {
          if (!Array.isArray(hits)) hits = hits.hits || hits.results || [];
          // Merge API results with local, deduplicating by node key
          var seen = new Set(localMatches.map(function (m) { return m.node; }));
          hits.forEach(function (h) {
            var key = h.key || h.id;
            if (key && !seen.has(key)) {
              localMatches.push({ node: key, label: h.label || key, type: h.type || "" });
              seen.add(key);
            }
          });
          renderSearchResults(localMatches, needle);
        })
        .catch(function () {
          // API search unavailable; show local results only
          renderSearchResults(localMatches, needle);
        });
    }, 250);

    // Show local results immediately
    renderSearchResults(localMatches, needle);
  }

  function renderSearchResults(matches, query) {
    suggestions.innerHTML = "";
    sugIndex = -1;
    matches.sort(function (a, b) { return a.label.length - b.label.length; });
    var shown = matches.slice(0, 15);

    // Highlight matching nodes in the graph
    highlightedSearchNodes = new Set(shown.map(function (m) { return m.node; }));
    if (renderer) renderer.refresh();

    shown.forEach(function (m) {
      var li = document.createElement("li");
      li.textContent = m.label;
      if (m.type) {
        var s = document.createElement("span");
        s.className = "stype";
        s.textContent = m.type;
        li.appendChild(s);
      }
      li.addEventListener("mousedown", function (ev) {
        ev.preventDefault();
        pickSuggestion(m.node, m.label);
      });
      suggestions.appendChild(li);
    });
    suggestions.classList.toggle("open", shown.length > 0);
  }

  function pickSuggestion(node, label) {
    searchInput.value = label;
    suggestions.classList.remove("open");
    highlightedSearchNodes.clear();
    if (graph.hasNode(node)) {
      flyTo(node);
      openDetail(node);
    } else {
      drillTo(node, label);
    }
  }

  function clearSearchHighlight() {
    highlightedSearchNodes.clear();
    if (renderer) renderer.refresh();
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
      if (isLowConfidence(node)) return;
      visN++;
    });
    el("stat-nodes").textContent = visN;
    el("stat-edges").textContent = graph.size;
  }

  // ---- analytics (centrality / communities / shortest path) ---------------
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
        renderLeaderboard(d.scores || []);
        status("centrality (" + (d.measure || centralityMeasure) + ") on " + (d.count || 0) + " nodes");
      })
      .catch(function (err) {
        centralityOn = false; el("centrality-toggle").checked = false; restyleAll();
        status("centrality unavailable: " + err.message);
      });
  }

  function setCommunities(on) {
    communityOn = on;
    if (!on) {
      communityOf = {};
      communityData = [];
      el("stat-communities").textContent = "—";
      renderCommunityList([]);
      restyleAll();
      status("communities off");
      return;
    }
    if (graph.order === 0) { status("no graph to cluster"); el("community-toggle").checked = false; communityOn = false; return; }
    status("detecting communities…");
    api(analyticsPath("communities"))
      .then(function (d) {
        communityOf = {};
        communityData = d.communities || [];
        communityData.forEach(function (c) {
          (c.members || []).forEach(function (m) { communityOf[m] = c.community_id; });
        });
        el("stat-communities").textContent = (d.count != null ? d.count : communityData.length);
        renderCommunityList(communityData);
        restyleAll();
        status((d.count || 0) + " communities detected");
      })
      .catch(function (err) {
        communityOn = false; el("community-toggle").checked = false; el("stat-communities").textContent = "—";
        communityData = [];
        renderCommunityList([]);
        restyleAll();
        status("communities unavailable: " + err.message);
      });
  }

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

  function runShortestPath(fromEl, toEl, resultEl) {
    var fromRef = fromEl.value, toRef = toEl.value;
    var from = resolveNodeRef(fromRef), to = resolveNodeRef(toRef);
    if (!from || !to) {
      resultEl.className = "path-result miss";
      resultEl.textContent = "enter two known nodes (id or label)";
      return;
    }
    status("finding path…");
    resultEl.className = "path-result"; resultEl.textContent = "finding path…";
    api(analyticsPath("path") + "?from=" + encodeURIComponent(from) + "&to=" + encodeURIComponent(to))
      .then(function (d) {
        if (!d.found || !d.nodes || !d.nodes.length) {
          clearPath();
          resultEl.className = "path-result miss";
          resultEl.textContent = "no path between these nodes";
          return;
        }
        highlightPath(d.nodes);
        resultEl.className = "path-result ok";
        resultEl.textContent = "path: " + d.hops + " hop" + (d.hops === 1 ? "" : "s");
      })
      .catch(function (err) {
        clearPath();
        resultEl.className = "path-result miss";
        resultEl.textContent = "path unavailable: " + err.message;
      });
  }

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
    el("sidebar-path-result").textContent = "";
    el("sidebar-path-result").className = "path-result";
    if (renderer) renderer.refresh();
  }

  // ---- (b) Analytics sidebar: centrality leaderboard ----------------------
  function renderLeaderboard(scores) {
    var box = el("leaderboard-list");
    box.innerHTML = "";
    if (!scores || !scores.length) {
      var emptyLi = document.createElement("li");
      emptyLi.className = "empty";
      emptyLi.textContent = "load centrality to see the leaderboard";
      emptyLi.style.listStyle = "none";
      emptyLi.style.color = "#8A94A6";
      emptyLi.style.fontStyle = "italic";
      box.appendChild(emptyLi);
      return;
    }
    var maxScore = scores.length ? scores[0].score : 1;
    scores.slice(0, 10).forEach(function (s, i) {
      var li = document.createElement("li");
      var rank = document.createElement("span");
      rank.className = "lb-rank";
      rank.textContent = (i + 1) + ".";
      var lbl = document.createElement("span");
      lbl.className = "lb-label";
      lbl.textContent = s.label || s.key;
      lbl.title = s.key;
      var sc = document.createElement("span");
      sc.className = "lb-score";
      sc.textContent = fmtVal(s.score);
      var bar = document.createElement("span");
      bar.className = "lb-bar";
      var fill = document.createElement("span");
      fill.className = "lb-bar-fill";
      var pct = maxScore > 0 ? (s.score / maxScore) * 100 : 0;
      fill.style.width = pct + "%";
      fill.style.background = centralityColor(s.score);
      bar.appendChild(fill);
      li.appendChild(rank);
      li.appendChild(lbl);
      li.appendChild(sc);
      li.appendChild(bar);
      li.addEventListener("click", function () {
        if (graph.hasNode(s.key)) { flyTo(s.key); openDetail(s.key); }
        else { drillTo(s.key, s.label); }
      });
      box.appendChild(li);
    });
  }

  function loadLeaderboard() {
    if (graph.order === 0) { renderLeaderboard([]); return; }
    var measure = el("leaderboard-measure").value;
    el("leaderboard-refresh").disabled = true;
    api(analyticsPath("centrality") + "?measure=" + encodeURIComponent(measure) + "&top_n=10")
      .then(function (d) { renderLeaderboard(d.scores || []); })
      .catch(function (err) { status("leaderboard unavailable: " + err.message); renderLeaderboard([]); })
      .finally(function () { el("leaderboard-refresh").disabled = false; });
  }

  // ---- (b) Analytics sidebar: community browser ---------------------------
  function renderCommunityList(communities) {
    var box = el("community-list");
    var clearBtn = el("community-clear");
    box.innerHTML = "";
    if (!communities || !communities.length) {
      box.innerHTML = '<div class="empty" style="padding:4px 6px;font-size:12px;">enable communities to browse</div>';
      clearBtn.style.display = "none";
      return;
    }
    communities.forEach(function (c) {
      var item = document.createElement("div");
      item.className = "community-item" + (activeCommunityId === c.community_id ? " active" : "");
      var swatch = document.createElement("span");
      swatch.className = "community-swatch";
      swatch.style.background = colorFor("community:" + c.community_id);
      var label = document.createElement("span");
      label.className = "community-label";
      label.textContent = "Community " + c.community_id;
      var count = document.createElement("span");
      count.className = "community-count";
      count.textContent = (c.members || []).length + " nodes";
      item.appendChild(swatch);
      item.appendChild(label);
      item.appendChild(count);
      item.addEventListener("click", function () {
        if (activeCommunityId === c.community_id) {
          activeCommunityId = null;
        } else {
          activeCommunityId = c.community_id;
        }
        renderCommunityList(communityData);
        if (renderer) renderer.refresh();
        clearBtn.style.display = activeCommunityId !== null ? "block" : "none";
      });
      box.appendChild(item);
    });
    clearBtn.style.display = activeCommunityId !== null ? "block" : "none";
  }

  // ---- (c) Growth timeline sparkline --------------------------------------
  function recordGrowthSnapshot() {
    var now = new Date();
    var dateStr = now.toISOString().slice(0, 10);
    var last = growthHistory.length ? growthHistory[growthHistory.length - 1] : null;
    if (last && last.date === dateStr) {
      last.nodes = graph.order;
      last.edges = graph.size;
    } else {
      growthHistory.push({ date: dateStr, nodes: graph.order, edges: graph.size });
    }
    if (growthHistory.length > 60) growthHistory = growthHistory.slice(-60);
    drawSparkline();
  }

  function drawSparkline() {
    var canvas = el("sparkline-canvas");
    if (!canvas) return;
    var ctx = canvas.getContext("2d");
    var w = canvas.parentElement.offsetWidth - 120;
    if (w < 40) w = 40;
    canvas.width = w;
    canvas.height = 40;
    ctx.clearRect(0, 0, w, 40);

    var data = growthHistory;
    if (data.length < 2) {
      // Not enough data; show a placeholder line
      ctx.strokeStyle = "#2A3340";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(0, 20);
      ctx.lineTo(w, 20);
      ctx.stroke();
      el("sparkline-label").textContent = data.length ? (data[0].nodes + " nodes") : "no data";
      return;
    }

    var maxN = 1;
    data.forEach(function (d) { if (d.nodes > maxN) maxN = d.nodes; });

    // Draw area fill
    var step = w / (data.length - 1);
    ctx.beginPath();
    ctx.moveTo(0, 40);
    data.forEach(function (d, i) {
      var x = i * step;
      var y = 38 - (d.nodes / maxN) * 34;
      if (i === 0) ctx.lineTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.lineTo(w, 40);
    ctx.closePath();
    ctx.fillStyle = "rgba(45, 108, 223, 0.15)";
    ctx.fill();

    // Draw line
    ctx.beginPath();
    data.forEach(function (d, i) {
      var x = i * step;
      var y = 38 - (d.nodes / maxN) * 34;
      if (i === 0) ctx.moveTo(x, y);
      else ctx.lineTo(x, y);
    });
    ctx.strokeStyle = "#5BC0EB";
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // Dot on the latest point
    var lastPt = data[data.length - 1];
    var lx = (data.length - 1) * step;
    var ly = 38 - (lastPt.nodes / maxN) * 34;
    ctx.beginPath();
    ctx.arc(lx, ly, 3, 0, Math.PI * 2);
    ctx.fillStyle = "#5BC0EB";
    ctx.fill();

    el("sparkline-label").textContent = lastPt.nodes + " nodes, " + lastPt.edges + " edges";
  }

  // ---- (d) Project switcher -----------------------------------------------
  function loadProjects() {
    api("/api/v1/projects")
      .then(function (data) {
        var projects = Array.isArray(data) ? data : (data.projects || data.items || []);
        var select = el("project-select");
        select.innerHTML = "";
        if (!projects.length) {
          var opt = document.createElement("option");
          opt.value = CFG.project || "";
          opt.textContent = CFG.project || "(no projects)";
          select.appendChild(opt);
          return;
        }
        projects.forEach(function (p) {
          var opt = document.createElement("option");
          var pid = typeof p === "string" ? p : (p.id || p.project_id || p.name);
          opt.value = pid;
          opt.textContent = pid;
          if (pid === CFG.project) opt.selected = true;
          select.appendChild(opt);
        });
      })
      .catch(function () {
        // Project list unavailable; show current project only
        var select = el("project-select");
        select.innerHTML = "";
        var opt = document.createElement("option");
        opt.value = CFG.project || "";
        opt.textContent = CFG.project || "(current)";
        select.appendChild(opt);
      });
  }

  function switchProject(projectId) {
    if (!projectId || projectId === CFG.project) return;
    CFG.project = projectId;
    el("target-badge").textContent = CFG.target ? CFG.target + " / " + projectId : projectId;

    // Clear and reload
    if (liveSource) { liveSource.close(); liveSource = null; el("live-toggle").checked = false; }
    clearFocus();
    clearPath();
    clearSearchHighlight();
    communityOf = {};
    communityData = [];
    centralityScores = {};
    activeCommunityId = null;
    growthHistory = [];
    if (fa2Layout) { fa2Layout.kill(); fa2Layout = null; }
    graph.clear();
    if (renderer) { renderer.kill(); renderer = null; }

    status("loading graph for " + projectId + "…");
    api("/api/v1/projects/" + encodeURIComponent(projectId) + "/graph?mode=knowledge")
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
        showBanner("Failed to load graph for " + escapeHtml(projectId) + ": " + escapeHtml(err.message));
      });
  }

  // ---- live growth (Server-Sent Events) -----------------------------------
  var HIGHLIGHT_MS = 2600;

  function setLive(on) {
    el("live-toggle").parentElement.classList.toggle("on", on);
    if (on) {
      if (liveSource) return;
      var url = "/api/v1/projects/" + encodeURIComponent(CFG.project) +
        "/graph/stream?live=true&pace=" + encodeURIComponent(LIVE_REPLAY_PACE_S);
      liveSource = new EventSource(url, { withCredentials: true });
      liveSource.addEventListener("node.added", function (ev) { onLiveNode(ev); });
      liveSource.addEventListener("edge.added", function (ev) { onLiveEdge(ev); });
      liveSource.onmessage = function (ev) { onLiveGeneric(ev); };
      liveSource.onerror = function () { status("live stream interrupted — retrying… (graph still interactive)"); };
      status("live growth ON — watching ingest for new nodes/edges");
    } else if (liveSource) {
      liveSource.close(); liveSource = null;
      status("live growth OFF");
    }
  }

  var replaySource = null;
  function replayGrowth() {
    if (replaySource) { replaySource.close(); replaySource = null; }
    if (liveSource) { liveSource.close(); liveSource = null; el("live-toggle").checked = false; el("live-toggle").parentElement.classList.remove("on"); }
    clearFocus();
    graph.clear();
    if (renderer) renderer.refresh();
    rebuildTypeFilters();
    updateStats();
    growthHistory = [];
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
    // Search
    searchInput.addEventListener("input", function () { runSearch(searchInput.value); });
    searchInput.addEventListener("keydown", function (e) {
      var items = suggestions.querySelectorAll("li");
      if (e.key === "ArrowDown") { e.preventDefault(); sugIndex = Math.min(sugIndex + 1, items.length - 1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); sugIndex = Math.max(sugIndex - 1, 0); }
      else if (e.key === "Enter") {
        if (items.length) { e.preventDefault(); var i = sugIndex >= 0 ? sugIndex : 0; items[i].dispatchEvent(new MouseEvent("mousedown")); }
        return;
      } else if (e.key === "Escape") { suggestions.classList.remove("open"); clearSearchHighlight(); return; }
      items.forEach(function (li, i) { li.classList.toggle("active", i === sugIndex); });
    });
    document.addEventListener("click", function (e) {
      if (!suggestions.contains(e.target) && e.target !== searchInput) {
        suggestions.classList.remove("open");
      }
    });

    // Color mode / size / confidence
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
      if (renderer) renderer.refresh();
      updateStats();
    });
    el("replay-btn").addEventListener("click", function () { replayGrowth(); });
    el("live-toggle").addEventListener("change", function (e) { setLive(e.target.checked); });

    // Detail panel
    el("detail-close").addEventListener("click", function () {
      detailPanel.classList.add("hidden"); detailPanel.setAttribute("aria-hidden", "true");
    });
    el("expand-btn").addEventListener("click", function () {
      var node = el("expand-btn").dataset.node; if (node) expand(node);
    });

    // Left sidebar analytics controls
    el("centrality-toggle").addEventListener("change", function (e) { setCentrality(e.target.checked); });
    el("centrality-measure").addEventListener("change", function (e) {
      centralityMeasure = e.target.value;
      if (centralityOn) setCentrality(true);
      else loadHubs();
    });
    el("community-toggle").addEventListener("change", function (e) { setCommunities(e.target.checked); });
    el("hubs-btn").addEventListener("click", function () { loadHubs(); });
    el("path-btn").addEventListener("click", function () {
      runShortestPath(el("path-from"), el("path-to"), el("path-result"));
    });
    el("path-clear").addEventListener("click", function () { clearPath(); });
    ["path-from", "path-to"].forEach(function (id) {
      el(id).addEventListener("keydown", function (e) {
        if (e.key === "Enter") runShortestPath(el("path-from"), el("path-to"), el("path-result"));
      });
    });

    // Right sidebar: analytics
    el("analytics-sidebar-toggle").addEventListener("click", function () {
      document.getElementById("app").classList.toggle("sidebar-collapsed");
      if (renderer) setTimeout(function () { renderer.refresh(); }, 250);
    });

    el("leaderboard-refresh").addEventListener("click", function () { loadLeaderboard(); });
    el("leaderboard-measure").addEventListener("change", function () { loadLeaderboard(); });

    el("sidebar-path-btn").addEventListener("click", function () {
      runShortestPath(el("sidebar-path-from"), el("sidebar-path-to"), el("sidebar-path-result"));
    });
    el("sidebar-path-clear").addEventListener("click", function () { clearPath(); });
    ["sidebar-path-from", "sidebar-path-to"].forEach(function (id) {
      el(id).addEventListener("keydown", function (e) {
        if (e.key === "Enter") runShortestPath(el("sidebar-path-from"), el("sidebar-path-to"), el("sidebar-path-result"));
      });
    });

    el("community-clear").addEventListener("click", function () {
      activeCommunityId = null;
      renderCommunityList(communityData);
      if (renderer) renderer.refresh();
    });

    // Project switcher
    el("project-select").addEventListener("change", function (e) {
      switchProject(e.target.value);
    });

    // Resize sparkline on window resize
    window.addEventListener("resize", function () { drawSparkline(); });
  }

  // ---- bootstrap ----------------------------------------------------------
  function boot() {
    wireControls();
    loadProjects();

    if (!CFG.project) {
      showBanner("No project id configured. Re-run <code>mdk graph dashboard --target &lt;env&gt; --project &lt;id&gt;</code>.");
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
        // Auto-load leaderboard on boot
        loadLeaderboard();
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
