(function () {
  // API base: same-origin in production (single-port FastAPI mounts web/ at /),
  // explicit localhost:8000 when the frontend is being served separately on
  // :8080 by serve_static.py during local dev.
  const API_BASE = (location.port === "8080" || location.protocol === "file:")
    ? "http://localhost:8000"
    : "";
  const REFRESH_MS = 5 * 60 * 1000;

  let map, markersByReach = {}, reachGroup = null;
  let speciesById = null;  // populated once from /calendar — used to render full pattern lists per active species
  let scrubGrid = null;    // { hours: [...], scores: { reach_id: [...] } } for the time scrubber
  let scrubOffset = 0;     // hour index into scrubGrid.hours; 0 = "now"
  let residualsByReach = {}; // { reach_id: { residual: -.20..+.20, n: int } } from /residuals
  const REPORTER_PROFILE_KEY = "driftless-cast-reporter-profile";
  const TOPWATER_LABELS = ["No topwater", "A few rises", "Steady eats", "Topwater popping"];

  function loadReporterProfile() {
    try {
      return JSON.parse(localStorage.getItem(REPORTER_PROFILE_KEY) || "{}");
    } catch {
      return {};
    }
  }

  function saveReporterProfile(profile) {
    localStorage.setItem(REPORTER_PROFILE_KEY, JSON.stringify(profile || {}));
  }

  function topwaterLabel(level) {
    const idx = Number(level);
    return Number.isInteger(idx) && idx >= 0 && idx < TOPWATER_LABELS.length ? TOPWATER_LABELS[idx] : null;
  }

  function formatSessionWindow(value) {
    if (!value) return null;
    return value.charAt(0).toUpperCase() + value.slice(1);
  }

  async function ensureSpeciesCache() {
    if (speciesById) return;
    try {
      const resp = await fetch(`${API_BASE}/calendar`);
      const data = await resp.json();
      speciesById = {};
      (data.species || []).forEach((sp) => { speciesById[sp.species_id] = sp; });
    } catch {
      speciesById = {};
    }
  }

  // Colorblind-safe diverging ramp (ColorBrewer RdYlGn, 5-step). Luminance
  // varies monotonically with score so deuteranopic / protanopic readers can
  // still rank by lightness alone. Pair with line-weight encoding for redundancy.
  const SCORE_STOPS = [
    { t: 0.00, c: [199,  35,  44] },   // dark red       — Skip
    { t: 0.30, c: [241, 138,  62] },   // orange         — Marginal
    { t: 0.55, c: [255, 220, 110] },   // soft yellow    — Fair
    { t: 0.75, c: [149, 199, 110] },   // light green    — Worth it
    { t: 1.00, c: [ 30, 132,  73] },   // dark green     — Go
  ];

  // Discrete verdict palette — the same five classes as the daily verdict
  // (trip_verdict.py thresholds) so map, chips and day strip all agree.
  const VERDICT = {
    go:     { label: "Go",       color: "#1a7f4b" },
    good:   { label: "Worth it", color: "#5fa04a" },
    slow:   { label: "Slow",     color: "#d9a521" },
    skip:   { label: "Skip",     color: "#c4442f" },
    closed: { label: "Closed",   color: "#8a9893" },
  };

  function verdictKey(score, closed) {
    if (closed) return "closed";
    if (score == null) return "closed";
    if (score >= 0.70) return "go";
    if (score >= 0.50) return "good";
    if (score > 0.30) return "slow";
    return "skip";
  }

  function vchip(score, closed, text) {
    const k = verdictKey(score, closed);
    return `<span class="vchip v-${k}">${escapeHtml(text || VERDICT[k].label)}</span>`;
  }

  function scoreColor(score) {
    if (score == null) return VERDICT.closed.color;
    return VERDICT[verdictKey(score, false)].color;
  }


  // Redundant encoding: line/marker thickness grows with score so the map
  // still ranks correctly when printed grayscale or read by colorblind users.
  // Tuned to be visible against the busy USGS Topo basemap at zoom 8 (the
  // regional default); thin lines disappeared into the topographic shading.
  function scoreWeight(score) {
    if (score == null) return 3;
    return 4 + Math.round(Math.max(0, Math.min(1, score)) * 2);  // 4..6 px
  }
  function dotRadius(score) {
    if (score == null) return 6;
    const s = Math.max(0, Math.min(1, score));
    return 7 + Math.round(s * 4);  // 7..11 px
  }

  function scoreLabel(score) {
    if (score == null) return "—";
    return VERDICT[verdictKey(score, false)].label;
  }

  function cssVar(name, fallback) {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return v || fallback;
  }

  function hourScore(hour) {
    if (!hour) return 0;
    if (hour.combined_score != null) return hour.combined_score;
    return Math.max(hour.nymph_score || 0, hour.dry_score || 0);
  }

  function initMap() {
    map = L.map("map", {
      zoomControl: false, attributionControl: true, minZoom: 7, maxZoom: 15,
      maxBounds: [[40.9, -96.4], [46.6, -86.9]], maxBoundsViscosity: 0.8,
    })
      .setView([43.75, -91.55], 8);
    L.control.zoom({ position: "bottomright" }).addTo(map);
    // Our own basemap: USGS 3DEP hillshade pre-rendered for the Driftless
    // (src/scripts/build_basemap.py) + USGS hydrography. Public domain, fast,
    // and the carved coulee terrain is the region's whole identity.
    L.tileLayer("/tiles/hillshade/{z}/{x}/{y}.jpg", {
      minZoom: 6, maxNativeZoom: 11, maxZoom: 15, className: "tiles-relief",
      bounds: [[40.8, -96.5], [46.7, -86.8]],
      attribution: "Terrain &amp; water: USGS The National Map",
    }).addTo(map);
    L.tileLayer("https://basemap.nationalmap.gov/arcgis/rest/services/USGSHydroCached/MapServer/tile/{z}/{y}/{x}", {
      maxZoom: 15, className: "tiles-hydro",
    }).addTo(map);
    map.createPane("towns");
    map.getPane("towns").style.zIndex = 450;
    map.getPane("towns").style.pointerEvents = "none";
    loadTowns();
    map.on("zoomend", updateTownVisibility);
  }

  let townLayer = null;
  let townsData = [];
  async function loadTowns() {
    try {
      const resp = await fetch(`${API_BASE}/data/towns.json`);
      townsData = await resp.json();
    } catch { townsData = []; }
    townLayer = L.layerGroup();
    townsData.forEach((t) => {
      const icon = L.divIcon({ className: `town town-t${t.tier}`, html: `<span>${escapeHtml(t.name)}</span>`, iconSize: null });
      L.marker([t.lat, t.lon], { icon, pane: "towns", interactive: false, keyboard: false }).addTo(townLayer);
    });
    townLayer.addTo(map);
    updateTownVisibility();
  }

  function updateTownVisibility() {
    const z = map.getZoom();
    document.body.classList.toggle("z-towns2", z >= 9);
    document.body.classList.toggle("z-towns1", z >= 7);
  }

  function timeLocal(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    return d.toLocaleString(undefined, { weekday: "short", hour: "numeric", minute: "2-digit" });
  }

  function relativeAge(minutes) {
    if (minutes == null || minutes < 0) return "unknown";
    if (minutes < 2) return "just now";
    if (minutes < 60) return `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 36) return `${hours}h ago`;
    return `${Math.round(hours / 24)}d ago`;
  }

  function geoLatLngs(geom) {
    if (!geom || !geom.coordinates) return null;
    if (geom.type === "LineString") {
      return geom.coordinates.map(([lon, lat]) => [lat, lon]);
    }
    if (geom.type === "MultiLineString") {
      return geom.coordinates
        .map((segment) => segment.map(([lon, lat]) => [lat, lon]))
        .filter((segment) => segment.length >= 2);
    }
    return null;
  }

  async function loadPlanningSummary() {
    const el = document.getElementById("planning-summary");
    if (!el) return;
    try {
      const resp = await fetch(`${API_BASE}/status`);
      const status = resp.ok ? await resp.json() : {};
      const fresh = status.is_stale === false;
      el.innerHTML = `<span class="dot ${fresh ? "ok" : "stale"}"></span>Forecast updated ${relativeAge(status.stale_minutes)}`;
    } catch {
      el.textContent = "";
    }
  }

  function applyResidual(reachId, score) {
    // Layer the catch-log residual on top of the physical score for display
    // only. Original score is preserved in the API response — anglers can opt
    // out of the calibration in the future if we surface a toggle.
    if (score == null) return score;
    const r = residualsByReach[reachId];
    if (!r || r.n < 3) return score;
    return Math.max(0, Math.min(1, score + r.residual));
  }

  function effectiveScore(reach) {
    let v = null;
    if (scrubGrid && scrubGrid.scores && scrubGrid.scores[reach.reach_id]) {
      v = scrubGrid.scores[reach.reach_id][scrubOffset];
    }
    if (v == null) v = reach.combined_score;
    return applyResidual(reach.reach_id, v);
  }

  async function loadResiduals() {
    try {
      const resp = await fetch(`${API_BASE}/residuals`);
      if (!resp.ok) return;
      const data = await resp.json();
      residualsByReach = data.reaches || {};
    } catch {
      // Quiet failure — without residuals we just show raw model scores.
    }
  }

  function isClosed(reach) {
    return !!(reach && reach.season && reach.season.is_closed);
  }

  function reachColor(reach) {
    return VERDICT[verdictKey(effectiveScore(reach), isClosed(reach))].color;
  }

  function styleForReach(reach) {
    const score = effectiveScore(reach);
    const closed = isClosed(reach);
    return {
      color: reachColor(reach),
      weight: closed ? 3 : scoreWeight(score),
      opacity: closed ? 0.85 : 1,
      lineCap: "round",
      lineJoin: "round",
    };
  }

  function reachTooltip(reach) {
    const score = effectiveScore(reach);
    const name = escapeHtml(reach.stream_name);
    const seg = reach.segment_name ? `<span class="muted"> · ${escapeHtml(reach.segment_name)}</span>` : "";
    if (isClosed(reach)) return `<strong>${name}</strong>${seg}<br>Season closed`;
    if (score == null) return `<strong>${name}</strong>${seg}<br>No forecast yet`;
    return `<strong>${name}</strong>${seg}<br>${scoreLabel(score)} · ${Math.round(score * 100)}`;
  }

  function renderContextLine(reaches) {
    // "Today" summary at the top of the home panel: date, what's open, what
    // looks good — one glance, plain English.
    const el = document.getElementById("today-summary");
    if (!el) return;
    const now = new Date();
    const day = now.toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" });
    const byState = {};
    reaches.forEach((r) => { if (r.state && r.season && !byState[r.state]) byState[r.state] = r.season; });
    const open = reaches.filter((r) => !isClosed(r));
    const good = open.filter((r) => (effectiveScore(r) ?? 0) >= 0.5).length;
    const closedStates = Object.entries(byState).filter(([, s]) => s.status === "closed");
    const crStates = Object.entries(byState).filter(([, s]) => s.status === "catch_and_release");
    const fmt = (iso) => iso ? new Date(iso + "T12:00:00").toLocaleDateString(undefined, { month: "short", day: "numeric" }) : "";
    let line;
    if (!open.length) {
      line = "Every stream is in its closed season.";
    } else if (closedStates.length) {
      const names = closedStates.map(([st, s]) => `${st} reopens ${fmt(s.next_change)}`).join(" · ");
      line = `${open.length} of ${reaches.length} streams are open today. ${names}.`;
    } else {
      line = `${open.length} streams open${good ? ` · ${good} worth fishing right now` : ""}.`;
    }
    if (crStates.length) line += ` ${crStates.map(([st]) => st).join(" & ")}: catch & release only.`;
    const chips = ["MN", "WI", "IA"].filter((st) => byState[st]).map((st) => {
      const s = byState[st];
      const cls = s.status === "open" ? "is-open" : s.status === "catch_and_release" ? "is-cr" : "is-closed";
      const word = s.status === "open" ? "Open" : s.status === "catch_and_release" ? "C&R" : "Closed";
      return `<span class="state-chip ${cls}" title="${escapeHtml(s.label)}">${st} · ${word}</span>`;
    }).join("");
    el.innerHTML = `<p class="today-date">${escapeHtml(day)}</p><p class="today-line">${escapeHtml(line)}</p><div class="state-chips">${chips}</div>`;
  }

  // One row style for every list (best bets, near me, hatch watch, all streams).
  function streamRow({ reachId, title, sub, chip, meta }) {
    return `<li><button type="button" class="row" data-reach="${escapeHtml(reachId)}">
        <span class="row-main"><span class="row-title">${title}</span>${sub ? `<span class="row-sub">${sub}</span>` : ""}</span>
        <span class="row-side">${chip || ""}${meta ? `<span class="row-meta">${meta}</span>` : ""}</span>
      </button></li>`;
  }

  function bindRows(container) {
    container.querySelectorAll("button.row[data-reach]").forEach((b) =>
      b.addEventListener("click", () => showReachDetail(b.dataset.reach)));
  }

  // Best score over the rest of today's fishable hours (5am–9pm local), from
  // the hourly grid the scrubber already loads. Lists say "today", the map
  // follows the scrubber hour.
  function todayBest(reach) {
    const hours = scrubGrid && scrubGrid.hours;
    const scores = scrubGrid && scrubGrid.scores && scrubGrid.scores[reach.reach_id];
    if (!hours || !scores) return effectiveScore(reach);
    const today = new Date().toDateString();
    let best = null;
    hours.forEach((iso, i) => {
      const d = new Date(iso);
      const h = d.getHours();
      if (d.toDateString() !== today || h < 5 || h > 21) return;
      const v = scores[i];
      if (v != null && (best == null || v > best)) best = v;
    });
    return applyResidual(reach.reach_id, best == null ? effectiveScore(reach) : best);
  }

  let allReaches = [];
  function renderAllStreams() {
    const el = document.getElementById("all-streams");
    if (!el) return;
    const q = (document.getElementById("stream-search")?.value || "").trim().toLowerCase();
    let list = allReaches.slice();
    let townHit = null;
    if (q) {
      townHit = townsData.find((t) => t.name.toLowerCase().startsWith(q));
      list = list.filter((r) => {
        const hay = `${r.stream_name} ${r.segment_name || ""} ${r.state}`.toLowerCase();
        if (hay.includes(q)) return true;
        if (townHit) {
          const d = Math.hypot((r.centroid_lat - townHit.lat) * 111, (r.centroid_lon - townHit.lon) * 80);
          return d <= 30;
        }
        return false;
      });
    }
    const row = (r) => {
      const score = todayBest(r);
      const k = verdictKey(score, isClosed(r));
      return streamRow({
        reachId: r.reach_id,
        title: `${escapeHtml(r.stream_name)}<span class="row-state">${escapeHtml(r.state || "")}</span>`,
        sub: escapeHtml(r.segment_name || ""),
        chip: `<span class="vdot v-${k}"></span><span class="vlabel">${VERDICT[k].label}</span>`,
      });
    };
    const open = list.filter((r) => !isClosed(r)).sort((a, b) => (todayBest(b) ?? 0) - (todayBest(a) ?? 0));
    const closed = list.filter((r) => isClosed(r)).sort((a, b) => a.stream_name.localeCompare(b.stream_name));
    document.getElementById("all-count").textContent = q ? `${list.length} match${list.length === 1 ? "" : "es"}` : `${allReaches.length}`;
    if (!list.length) {
      el.innerHTML = `<p class="muted empty">No streams match “${escapeHtml(q)}”.</p>`;
      return;
    }
    el.innerHTML = `
      ${open.length ? `<p class="subgroup">Open · best today${townHit ? ` · near ${escapeHtml(townHit.name)}` : ""}</p><ol class="rows">${open.map(row).join("")}</ol>` : ""}
      ${closed.length ? `<details class="closed-group"${q ? " open" : ""}><summary><span>Closed season</span><span class="muted">${closed.length}</span></summary><ol class="rows">${closed.map(row).join("")}</ol></details>` : ""}`;
    bindRows(el);
  }

  let geometryById = null;
  async function loadGeometry() {
    if (geometryById) return geometryById;
    try {
      const resp = await fetch(`${API_BASE}/geometry`);
      const fc = await resp.json();
      geometryById = {};
      (fc.features || []).forEach((f) => { geometryById[f.id] = f.geometry; });
    } catch { geometryById = {}; }
    return geometryById;
  }

  let firstFit = true;
  async function loadReaches() {
    const summaryEl = document.getElementById("summary");
    try {
      const [resp, geoms] = await Promise.all([fetch(`${API_BASE}/reaches`), loadGeometry()]);
      const reaches = await resp.json();
      allReaches = reaches;
      // Reuse one layer group: this runs on every refresh, and adding a new
      // group each time used to stack duplicate lines on the map.
      if (reachGroup) reachGroup.clearLayers();
      else reachGroup = L.layerGroup().addTo(map);
      const group = reachGroup;
      markersByReach = {};
      renderContextLine(reaches);
      renderAllStreams();
      const casing = cssVar("--map-casing", "#ffffff");
      const bounds = [];
      // Closed reaches first so open (colored) water draws on top.
      reaches.slice().sort((a, b) => Number(isClosed(b)) - Number(isClosed(a))).forEach((reach) => {
        if (reach.centroid_lat == null || reach.centroid_lon == null) return;
        const score = effectiveScore(reach);
        const latlngs = geoLatLngs(geoms[reach.reach_id]);
        let line, halo = null;
        if (latlngs && latlngs.length) {
          halo = L.polyline(latlngs, { color: casing, weight: scoreWeight(score) + 4, opacity: isClosed(reach) ? 0.6 : 0.95, lineCap: "round", interactive: false });
          line = L.polyline(latlngs, styleForReach(reach));
          group.addLayer(halo);
          group.addLayer(line);
          bounds.push(...(Array.isArray(latlngs[0][0]) ? latlngs.flat() : latlngs));
        } else {
          line = L.circleMarker([reach.centroid_lat, reach.centroid_lon], { radius: 6, color: casing, weight: 2, fillColor: reachColor(reach), fillOpacity: 1 });
          group.addLayer(line);
          bounds.push([reach.centroid_lat, reach.centroid_lon]);
        }
        line.bindTooltip(reachTooltip(reach), { sticky: true, className: "reach-tip" });
        line.on("click", () => showReachDetail(reach.reach_id));
        markersByReach[reach.reach_id] = { line, halo, dot: null, reach };
      });
      if (firstFit && bounds.length) {
        const padLeft = window.innerWidth > 720 ? 440 : 20;
        map.fitBounds(bounds, { paddingTopLeft: [padLeft, 30], paddingBottomRight: [30, window.innerWidth > 720 ? 90 : 220] });
        firstFit = false;
      }
      if (focusedReachId) focusReach(focusedReachId, !pendingFly);
      const openCount = reaches.filter((r) => !isClosed(r)).length;
      if (summaryEl) summaryEl.textContent = `${reaches.length} streams · ${openCount} open today`;
    } catch (err) {
      const el = document.getElementById("all-streams");
      if (el) el.innerHTML = `<p class="error">Couldn't load streams: ${escapeHtml(err.message)}</p>`;
    }
  }

  function repaintReaches() {
    Object.values(markersByReach).forEach(({ line, reach }) => {
      if (line.setStyle) line.setStyle(line instanceof L.CircleMarker ? { fillColor: reachColor(reach) } : styleForReach(reach));
      if (line.setTooltipContent) line.setTooltipContent(reachTooltip(reach));
    });
    if (focusedReachId) focusReach(focusedReachId, true);
    renderAllStreams();
  }

  async function loadBestWindows() {
    const listEl = document.getElementById("windows-list");
    try {
      const resp = await fetch(`${API_BASE}/best-windows?hours=72&limit=6`);
      let windows = await resp.json();
      if (!windows.length) {
        listEl.innerHTML = `<li class="empty muted">No open streams look worth the drive in the next 3 days.</li>`;
        return;
      }
      windows = windows
        .map((w) => ({ ...w, score: applyResidual(w.reach_id, w.score), rank_score: w.rank_score != null ? applyResidual(w.reach_id, w.rank_score) : applyResidual(w.reach_id, w.score) }))
        .sort((a, b) => b.rank_score - a.rank_score);
      listEl.innerHTML = windows.map((w) => {
        const k = verdictKey(w.score, false);
        const play = w.regime && w.regime.label ? escapeHtml(w.regime.label) : "";
        return streamRow({
          reachId: w.reach_id,
          title: `${escapeHtml(w.stream_name)}<span class="row-state">${escapeHtml(w.state || "")}</span>`,
          sub: [timeLocal(w.valid_at), play].filter(Boolean).join(" · "),
          chip: `<span class="vchip v-${k}">${VERDICT[k].label}</span>`,
        });
      }).join("");
      bindRows(listEl);
    } catch (err) {
      listEl.innerHTML = `<li class="error">${escapeHtml(err.message)}</li>`;
    }
  }

  async function loadHatchWatch() {
    const listEl = document.getElementById("hatch-watch-list");
    const group = document.getElementById("hatch-watch-group");
    if (!listEl) return;
    try {
      const resp = await fetch(`${API_BASE}/hatch-windows?hours=168&limit=4&min_surface=0.25`);
      const windows = resp.ok ? await resp.json() : [];
      if (group) group.classList.toggle("hidden", !windows.length);
      listEl.innerHTML = windows.map((w) => {
        const sp = w.top_species || {};
        const pct = w.surface_signal != null ? `${Math.round(w.surface_signal * 100)}%` : "";
        return streamRow({
          reachId: w.reach_id,
          title: escapeHtml(sp.common_name || sp.id || "Surface activity"),
          sub: `${escapeHtml(w.stream_name || "")} · ${timeLocal(w.valid_at)}`,
          meta: pct,
        });
      }).join("");
      bindRows(listEl);
    } catch {
      if (group) group.classList.add("hidden");
    }
  }


  function renderHatchPatternBoxes(activeSpecies, allSpecies) {
    const top = (activeSpecies || []).slice().sort((a,b) => (b.probability||0)-(a.probability||0)).slice(0, 4);
    if (!top.length || !allSpecies) return "";
    const blocks = top.map((s) => {
      const sp = allSpecies[s.id] || {};
      const patterns = sp.fly_patterns || [];
      if (!patterns.length) return "";
      const stages = ["dun","emerger","spinner","nymph","adult"];
      patterns.sort((a,b) => stages.indexOf(a.stage) - stages.indexOf(b.stage));
      const items = patterns.map(p => `<li><span class="pat-stage">${p.stage || "fly"}</span> ${p.pattern}${p.size ? ` <span class="pat-size">#${p.size}</span>` : ""}</li>`).join("");
      const name = s.common_name || s.id;
      return `<div class="hatch-flies">
        <div class="hatch-flies-head">
          <strong>${name}</strong>
          <span class="hatch-prob">${Math.round((s.probability||0)*100)}%</span>
        </div>
        <ul class="pat-list">${items}</ul>
      </div>`;
    }).join("");
    return blocks;
  }

  function dayKey(iso) {
    const d = new Date(iso);
    return d.toLocaleDateString(undefined, { weekday: "short", month: "numeric", day: "numeric" });
  }

  function weatherIcon(label, precipProb, cloudCover) {
    const s = (label || "").toLowerCase();
    if (s.includes("thunder")) return "⛈";
    if (s.includes("snow")) return "❄";
    if (s.includes("rain") || s.includes("shower") || s.includes("drizzle")) return "🌧";
    if (s.includes("fog")) return "🌫";
    if ((precipProb || 0) >= 50) return "🌧";
    if (s.includes("overcast") || s.includes("cloudy") || (cloudCover || 0) > 0.75) return "☁";
    if (s.includes("partly") || (cloudCover || 0) > 0.35) return "⛅";
    return "☀";
  }

  function summarizeWeather(hours) {
    const temps = hours.map(h => h.air_temp_f).filter(v => v != null);
    const hi = temps.length ? Math.max(...temps) : null;
    const lo = temps.length ? Math.min(...temps) : null;
    const precips = hours.map(h => h.precip_prob).filter(v => v != null);
    const maxPop = precips.length ? Math.max(...precips) : 0;
    // Weight short_forecast toward mid-day when people actually fish.
    const midday = hours.filter(h => {
      const hr = new Date(h.valid_at).getHours();
      return hr >= 10 && hr <= 18;
    });
    const counts = {};
    (midday.length ? midday : hours).forEach(h => {
      const s = h.short_forecast || "";
      counts[s] = (counts[s] || 0) + 1;
    });
    let top = "", topN = 0;
    for (const [s, n] of Object.entries(counts)) { if (n > topN) { top = s; topN = n; } }
    const dominant = midday[0] || hours[0] || {};
    return {
      hi, lo, maxPop, label: top,
      icon: weatherIcon(top, maxPop, dominant.cloud_cover),
    };
  }

  // Salmonids feed predominantly at dawn and dusk through the day in
  // spring/summer/autumn, with "little food from 10pm to 5am" (Hoar 1942,
  // J. Fish. Res. Bd. Can.; Helfman 1979 for the broader light-coupling
  // basis). Showing a 4am peak — technically valid because pre-dawn water
  // is at its diurnal trough — is not actionable for a human angler.
  // Constrain the displayed daily peak to local 5–22; the underlying hourly
  // scores in `hours` are unchanged.
  function isFishableHour(iso) {
    const h = new Date(iso).getHours();
    return h >= 5 && h <= 22;
  }

  function groupByDay(hours) {
    const days = {};
    hours.forEach((h) => {
      const key = dayKey(h.valid_at);
      (days[key] = days[key] || []).push(h);
    });
    return Object.entries(days).map(([label, hrs]) => {
      // Restrict the peak picker to angler-fishable hours so the headline
      // recommendation lands on a time a human can use. Fall back to all
      // hours only if the day has fewer than 3 fishable hours (shouldn't
      // happen — every day has 18 fishable hours in our window).
      const fishable = hrs.filter((h) => isFishableHour(h.valid_at));
      const pickFrom = fishable.length >= 3 ? fishable : hrs;
      const scores = pickFrom.map(hourScore);
      // Day "peak" = best 3-hour window mean, not single-hour spike. Single-hour
      // spikes happen when chunky NWS cloud-cover input crosses our cloud-pref
      // saturation thresholds — they're real but not actionable for fishing.
      let peak = 0, peakIdx = 0;
      if (scores.length >= 3) {
        for (let i = 0; i <= scores.length - 3; i++) {
          const m = (scores[i] + scores[i+1] + scores[i+2]) / 3;
          if (m > peak) { peak = m; peakIdx = i + 1; }  // middle of window
        }
      } else {
        peak = scores.reduce((a, b) => Math.max(a, b), 0);
        peakIdx = scores.indexOf(peak);
      }
      const weather = summarizeWeather(hrs);
      return { label, hours: hrs, peak, peakHour: pickFrom[peakIdx] || pickFrom[0], weather };
    });
  }

  function findBestWindow(hours) {
    // Sliding 3-hour window. Score per hour = API headline score; window
    // score = mean of those per-hour scores.
    // Restricted to angler-fishable hours for the same reason as groupByDay —
    // a "best window" centered on 3am isn't a useful recommendation.
    if (hours.length < 3) return null;
    const fishable = hours.filter((h) => isFishableHour(h.valid_at));
    const pickFrom = fishable.length >= 3 ? fishable : hours;
    const combined = pickFrom.map(hourScore);
    let bestMean = 0, bestStart = 0;
    for (let i = 0; i <= combined.length - 3; i++) {
      const m = (combined[i] + combined[i+1] + combined[i+2]) / 3;
      if (m > bestMean) { bestMean = m; bestStart = i; }
    }
    return {
      start: pickFrom[bestStart].valid_at,
      end: pickFrom[bestStart + 2].valid_at,
      score: bestMean,
    };
  }

  function renderSparkline(hours) {
    if (!hours.length) return "";
    const w = 320, h = 60, pad = 4;
    const barWidth = (w - 2 * pad) / hours.length;
    // Day-boundary ticks so the chart reads as a calendar, not a mystery ramp.
    const boundaries = [];
    let prevDay = null;
    hours.forEach((hr, i) => {
      const day = new Date(hr.valid_at).toDateString();
      if (day !== prevDay) {
        boundaries.push(i);
        prevDay = day;
      }
    });
    const boundaryLines = boundaries.map(i => {
      const x = pad + i * barWidth;
      return `<line x1="${x}" y1="0" x2="${x}" y2="${h}" stroke="#cbd5e0" stroke-width="0.4" />`;
    }).join("");
    const boundaryLabels = boundaries.map(i => {
      const x = pad + i * barWidth + 2;
      const d = new Date(hours[i].valid_at);
      const label = d.toLocaleDateString(undefined, { weekday: "short" });
      return `<text x="${x}" y="9" font-size="7" fill="#6b7280">${label}</text>`;
    }).join("");
    const bars = hours.map((hr, i) => {
      const s = hourScore(hr);
      const x = pad + i * barWidth;
      const barH = s * (h - 2 * pad - 10);
      return `<rect class="spark-bar" data-i="${i}" x="${x}" y="${h - pad - barH}" width="${Math.max(barWidth - 0.2, 0.5)}" height="${barH}" fill="${scoreColor(s)}" />`;
    }).join("");
    return `<svg class="sparkline" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" aria-label="${hours.length}h combined score">
      <rect x="0" y="0" width="${w}" height="${h}" fill="#fafafa" />
      ${boundaryLines}
      ${boundaryLabels}
      ${bars}
      <line id="spark-cursor" x1="${pad}" y1="0" x2="${pad}" y2="${h}" stroke="#264653" stroke-width="1" opacity="0" pointer-events="none" />
      <rect id="spark-hit" x="0" y="0" width="${w}" height="${h}" fill="transparent" />
    </svg>`;
  }

  function attachSparklineHover(hours) {
    const svg = document.querySelector("#detail-body .sparkline");
    const tooltip = document.getElementById("spark-tooltip");
    if (!svg || !tooltip) return;
    const hit = svg.querySelector("#spark-hit");
    const cursor = svg.querySelector("#spark-cursor");
    const pad = 4;
    const w = 320;

    function show(i) {
      if (i < 0 || i >= hours.length) return;
      const hr = hours[i];
      const s = hourScore(hr);
      const dt = new Date(hr.valid_at);
      const when = dt.toLocaleString(undefined, {
        weekday: "short", month: "numeric", day: "numeric",
        hour: "numeric", minute: "2-digit",
      });
      const airT = hr.air_temp_f != null ? `air ${Math.round(hr.air_temp_f)}°F` : "";
      const wind = hr.wind_mph != null ? `wind ${Math.round(hr.wind_mph)} mph` : "";
      const sf = hr.short_forecast || "";
      const pop = hr.precip_prob != null && hr.precip_prob > 0 ? ` · ${Math.round(hr.precip_prob)}% rain` : "";
      const wline = [sf, airT, wind].filter(Boolean).join(" · ");
      tooltip.innerHTML = `
        <div class="tt-head">
          <span class="tt-when">${when}</span>
          <span class="score-pill" style="background:${scoreColor(s)}">${Math.round(s * 100)}</span>
        </div>
        <div class="tt-weather">${wline}${pop}</div>
        <div class="tt-explain">${hr.explanation || ""}</div>
      `;
      tooltip.style.display = "block";
      const x = pad + i * ((w - 2 * pad) / hours.length);
      cursor.setAttribute("x1", x);
      cursor.setAttribute("x2", x);
      cursor.setAttribute("opacity", "0.7");
    }

    function onMove(e) {
      const rect = svg.getBoundingClientRect();
      const xPx = (e.touches ? e.touches[0].clientX : e.clientX) - rect.left;
      const vbX = (xPx / rect.width) * w;
      const i = Math.max(0, Math.min(hours.length - 1,
        Math.floor((vbX - pad) / ((w - 2 * pad) / hours.length))));
      show(i);
    }

    hit.addEventListener("mousemove", onMove);
    hit.addEventListener("touchmove", onMove);
    hit.addEventListener("mouseleave", () => {
      tooltip.style.display = "none";
      cursor.setAttribute("opacity", "0");
    });
  }

  function renderDayStrip(days) {
    const maxDays = Math.min(days.length, 8);
    const rows = days.slice(0, maxDays).map((d) => {
      const hour = d.peakHour ? new Date(d.peakHour.valid_at).toLocaleTimeString(undefined, { hour: "numeric" }) : "";
      const w = d.weather || {};
      const hi = w.hi != null ? `${Math.round(w.hi)}°` : "—";
      const lo = w.lo != null ? `${Math.round(w.lo)}°` : "—";
      const pop = w.maxPop ? `<span class="dr-pop">${Math.round(w.maxPop)}%</span>` : "";
      const dayParts = d.label.split(" ");
      return `<div class="day-row" title="${(w.label || '').replace(/"/g,'&quot;')}">
        <div class="dr-when">
          <span class="dr-day">${dayParts[0]}</span>
          <span class="dr-date">${dayParts.slice(1).join(" ")}</span>
        </div>
        <div class="dr-weather">
          <span class="dr-icon">${w.icon || ""}</span>
          <span class="dr-temps">${hi}/${lo}</span>
          ${pop}
        </div>
        <div class="dr-peak">
          <span class="dr-score" style="background:${scoreColor(d.peak)}">${Math.round(d.peak * 100)}</span>
          <span class="dr-hour">${hour}</span>
        </div>
      </div>`;
    }).join("");
    return `<div class="day-list">${rows}</div>`;
  }

  function formatWindowRange(start, end) {
    const s = new Date(start);
    const e = new Date(end);
    const sDay = s.toLocaleDateString(undefined, { weekday: "short" });
    const sTime = s.toLocaleTimeString(undefined, { hour: "numeric" });
    const eTime = e.toLocaleTimeString(undefined, { hour: "numeric" });
    return `${sDay} ${sTime}–${eTime}`;
  }

  function windowHours(hours, window_) {
    if (!window_) return [];
    const start = new Date(window_.start).getTime();
    const end = new Date(window_.end).getTime();
    return hours.filter((h) => {
      const t = new Date(h.valid_at).getTime();
      return t >= start && t <= end;
    });
  }

  function peakHourForWindow(hours, window_) {
    const hrs = windowHours(hours, window_);
    return hrs.reduce((best, h) => hourScore(h) > hourScore(best) ? h : best, hrs[0] || null);
  }

  function fishingMode(hour) {
    if (!hour) return "Forecast";
    const model = hour.score_model || {};
    const lanes = model.lanes || {};
    const surface = Math.max(hour.dry_score || 0, model.top_hatch_probability || 0, lanes.surface_window || 0);
    const regime = hour.regime || {};
    if (regime.code === "STREAMER") return "Streamer";
    if (surface >= 0.15) return "Dry-fly watch";
    if ((lanes.activation || 0) >= 0.65) return "Activation window";
    if ((hour.nymph_score || 0) >= 0.45) return "Nymphing play";
    return "Scouting window";
  }

  function scoreDriverChips(hour) {
    if (!hour) return "";
    const sb = hour.score_breakdown || {};
    const model = hour.score_model || {};
    const chips = [];
    if (hour.water_temp_f != null) {
      const t = Math.round(hour.water_temp_f);
      if (t >= 52 && t <= 64) chips.push(`ideal water ${t}°`);
      else if (t >= 65 && t <= 68) chips.push(`warm water ${t}°`);
      else chips.push(`water ${t}°`);
    }
    if (sb.flow_percentile != null) {
      if (sb.flow_percentile >= 0.85) chips.push("strong flow band");
      else if (sb.flow_percentile < 0.55) chips.push("flow penalty");
    }
    if (sb.diel_activity != null) {
      if (sb.diel_activity >= 0.96) chips.push("low-light window");
      else if (sb.diel_activity <= 0.86) chips.push("midday/overnight drag");
    }
    if (sb.pressure_factor != null) {
      if (sb.pressure_factor >= 1.01) chips.push("falling barometer");
      else if (sb.pressure_factor <= 0.98) chips.push("rising barometer");
    }
    if (sb.sun_factor != null && sb.sun_factor < 0.90) chips.push("bright-sun drag");
    if (model.surface_signal >= 0.15) chips.push("surface signal");
    if (model.lanes && model.lanes.activation >= 0.65) chips.push("activation window");
    return chips.slice(0, 4).map((c) => `<span class="driver-chip">${c}</span>`).join("");
  }

  function explainWhyLater(now, bestHour, bestScore) {
    if (!now || !bestHour) return "";
    const nowScore = hourScore(now);
    const nowTime = new Date(now.valid_at).getTime();
    const bestTime = new Date(bestHour.valid_at).getTime();
    if (bestTime <= nowTime || bestScore < nowScore + 0.06) return "";

    const n = now.score_breakdown || {};
    const b = bestHour.score_breakdown || {};
    const reasons = [];
    if ((b.diel_activity || 0) >= (n.diel_activity || 0) + 0.06) {
      reasons.push("the light/timing window improves");
    }
    if ((b.sun_factor || 1) >= (n.sun_factor || 1) + 0.08) {
      reasons.push("bright sun eases");
    }
    if ((b.pressure_factor || 1) >= (n.pressure_factor || 1) + 0.02) {
      reasons.push("barometer trend helps more");
    }
    if ((b.temperature || 0) >= (n.temperature || 0) + 0.08) {
      reasons.push("water temperature moves into a better band");
    }
    if ((b.flow_percentile || 0) >= (n.flow_percentile || 0) + 0.08) {
      reasons.push("flow percentile improves");
    }
    const nowSurface = Math.max(now.dry_score || 0, (now.score_model || {}).top_hatch_probability || 0);
    const bestSurface = Math.max(bestHour.dry_score || 0, (bestHour.score_model || {}).top_hatch_probability || 0);
    if (bestSurface >= nowSurface + 0.10) {
      reasons.push("surface activity gets stronger");
    }

    const lead = reasons.length
      ? reasons.slice(0, 2).join(" and ")
      : "the later 3-hour average is stronger";
    return `<p class="why-later"><strong>Why not now?</strong> ${lead}, so the better play is to wait for the window.</p>`;
  }

  function renderFishingPlan(hours, now, bestWindow) {
    if (!now) return "";
    const nowScore = hourScore(now);
    const bestHour = peakHourForWindow(hours, bestWindow) || now;
    const bestScore = bestWindow ? bestWindow.score : hourScore(bestHour);
    const bestRange = bestWindow ? formatWindowRange(bestWindow.start, bestWindow.end) : timeLocal(bestHour.valid_at);
    const delta = bestScore - nowScore;
    const deltaText = delta >= 0.04
      ? `+${Math.round(delta * 100)} vs now`
      : delta <= -0.04
        ? `${Math.round(delta * 100)} vs now`
        : "similar to now";
    const bestWhy = scoreDriverChips(bestHour);
    const nowWhy = scoreDriverChips(now);
    const whyLater = explainWhyLater(now, bestHour, bestScore);
    return `
      <div class="plan-panel">
        <div class="plan-card">
          <span class="plan-label">Right now</span>
          <strong>${Math.round(nowScore * 100)}</strong>
          <span>${scoreLabel(nowScore)} · ${fishingMode(now)}</span>
          <div class="driver-chips">${nowWhy}</div>
        </div>
        <div class="plan-card plan-card-best">
          <span class="plan-label">Best next window</span>
          <strong>${bestRange}</strong>
          <span>${Math.round(bestScore * 100)}/100 · ${deltaText}</span>
          <div class="driver-chips">${bestWhy}</div>
          ${whyLater}
        </div>
      </div>
    `;
  }

  // ── Field guide: season, trip call, tactics, near me ─────────────────────
  const VERDICT_COLORS = Object.fromEntries(Object.entries(VERDICT).map(([k, v]) => [k, v.color]));

  function renderSeasonBanner(season) {
    if (!season) return "";
    const link = season.source_url
      ? ` <a href="${season.source_url}" target="_blank" rel="noopener">regs</a>` : "";
    let html = "";
    if (season.status === "closed") {
      html += `<div class="warning-banner season-closed">⛔ <strong>${escapeHtml(season.label)}</strong>${season.next_change ? ` — reopens ${escapeHtml(season.next_change)}` : ""}. ${escapeHtml(season.detail || "")}${link}</div>`;
    } else if (season.status === "catch_and_release") {
      html += `<div class="warning-banner soft">↺ <strong>${escapeHtml(season.label)}</strong>${season.next_change ? ` until ${escapeHtml(season.next_change)}` : ""}. Gear rules vary by stream.${link}</div>`;
    }
    if (season.spawning_advisory && season.status !== "closed") {
      html += `<div class="warning-banner soft">🥚 ${escapeHtml(season.spawning_advisory)}</div>`;
    }
    return html;
  }

  function heroLine(day) {
    // The verdict + window are already the headline; for an ordinary day the
    // useful sentence is the method. Keep the advice when it says more
    // (skip reason, closed, or "a later day is clearly better").
    const method = day.best_window && day.best_window.method;
    const generic = day.advice && day.advice.startsWith(`${day.verdict_label} — best`);
    if (generic && method) return method.charAt(0).toUpperCase() + method.slice(1) + ".";
    return day.advice || "";
  }

  function renderHero(days, nowScore) {
    const today = days && days[0];
    if (!today) return "";
    const v = VERDICT[today.verdict] || VERDICT.closed;
    const win = today.verdict === "closed" ? "" : `best ${escapeHtml(today.best_window.label)}`;
    const reasons = (today.reasons || []).map((r) => `<li>${escapeHtml(r)}</li>`).join("");
    const score = today.best_window && today.best_window.score != null ? Math.round(today.best_window.score * 100) : null;
    return `
      <section class="hero" style="--hero-color:${v.color}">
        ${score != null && today.verdict !== "closed" ? `<div class="hero-score"><span class="num">${score}</span><small>best window</small></div>` : ""}
        <div class="hero-label">Today</div>
        <div class="hero-verdict"><span class="hero-word">${escapeHtml(v.label)}</span><span class="hero-window">${win}</span></div>
        <p class="hero-advice">${escapeHtml(heroLine(today))}</p>
        ${reasons ? `<ul class="hero-reasons">${reasons}</ul>` : ""}
      </section>`;
  }

  function renderTripCall(days) {
    if (!days || !days.length) return "";
    const cells = days.slice(0, 7).map((d, i) => `
      <li class="trip-day${i === 0 ? " is-today" : ""}" title="${escapeHtml(d.advice || "")}">
        <span class="trip-wd">${i === 0 ? "Today" : escapeHtml(d.weekday)}</span>
        <span class="trip-verdict" style="background:${VERDICT_COLORS[d.verdict] || "#9aa5b1"}" aria-label="${escapeHtml(d.verdict_label)}"></span>
        <span class="trip-window">${d.verdict === "closed" ? "—" : escapeHtml((d.best_window.label || "").split("–")[0])}</span>
      </li>`).join("");
    const legend = ["go", "good", "slow", "skip", "closed"].map((k) =>
      `<span><i style="background:${VERDICT[k].color}"></i>${VERDICT[k].label}</span>`).join("");
    return `
      <section class="section trip-call">
        <h3>Next 7 days</h3>
        <ol class="trip-days">${cells}</ol>
        <div class="trip-legend">${legend}</div>
      </section>`;
  }

  function flowWord(pct) {
    if (pct == null) return { v: "—", s: "no flow history" };
    const p = Math.round(pct * 100);
    if (pct < 0.10) return { v: "Very low", s: `${p}th percentile for the date` };
    if (pct < 0.28) return { v: "Low", s: `${p}th percentile for the date` };
    if (pct <= 0.68) return { v: "Normal", s: `${p}th percentile for the date` };
    if (pct <= 0.85) return { v: "High", s: `${p}th percentile for the date` };
    return { v: "Very high", s: `${p}th percentile for the date` };
  }

  function renderConditions(h) {
    if (!h) return "";
    const sb = h.score_breakdown || {};
    const t = h.tactics || {};
    const flow = flowWord(sb.percentile_used);
    const water = h.water_temp_f != null ? `${Math.round(h.water_temp_f)}°F` : "—";
    const waterSrc = h.water_temp_source === "gauge" ? "measured at gauge" : "estimated from air temps";
    const cfs = h.flow_cfs != null ? ` · ${Math.round(h.flow_cfs)} cfs` : "";
    const air = h.air_temp_f != null ? `${Math.round(h.air_temp_f)}°F` : "—";
    const wind = h.wind_mph != null ? `wind ${Math.round(h.wind_mph)} mph` : "";
    const sky = h.short_forecast ? escapeHtml(h.short_forecast) : "";
    return `
      <section class="section">
        <h3>Conditions now</h3>
        <div class="tiles">
          <div class="tile"><div class="tile-k">Water ${helpButton(h.water_temp_source === "gauge" ? "water_temp_zone" : "mohseni")}</div><div class="tile-v">${water}</div><div class="tile-s">${waterSrc}</div></div>
          <div class="tile"><div class="tile-k">Flow ${helpButton("flow_percentile")}</div><div class="tile-v">${flow.v}</div><div class="tile-s">${flow.s}${cfs}</div></div>
          <div class="tile"><div class="tile-k">Clarity</div><div class="tile-v">${escapeHtml((t.clarity_label || "—").replace(/^./, (c) => c.toUpperCase()))}</div><div class="tile-s">estimated from flow &amp; rain</div></div>
          <div class="tile"><div class="tile-k">Weather</div><div class="tile-v">${air}</div><div class="tile-s">${[sky, wind].filter(Boolean).join(" · ")}</div></div>
        </div>
      </section>`;
  }

  function renderOnTheWater(h) {
    const top = ((h && h.active_species) || []).slice()
      .sort((a, b) => (b.probability || 0) - (a.probability || 0)).slice(0, 4);
    const items = top.map((s) => {
      const p = Math.round((s.probability || 0) * 100);
      return `<li><span>${escapeHtml(s.common_name || s.id)}</span><span class="muted small">${p}%</span>
        <span class="hatch-bar"><i style="width:${Math.max(4, p)}%"></i></span></li>`;
    }).join("");
    return `
      <section class="section">
        <h3>On the water ${helpButton("emergence_hour")}</h3>
        ${items ? `<ul class="hatches">${items}</ul>` : `<p class="muted">No meaningful hatch in this window — fish subsurface.</p>`}
        ${renderHatchPatternBoxes(h && h.active_species, speciesById)}
      </section>`;
  }

  function renderTactics(t, whenLabel) {
    if (!t) return "";
    const list = (items) => (items || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("");
    return `
      <section class="section tactics">
        <h3>How to fish it ${helpButton("season_and_tactics")}</h3>
        ${whenLabel ? `<p class="section-sub">${escapeHtml(whenLabel)}</p>` : ""}
        <p class="tactic-method"><strong>${escapeHtml(t.method)}</strong></p>
        <div class="tactic-grid">
          ${t.flies && t.flies.length ? `<div class="tactic-block"><span class="tactic-k">Tie on</span><ul class="tactic-flies">${list(t.flies.slice(0, 6))}</ul></div>` : ""}
          ${t.rig && t.rig !== "—" ? `<div class="tactic-block"><span class="tactic-k">Rig</span><p class="tactic-rig">${escapeHtml(t.rig)}</p></div>` : ""}
          ${t.water && t.water.length ? `<div class="tactic-block"><span class="tactic-k">Fish</span><ul>${list(t.water)}</ul></div>` : ""}
          ${t.approach && t.approach.length ? `<div class="tactic-block"><span class="tactic-k">Approach</span><ul>${list(t.approach)}</ul></div>` : ""}
          ${t.where ? `<div class="tactic-block"><span class="tactic-k">Where</span><p class="tactic-where">${escapeHtml(t.where)}</p></div>` : ""}
        </div>
        <p class="fine-print">Guide-derived practice from Driftless fly-shop reports and agency guidance — a starting point, not gospel.</p>
      </section>`;
  }

  function tacticsHourFor(hours, days) {
    const start = days && days[0] && days[0].best_window && days[0].best_window.start;
    const hit = start ? hours.find((h) => h.valid_at === start) : null;
    return hit || hours[0];
  }

  async function loadNearMe() {
    const statusEl = document.getElementById("near-status");
    const listEl = document.getElementById("near-list");
    const show = (msg) => { statusEl.textContent = msg; statusEl.classList.remove("hidden"); };
    if (!navigator.geolocation) { show("Location isn't available in this browser."); return; }
    show("Finding streams near you…");
    navigator.geolocation.getCurrentPosition(async (pos) => {
      try {
        const { latitude, longitude } = pos.coords;
        const resp = await fetch(`${API_BASE}/near?lat=${latitude.toFixed(3)}&lon=${longitude.toFixed(3)}&max_km=120&limit=6`);
        if (!resp.ok) throw new Error(resp.status === 422 ? "You look to be outside the Driftless region." : `HTTP ${resp.status}`);
        const data = await resp.json();
        const rows = data.reaches || [];
        if (!rows.length) { show("No modeled streams within ~75 miles."); listEl.innerHTML = ""; return; }
        statusEl.classList.add("hidden");
        listEl.innerHTML = rows.map((r) => {
          const today = r.today || {};
          const tmrw = r.tomorrow || {};
          const win = today.verdict && today.verdict !== "closed" ? today.best_window.label : "";
          const tm = tmrw.verdict ? `Tomorrow ${tmrw.verdict_label.toLowerCase()}` : "";
          return streamRow({
            reachId: r.reach_id,
            title: `${escapeHtml(r.stream_name)}<span class="row-state">${r.distance_mi} mi</span>`,
            sub: [win && `Best ${win}`, tm].filter(Boolean).map(escapeHtml).join(" · "),
            chip: `<span class="vchip v-${today.verdict || "closed"}">${escapeHtml(today.verdict_label || "—")}</span>`,
          });
        }).join("");
        bindRows(listEl);
      } catch (err) {
        show(err.message);
      }
    }, () => show("Location permission was declined — pick a stream on the map instead."),
       { enableHighAccuracy: false, timeout: 10000, maximumAge: 600000 });
  }

  async function showReachDetail(reachId, opts = {}) {
    currentDetailReachId = reachId;
    focusReach(reachId);
    setView("detail", { silent: true });
    if (!opts.silent) writeHash(`#/reach/${reachId}`);
    // On mobile the sheet is collapsed by default; opening detail should pop it up.
    document.body.classList.add("sidebar-open");
    const tb = document.getElementById("sidebar-toggle");
    if (tb) tb.setAttribute("aria-expanded", "true");
    const body = document.getElementById("detail-body");
    body.innerHTML = `<p class="muted">Loading forecast…</p>`;
    await ensureSpeciesCache();

    try {
      const resp = await fetch(`${API_BASE}/forecast/${reachId}?hours=168`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const fc = await resp.json();
      const hours = fc.hours || [];
      if (!hours.length) {
        body.innerHTML = `<h2>${fc.stream_name}</h2>
          <p class="muted">${fc.segment_name || ""}</p>
          <p class="muted">No forecast rows yet — pipeline may still be warming up.</p>`;
        return;
      }
      const now = hours[0];
      const nowScore = hourScore(now);

      const waterTopic = now.water_temp_source === "gauge" ? "water_temp_zone" : "mohseni";
      const waterBadge = now.water_temp_f != null
        ? `<span class="water-chip ${now.water_temp_source === 'gauge' ? 'water-meas' : 'water-est'}">${Math.round(now.water_temp_f)}°F ${now.water_temp_source === 'gauge' ? 'measured' : 'est.'} ${helpButton(waterTopic)}</span>`
        : "";
      const anomalyChip = (now.anomaly_f != null && Math.abs(now.anomaly_f) >= 2.0)
        ? `<span class="anomaly-chip ${now.anomaly_f > 0 ? 'warm' : 'cold'}">${now.anomaly_f > 0 ? "+" : ""}${now.anomaly_f.toFixed(1)}°F vs normal ${helpButton("anomaly_shift")}</span>`
        : "";
      // Hide NORMAL regime — it conveys no actionable info and adds noise.
      const regimeChip = (now.regime && now.regime.code !== "NORMAL")
        ? `<span class="regime-chip regime-${(now.regime.severity || 'info')}" title="${(now.regime.detail || '').replace(/"/g,'&quot;')}">${now.regime.label} ${helpButton("regimes")}</span>`
        : "";
      const baroChip = barometricChip(now.pressure_delta_mb);
      const stressBanner = now.fish_stress
        ? `<div class="warning-banner">⚠ Water over 68°F — handle fish minimally, or fish dawn/dusk only. ${helpButton("fish_stress")}</div>`
        : "";

      // Curated fishery quality (premier/excellent/good/fair) — editorial tier
      // that spans MN/WI/IA, distinct from the WI-only DNR-inferred tier below.
      const fishery = fc.fishery;
      const fisheryTitle = [
        fishery && fishery.wild_population ? "Wild, self-sustaining population" : "",
        fishery && fishery.notes ? fishery.notes : "",
      ].filter(Boolean).join(" · ").replace(/"/g, "&quot;");
      const fisheryChip = (fishery && fishery.tier)
        ? `<span class="fishery-chip tier-${fishery.tier}" title="${fisheryTitle}">${fishery.tier} fishery${fishery.wild_population ? " · wild" : ""}</span>`
        : "";
      // Out-of-region streams (e.g. North Shore) where the Driftless-tuned hatch
      // model is less reliable carry a caveat; null for in-region reaches.
      const caveatBanner = fc.model_caveat
        ? `<div class="warning-banner soft">ℹ ${fc.model_caveat}</div>`
        : "";

      const dnr = fc.dnr_summary;
      const dnrBlock = dnr
        ? `<section class="section dnr">
            <h3>Fishery tier</h3>
            <p class="dnr-tier">${dnr.inferred_tier || "—"}</p>
            ${dnr.top_reg_category ? `<p class="dnr-reg muted"><strong>WI DNR reg:</strong> ${dnr.top_reg_category}</p>` : ""}
            ${dnr.top_gear_restrictions ? `<p class="dnr-gear muted"><strong>Gear:</strong> ${dnr.top_gear_restrictions}</p>` : ""}
            <p class="dnr-source muted">source: WI DNR trout regulations</p>
          </section>`
        : "";

      const days = groupByDay(hours);
      const bestWindow = findBestWindow(hours);
      const planHTML = renderFishingPlan(hours, now, bestWindow);
      const seasonHTML = renderSeasonBanner(fc.season);
      const tripHTML = renderTripCall(fc.days);
      const tacticsHour = tacticsHourFor(hours, fc.days);
      const tacticsHTML = renderTactics(
        tacticsHour.tactics,
        tacticsHour === now ? "For right now" : `For today's best window, ${(fc.days && fc.days[0] && fc.days[0].best_window.label) || ""}`,
      );

      const seasonChip = fc.season
        ? `<span class="chip ${fc.season.status === "closed" ? "chip-closed" : fc.season.status === "catch_and_release" ? "chip-info" : "chip-accent"}">${escapeHtml(fc.season.label)}</span>`
        : "";
      const springChip = fc.spring_influenced ? `<span class="chip chip-info">Spring creek</span>` : "";
      const stateChip = fc.state ? `<span class="chip">${escapeHtml(fc.state)}${fc.trout_class ? ` · Class ${escapeHtml(fc.trout_class)}` : ""}</span>` : "";
      body.innerHTML = `
        <header class="reach-head">
          <h2 class="reach-title">${escapeHtml(fc.stream_name)}</h2>
          <p class="reach-sub">${escapeHtml(fc.segment_name || "")}</p>
          <div class="reach-chips">${seasonChip}${stateChip}${springChip}${fisheryChip}</div>
        </header>
        ${seasonHTML}
        ${stressBanner}
        ${caveatBanner}
        ${renderHero(fc.days, nowScore)}
        ${tripHTML}
        ${tacticsHTML}
        ${renderOnTheWater(tacticsHour)}
        ${renderConditions(now)}
        <section class="section">
          <h3>Hour by hour</h3>
          ${renderSparkline(hours)}
          <div id="spark-tooltip" class="spark-tooltip"></div>
          <p class="spark-legend muted">Hover or drag across the chart for any hour.</p>
        </section>
        <details class="section why">
          <summary>Why this forecast</summary>
          <div class="why-body">
            <div class="chips">${waterBadge}${anomalyChip}${baroChip}${regimeChip}</div>
            <p class="explanation">${escapeHtml(now.explanation || "")}</p>
            ${planHTML}
            ${renderDayStrip(days)}
            <h3 style="margin-top:14px">Score breakdown ${helpButton("score_overview")}</h3>
            ${renderScoreBreakdown(now)}
          </div>
        </details>
        ${dnrBlock}
        <section class="section catch-log-section">
          <h3>Trip reports
            ${residualsByReach[fc.reach_id] && residualsByReach[fc.reach_id].n >= 3
              ? (() => {
                  const r = residualsByReach[fc.reach_id];
                  const v = Math.round(r.residual * 100);
                  const sign = v >= 0 ? "+" : "";
                  return `<span class="residual-tag" title="Score adjusted by ${r.n} angler reports">calibrated ${sign}${v}</span>`;
                })()
              : ""}
          </h3>
          <div class="quick-rate" role="group" aria-label="One-tap trip report">
            <span class="quick-rate-prompt">Just got off the water? Save a fast score first, then add what was hatching and what worked.</span>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="0" title="Skunked">😶</button>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="1" title="A few">🙂</button>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="2" title="Solid">😄</button>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="3" title="Great">🤩</button>
          </div>
          <button id="open-catch-log" class="open-catch-log" type="button">+ Add details</button>
          <div id="catch-log-form" class="catch-log-form hidden"></div>
          <button id="browse-all-reports" class="open-catch-log" type="button">Browse all recent reports</button>
          <ul id="catch-log-list" class="catch-log-list"><li class="muted small">loading recent reports…</li></ul>
        </section>
      `;
      attachSparklineHover(hours);
      attachCatchLog(fc.reach_id);
    } catch (err) {
      body.innerHTML = `<p class="error">Failed to load: ${err.message}</p>`;
    }
  }

  let currentView = "windows";  // "windows" | "reports" | "calendar" | "detail" | "learn"
  let currentDetailReachId = null;

  // ── Time scrubber ────────────────────────────────────────────────────────
  async function loadScrubGrid() {
    try {
      const resp = await fetch(`${API_BASE}/scores-grid?hours=168`);
      if (!resp.ok) {
        markScrubUnavailable();
        return;
      }
      scrubGrid = await resp.json();
      const slider = document.getElementById("scrub-slider");
      const scrubber = document.getElementById("scrubber");
      const n = (scrubGrid.hours || []).length;
      if (slider) {
        slider.max = String(Math.max(0, n - 1));
        slider.disabled = n < 2;
      }
      if (scrubber) scrubber.classList.toggle("scrubber-empty", n < 2);
      updateScrubLabel();
      renderAllStreams();
    } catch (err) {
      markScrubUnavailable();
    }
  }

  function markScrubUnavailable() {
    const slider = document.getElementById("scrub-slider");
    const scrubber = document.getElementById("scrubber");
    if (slider) slider.disabled = true;
    if (scrubber) scrubber.classList.add("scrubber-empty");
    const lbl = document.getElementById("scrub-label");
    if (lbl) lbl.textContent = "no forecast yet";
  }

  function scrubLabel() {
    const hours = (scrubGrid && scrubGrid.hours) || [];
    if (!hours.length) return scrubGrid ? "no forecast yet" : "loading…";
    const iso = hours[Math.min(scrubOffset, hours.length - 1)];
    if (!iso) return "now";
    const dt = new Date(iso);
    const dayHour = dt.toLocaleString(undefined, { weekday: "short", hour: "numeric" });
    if (scrubOffset === 0) return `Now · ${dayHour}`;
    // Distinct format for non-now so the user can tell at a glance whether
    // they've moved off "now".
    return dt.toLocaleString(undefined, { weekday: "short", month: "numeric", day: "numeric", hour: "numeric" });
  }

  function updateScrubLabel() {
    const lbl = document.getElementById("scrub-label");
    if (lbl) lbl.textContent = scrubLabel();
  }

  function attachScrubber() {
    const slider = document.getElementById("scrub-slider");
    const nowBtn = document.getElementById("scrub-now");
    if (!slider) return;
    slider.addEventListener("input", () => {
      scrubOffset = parseInt(slider.value, 10) || 0;
      updateScrubLabel();
      repaintReaches();
    });
    if (nowBtn) {
      nowBtn.addEventListener("click", () => {
        scrubOffset = 0;
        slider.value = "0";
        updateScrubLabel();
        repaintReaches();
      });
    }
  }

  // ── Hash routing ─────────────────────────────────────────────────────────
  // #/reach/{id}  → detail view for that reach (deep-linkable)
  // #/reports     → recent trip reports
  // #/calendar    → hatch calendar tab
  // #/learn       → learn tab
  // #/learn/{id}  → learn tab + topic overlay
  // (no hash)     → best-windows
  function routeFromHash() {
    const raw = (location.hash || "").replace(/^#\/?/, "");
    if (!raw) { setView("windows", { silent: true }); return; }
    const [head, ...rest] = raw.split("/");
    if (head === "reach" && rest[0]) { showReachDetail(rest[0], { silent: true }); return; }
    if (head === "reports") { setView("reports", { silent: true }); return; }
    if (head === "calendar") { setView("calendar", { silent: true }); return; }
    if (head === "learn") {
      setView("learn", { silent: true });
      if (rest[0]) showTopic(rest[0]);
      return;
    }
    if (head === "page" && rest[0]) { showPage(rest[0], { silent: true }); return; }
    setView("windows", { silent: true });
  }

  function writeHash(hash) {
    if (location.hash === hash) return;
    history.replaceState(null, "", hash || location.pathname);
  }

  function setView(view, opts = {}) {
    currentView = view;
    const sections = ["best-windows", "reports", "calendar", "detail", "learn", "page"];
    const map_ = { "best-windows": "windows", "reports": "reports", "calendar": "calendar", "detail": "detail", "learn": "learn", "page": "page" };
    sections.forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle("hidden", map_[id] !== view);
    });
    // "Near me" and the freshness/best-pick card belong to the home view only.
    ["near-me", "planning-summary"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle("hidden", view !== "windows");
    });
    document.querySelectorAll(".tab").forEach((b) => {
      b.classList.toggle("tab-active", b.dataset.view === view);
    });
    // Mobile: collapse the sidebar overlay state into a class so styles can target it.
    document.body.classList.toggle("view-detail", view === "detail");
    if (view === "reports") loadReports();
    if (view === "calendar") loadCalendar(currentDetailReachId);
    if (view === "learn") loadLearn();
    if (!opts.silent) {
      if (view === "windows") writeHash("");
      else if (view === "reports") writeHash("#/reports");
      else if (view === "calendar") writeHash("#/calendar");
      else if (view === "learn") writeHash("#/learn");
    }
  }

  const PAGES = {
    about: {
      title: "About Driftless Cast",
      html: `
        <p>Driftless Cast is a fishing forecast for the trout streams of the Driftless Area — southeast Minnesota, western Wisconsin and northeast Iowa.
        For every stream, every hour for the next week, it answers the questions you ask before you drive: <strong>is it worth going, when, and what do I tie on?</strong></p>
        <h4>How it works</h4>
        <ul>
          <li><strong>Water</strong> — live USGS and NOAA gauge flows compared with decades of history for the date; water temperature measured or modeled from air temperature with curves fit to Driftless streams.</li>
          <li><strong>Bugs</strong> — a hatch calendar for 19 Driftless food sources built from the MN DNR southeast Minnesota hatch chart and guide field reports, adjusted by degree-days and this year's warmth.</li>
          <li><strong>Weather</strong> — the National Weather Service hourly forecast: cloud, wind, rain and thunderstorms.</li>
          <li><strong>Rules</strong> — Minnesota, Wisconsin and Iowa trout seasons, a spawning advisory, and hard stops for lightning, dangerous cold and lethal water temperatures.</li>
        </ul>
        <p>Every number traces to a source in the Learn tab. When the evidence is weak we say so.</p>
        <p class="muted">Forecasts are guidance, not guarantees. Always check current regulations, landowner access and conditions on site.</p>`,
    },
    privacy: {
      title: "Privacy",
      html: `
        <p>Driftless Cast has no accounts, no ads and no third-party trackers.</p>
        <ul>
          <li><strong>Location</strong> — "near me" uses your browser's location once, rounded to about 100 m, to sort streams by distance. It is not stored.</li>
          <li><strong>Trip reports</strong> — what you submit (stream, date, how it went, optional notes and display name) is stored to calibrate the model and shown to other anglers. Don't include anything you wouldn't post publicly.</li>
          <li><strong>On your device</strong> — your reporter name is remembered in your browser's local storage.</li>
          <li><strong>Server logs</strong> — standard request logs (IP address, time, page) are kept briefly by our host for security and abuse prevention.</li>
        </ul>`,
    },
    data: {
      title: "Data sources",
      html: `
        <p>All live inputs are public-domain U.S. government data:</p>
        <ul>
          <li>USGS Water Data — streamflow, daily-flow statistics, water temperature</li>
          <li>NOAA National Weather Service — hourly forecast, precipitation (QPF)</li>
          <li>NOAA National Water Prediction Service — river gauges and flow forecasts</li>
          <li>NOAA Regional Climate Centers (ACIS) — historical daily air temperature</li>
          <li>USGS NHDPlus HR — stream centerlines · USGS The National Map — basemap</li>
          <li>MN / WI / IA DNR — trout-stream classes and season rules</li>
        </ul>
        <p>Hatch timing draws on the MN DNR southeast Minnesota hatch chart and published entomology; tactics on Driftless guide reports. Full citations are in the Learn tab.</p>`,
    },
  };

  function showPage(id, opts = {}) {
    const page = PAGES[id];
    if (!page) { setView("windows"); return; }
    currentView = "page";
    setView("page", { silent: true });
    document.getElementById("page-body").innerHTML =
      `<article class="page"><h2>${escapeHtml(page.title)}</h2>${page.html}</article>`;
    document.getElementById("sidebar").scrollTop = 0;
    if (!opts.silent) writeHash(`#/page/${id}`);
  }

  // Education content — grouped into categories so the page reads as a
  // navigable index, not a flat list of jargon.
  let educationTopics = null;
  let educationCache = {};
  const LEARN_CATEGORIES = [
    { id: "score",   title: "The score",          icon: "🎯", ids: ["score_overview", "regimes", "season_and_tactics", "validation", "honest_limits"] },
    { id: "water",   title: "Water & temp",       icon: "💧", ids: ["water_temp_zone", "fish_stress", "mohseni"] },
    { id: "flow",    title: "Flow",               icon: "🌊", ids: ["flow_percentile", "flow_trend", "flow_recession"] },
    { id: "hatch",   title: "Hatches & timing",   icon: "🪰", ids: ["degree_days", "emergence_hour", "anomaly_shift", "drift_window"] },
    { id: "weather", title: "Weather signals",    icon: "☁",  ids: ["weather_match", "barometric_pressure", "sun_angle"] },
    { id: "model",   title: "Behind the model",   icon: "🧪", ids: ["data_sources"] },
  ];

  function topicSummary(topic) {
    // Use the first sentence of the first paragraph as the card subhead. Cap
    // at 110 chars so cards stay scannable.
    const para = (topic && topic.body && topic.body[0]) || "";
    const stripped = para.replace(/\*\*(.+?)\*\*/g, "$1");
    const cut = stripped.search(/[.!?]\s/);
    const sentence = cut > 0 ? stripped.slice(0, cut + 1) : stripped;
    return sentence.length > 110 ? sentence.slice(0, 107) + "…" : sentence;
  }

  async function ensureEducationLoaded() {
    if (educationTopics) return;
    try {
      const resp = await fetch(`${API_BASE}/education`);
      const data = await resp.json();
      const list = data.topics || [];
      educationTopics = {};
      list.forEach((t) => { educationTopics[t.id] = t; });
      // Pre-fetch summaries lazily — only the visible category.
    } catch {
      educationTopics = {};
    }
  }

  async function loadLearn() {
    const body = document.getElementById("learn-body");
    body.innerHTML = `<p class="muted">loading…</p>`;
    await ensureEducationLoaded();
    if (!educationTopics || !Object.keys(educationTopics).length) {
      body.innerHTML = `<p class="muted">Couldn't load explainers. Check that the API is running.</p>`;
      return;
    }
    // Reliability dashboard goes at the top — it's "is this model right?"
    // and that's the most important question a user has.
    let reliabilityHtml = "";
    try {
      const r = await fetch(`${API_BASE}/reliability`);
      if (r.ok) reliabilityHtml = renderReliability(await r.json());
    } catch {}
    // Pull full topic bodies in parallel so we can show one-line summaries on
    // the cards. Cheap — 15 small JSON files.
    const idsToFetch = Object.keys(educationTopics).filter((id) => !educationCache[id]);
    await Promise.all(idsToFetch.map(async (id) => {
      try {
        const resp = await fetch(`${API_BASE}/education/${id}`);
        if (resp.ok) educationCache[id] = await resp.json();
      } catch {}
    }));
    const html = LEARN_CATEGORIES.map((cat) => {
      const cards = cat.ids.filter((id) => educationTopics[id]).map((id) => {
        const t = educationCache[id] || educationTopics[id];
        const summary = topicSummary(t);
        return `
          <button class="learn-card" type="button" data-id="${id}">
            <span class="learn-card-title">${t.title}</span>
            ${summary ? `<span class="learn-card-sub">${escapeHtml(summary)}</span>` : ""}
            <span class="learn-card-arrow" aria-hidden="true">→</span>
          </button>
        `;
      }).join("");
      if (!cards) return "";
      return `
        <section class="learn-cat">
          <h3 class="learn-cat-title"><span class="learn-cat-icon" aria-hidden="true">${cat.icon}</span>${cat.title}</h3>
          <div class="learn-cards">${cards}</div>
        </section>
      `;
    }).join("");
    body.innerHTML = `
      <p class="learn-lede">Plain-language explainers for every signal the model uses. Each one is the same content the <span class="help-btn-static">?</span> buttons open inline.</p>
      ${reliabilityHtml}
      ${html}
    `;
    body.querySelectorAll(".learn-card").forEach((b) => {
      b.addEventListener("click", () => showTopic(b.dataset.id));
    });
  }

  async function showTopic(topicId) {
    if (!educationCache[topicId]) {
      try {
        const resp = await fetch(`${API_BASE}/education/${topicId}`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        educationCache[topicId] = await resp.json();
      } catch (err) {
        alert(`Couldn't load topic: ${err.message}`);
        return;
      }
    }
    const t = educationCache[topicId];
    const renderInline = (s) => escapeHtml(s || "")
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      // Inline reference markers like [3] become superscripts.
      .replace(/\[(\d+)\]/g, "<sup class=\"ref-marker\">$1</sup>");
    // Treat first paragraph as a lede (slightly larger), then standard body.
    const body = (t.body || []);
    const lede = body.length ? `<p class="topic-lede">${renderInline(body[0])}</p>` : "";
    const rest = body.slice(1).map((p) => `<p>${renderInline(p)}</p>`).join("");
    const refs = (t.references || []).length
      ? `<aside class="topic-refs">
           <h4>Sources</h4>
           <ol>${t.references.map((r) => `<li id="ref-${r.id}">${renderInline(r.text)}</li>`).join("")}</ol>
         </aside>`
      : `<p class="muted small">Sources: see component sections (water temp, flow, weather, hatches).</p>`;
    showOverlay(`
      <article class="topic-article">
        <header class="topic-header">
          <h3>${t.title}</h3>
        </header>
        ${lede}
        ${rest}
        ${refs}
      </article>
    `);
  }

  // ── Reliability dashboard ─────────────────────────────────────────────────
  function renderReliability(data) {
    const n = data.n || 0;
    if (n < 5) {
      return `
        <section class="reliability-card reliability-empty">
          <h3 class="reliability-title">Is the model right? <span class="reliability-status">gathering data</span></h3>
          <p class="reliability-empty-body">
            We compare what the model predicted to what anglers actually report.
            With only <strong>${n}</strong> trip ${n === 1 ? "report" : "reports"} so far,
            we don't have enough to score the model yet — we need ~30+ for meaningful calibration.
          </p>
          <p class="reliability-empty-body">
            <strong>Help us:</strong> after any trip, tap "Log a trip" on the reach you fished.
            Every report tightens the model.
          </p>
        </section>
      `;
    }
    const bins = data.bins || [];
    const brier = data.brier_score;
    // Tiny SVG calibration plot: x = mean predicted, y = mean reported.
    const W = 280, H = 180, pad = 28;
    const inner = (axis) => pad + axis * (W - 2 * pad);
    const innerY = (axis) => H - pad - axis * (H - 2 * pad);
    const diagLine = `<line x1="${inner(0)}" y1="${innerY(0)}" x2="${inner(1)}" y2="${innerY(1)}"
                         stroke="#9aa5b1" stroke-dasharray="3 3" stroke-width="1" />`;
    const points = bins.filter(b => b.mean_predicted != null).map(b => {
      const x = inner(b.mean_predicted);
      const y = innerY(b.mean_reported);
      const r = 3 + Math.min(8, b.n);
      return `<circle cx="${x}" cy="${y}" r="${r}" fill="${scoreColor(b.mean_reported)}" stroke="#264653" stroke-width="1" opacity="0.85"><title>n=${b.n}, predicted=${b.mean_predicted.toFixed(2)}, reported=${b.mean_reported.toFixed(2)}</title></circle>`;
    }).join("");
    const ticks = [0, 0.25, 0.5, 0.75, 1.0];
    const xTicks = ticks.map(t => `<text x="${inner(t)}" y="${H - pad + 12}" font-size="9" fill="#6b7280" text-anchor="middle">${t.toFixed(2)}</text>`).join("");
    const yTicks = ticks.map(t => `<text x="${pad - 6}" y="${innerY(t) + 3}" font-size="9" fill="#6b7280" text-anchor="end">${t.toFixed(2)}</text>`).join("");
    const reachCount = Object.keys(data.by_reach_n || {}).length;
    return `
      <section class="reliability-card">
        <h3 class="reliability-title">Is the model right? <span class="reliability-status reliability-status-active">${n} reports · ${reachCount} reach${reachCount===1?"":"es"}</span></h3>
        <p class="reliability-blurb">Each dot is a bin of predicted scores; the dot's height is the average reported success in that bin. Perfect calibration would put every dot on the dashed diagonal.</p>
        <svg viewBox="0 0 ${W} ${H}" class="reliability-svg">
          <rect x="${pad}" y="${pad}" width="${W - 2*pad}" height="${H - 2*pad}" fill="#fafafa" stroke="#e1e6ec" />
          ${diagLine}
          ${xTicks}${yTicks}
          ${points}
          <text x="${W/2}" y="${H - 4}" font-size="10" fill="#374151" text-anchor="middle">predicted score</text>
          <text x="10" y="${H/2}" font-size="10" fill="#374151" text-anchor="middle" transform="rotate(-90 10 ${H/2})">reported success</text>
        </svg>
        <div class="reliability-metrics">
          ${brier != null ? `<div><span class="reliability-metric-name">Brier score</span><span class="reliability-metric-value">${brier.toFixed(3)}</span><span class="reliability-metric-help">${brier < 0.10 ? "excellent" : brier < 0.20 ? "good" : brier < 0.30 ? "fair" : "needs work"}</span></div>` : ""}
        </div>
      </section>
    `;
  }

  // ── Catch log (trip reports) ──────────────────────────────────────────────
  function attachCatchLog(reachId) {
    const openBtn = document.getElementById("open-catch-log");
    const form = document.getElementById("catch-log-form");
    if (openBtn) openBtn.addEventListener("click", () => openCatchLogForm(reachId, form, openBtn));
    const browseBtn = document.getElementById("browse-all-reports");
    if (browseBtn) browseBtn.addEventListener("click", () => setView("reports"));
    // Quick-rate: one-tap submission with default fields. Bypasses the form
    // entirely. The right reflex post-trip is "did I have a good time? tap"
    // — anything more friction-y than that and people don't log.
    document.querySelectorAll(`#detail-body .quick-rate-btn[data-reach="${reachId}"]`).forEach((btn) => {
      btn.addEventListener("click", async () => {
        const success = parseInt(btn.dataset.v, 10);
        const profile = loadReporterProfile();
        btn.disabled = true;
        const original = btn.textContent;
        btn.textContent = "…";
        try {
          const resp = await fetch(`${API_BASE}/catch-log`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              reach_id: reachId,
              fished_at: new Date().toISOString(),
              success,
              reporter_name: profile.name || null,
              method: null, fly_used: null, water_temp_f: null,
              notes: "quick-rate",
            }),
          });
          if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
          // Confirm + refresh the list and residuals.
          btn.textContent = "✓";
          setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1200);
          await loadCatchLog(reachId);
          await loadResiduals();
          repaintReaches();
        } catch (err) {
          btn.textContent = "!";
          setTimeout(() => { btn.textContent = original; btn.disabled = false; }, 1500);
        }
      });
    });
    loadCatchLog(reachId);
  }

  function openCatchLogForm(reachId, formEl, openBtn) {
    if (!formEl) return;
    const profile = loadReporterProfile();
    formEl.classList.remove("hidden");
    if (openBtn) openBtn.classList.add("hidden");
    const nowIso = new Date().toISOString().slice(0, 16);  // for datetime-local
    formEl.innerHTML = `
      <div class="catch-log-form-header">
        <span class="catch-log-form-title">What actually happened?</span>
        <span class="catch-log-form-sub">Capture the fishing experience, not just a score.</span>
      </div>
      <label class="cl-label">How was it?
        <div class="cl-rating" role="radiogroup" aria-label="Trip success">
          <button type="button" class="cl-rate" data-v="0" title="Skunked">😶</button>
          <button type="button" class="cl-rate" data-v="1" title="A few">🙂</button>
          <button type="button" class="cl-rate" data-v="2" title="Solid day">😄</button>
          <button type="button" class="cl-rate" data-v="3" title="Great">🤩</button>
        </div>
      </label>
      <div class="cl-grid">
        <label class="cl-label">Name / alias
          <input type="text" id="cl-name" value="${escapeHtml(profile.name || "")}" placeholder="Ben, DriftlessDan, etc." />
        </label>
        <label class="cl-label">When
          <input type="datetime-local" id="cl-fished-at" value="${nowIso}" />
        </label>
        <label class="cl-label">Time of day
          <select id="cl-window">
            <option value="">—</option>
            <option value="dawn">Dawn</option>
            <option value="morning">Morning</option>
            <option value="afternoon">Afternoon</option>
            <option value="dusk">Dusk / last light</option>
            <option value="night">After dark</option>
          </select>
        </label>
        <label class="cl-label">Method
          <select id="cl-method">
            <option value="">—</option>
            <option value="dry">Dry fly</option>
            <option value="nymph">Nymph</option>
            <option value="streamer">Streamer</option>
            <option value="mixed">Mixed</option>
          </select>
        </label>
        <label class="cl-label cl-label-wide">Topwater activity
          <div class="cl-toggle-grid" id="cl-topwater">
            <button type="button" class="cl-toggle" data-v="0">None</button>
            <button type="button" class="cl-toggle" data-v="1">A few</button>
            <button type="button" class="cl-toggle" data-v="2">Steady</button>
            <button type="button" class="cl-toggle" data-v="3">Popping</button>
          </div>
        </label>
        <label class="cl-label cl-label-wide">What bugs did you see?
          <input type="text" id="cl-insects" placeholder="PMDs, tan caddis, stoneflies crawling, spinners, etc." />
        </label>
        <label class="cl-label">What worked?
          <input type="text" id="cl-worked" placeholder="CDC sulphur, tan elk hair, hopper dropper…" />
        </label>
        <label class="cl-label">What didn't?
          <input type="text" id="cl-didnt-work" placeholder="Heavy nymphs, streamers, big attractors…" />
        </label>
        <label class="cl-label">Fly used
          <input type="text" id="cl-fly" placeholder="e.g. Pheasant Tail #14" />
        </label>
        <label class="cl-label">Water temp °F
          <input type="number" id="cl-water" min="32" max="90" step="1" />
        </label>
        <label class="cl-label cl-label-wide">Notes
          <textarea id="cl-notes" rows="3" placeholder="Fish rose only in the last 30 min. Refused duns, ate cripple and soft hackle."></textarea>
        </label>
      </div>
      <div class="cl-actions">
        <button type="button" id="cl-cancel" class="cl-btn cl-btn-ghost">Cancel</button>
        <button type="button" id="cl-submit" class="cl-btn cl-btn-primary" disabled>Submit</button>
      </div>
      <p id="cl-error" class="error small hidden"></p>
    `;
    let chosenRating = null;
    let chosenTopwater = null;
    formEl.querySelectorAll(".cl-rate").forEach((b) => {
      b.addEventListener("click", () => {
        chosenRating = parseInt(b.dataset.v, 10);
        formEl.querySelectorAll(".cl-rate").forEach(x => x.classList.toggle("cl-rate-on", x === b));
        document.getElementById("cl-submit").disabled = false;
      });
    });
    formEl.querySelectorAll(".cl-toggle").forEach((b) => {
      b.addEventListener("click", () => {
        chosenTopwater = parseInt(b.dataset.v, 10);
        formEl.querySelectorAll(".cl-toggle").forEach(x => x.classList.toggle("cl-toggle-on", x === b));
      });
    });
    document.getElementById("cl-cancel").addEventListener("click", () => closeCatchLogForm(formEl, openBtn));
    document.getElementById("cl-submit").addEventListener("click", async () => {
      if (chosenRating == null) return;
      const fished = document.getElementById("cl-fished-at").value;
      const fishedIso = fished ? new Date(fished).toISOString() : new Date().toISOString();
      const reporterName = document.getElementById("cl-name").value.trim() || null;
      const payload = {
        reach_id: reachId,
        fished_at: fishedIso,
        success: chosenRating,
        reporter_name: reporterName,
        method: document.getElementById("cl-method").value || null,
        session_window: document.getElementById("cl-window").value || null,
        topwater_level: chosenTopwater,
        insect_activity: document.getElementById("cl-insects").value.trim() || null,
        worked: document.getElementById("cl-worked").value.trim() || null,
        didnt_work: document.getElementById("cl-didnt-work").value.trim() || null,
        fly_used: document.getElementById("cl-fly").value || null,
        water_temp_f: parseFloat(document.getElementById("cl-water").value) || null,
        notes: document.getElementById("cl-notes").value || null,
      };
      saveReporterProfile({ name: reporterName || "" });
      const errEl = document.getElementById("cl-error");
      const submitBtn = document.getElementById("cl-submit");
      submitBtn.disabled = true;
      submitBtn.textContent = "Submitting…";
      try {
        const resp = await fetch(`${API_BASE}/catch-log`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        closeCatchLogForm(formEl, openBtn);
        await loadCatchLog(reachId);
        await loadResiduals();
        repaintReaches();
      } catch (err) {
        errEl.textContent = `Couldn't save: ${err.message}`;
        errEl.classList.remove("hidden");
        submitBtn.disabled = false;
        submitBtn.textContent = "Submit";
      }
    });
  }

  function closeCatchLogForm(formEl, openBtn) {
    if (formEl) {
      formEl.classList.add("hidden");
      formEl.innerHTML = "";
    }
    if (openBtn) openBtn.classList.remove("hidden");
  }

  async function loadCatchLog(reachId) {
    const list = document.getElementById("catch-log-list");
    if (!list) return;
    try {
      const resp = await fetch(`${API_BASE}/catch-log?reach_id=${encodeURIComponent(reachId)}&limit=8`);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const rows = await resp.json();
      if (!rows.length) {
        list.innerHTML = `<li class="muted small">No reports yet — be the first.</li>`;
        return;
      }
      list.innerHTML = rows.map(renderCatchLogRow).join("");
    } catch (err) {
      list.innerHTML = `<li class="error small">${err.message}</li>`;
    }
  }

  function renderCatchLogRow(r, opts = {}) {
    const emoji = ["😶", "🙂", "😄", "🤩"];
    const when = new Date(r.fished_at).toLocaleDateString(undefined, { weekday: "short", month: "numeric", day: "numeric" });
    const reachLabel = opts.showReach && r.stream_name
      ? `<span class="cl-method">${escapeHtml(r.stream_name)}</span>`
      : "";
    const method = r.method ? `<span class="cl-method">${escapeHtml(r.method)}</span>` : "";
    const reporter = r.reporter_name ? `<span class="cl-reporter">${escapeHtml(r.reporter_name)}</span>` : "";
    const predicted = r.predicted_score != null ? ` <span class="cl-vs muted small">(model ${Math.round(r.predicted_score * 100)})</span>` : "";
    const chips = [
      r.session_window ? `<span class="report-chip">${escapeHtml(formatSessionWindow(r.session_window))}</span>` : "",
      r.topwater_level != null ? `<span class="report-chip report-chip-topwater-${Number(r.topwater_level)}">${escapeHtml(topwaterLabel(r.topwater_level) || "Topwater")}</span>` : "",
      r.water_temp_f ? `<span class="report-chip">${Math.round(r.water_temp_f)}°F</span>` : "",
      r.fly_used ? `<span class="report-chip">${escapeHtml(r.fly_used)}</span>` : "",
    ].filter(Boolean).join("");
    const outcome = [
      r.worked ? `<div class="cl-outcome-line"><span class="cl-outcome-label">Worked:</span>${escapeHtml(r.worked)}</div>` : "",
      r.didnt_work ? `<div class="cl-outcome-line"><span class="cl-outcome-label">Didn't:</span>${escapeHtml(r.didnt_work)}</div>` : "",
    ].join("");
    const bugLine = r.insect_activity ? `<div class="cl-notes-line">Bugs: ${escapeHtml(r.insect_activity)}</div>` : "";
    const notes = r.notes && r.notes !== "quick-rate" ? `<div class="cl-notes-line">${escapeHtml(r.notes)}</div>` : "";
    return `
      <li class="cl-row">
        <div class="cl-row-head">
          <span class="cl-emoji">${emoji[r.success] || ""}</span>
          <span class="cl-when">${when}</span>
          ${reporter}${reachLabel}${method}${predicted}
        </div>
        ${chips ? `<div class="report-entry-chips">${chips}</div>` : ""}
        ${bugLine}
        ${outcome}
        ${notes}
      </li>
    `;
  }

  async function loadReports() {
    const body = document.getElementById("reports-body");
    if (!body) return;
    body.innerHTML = `<p class="muted">loading…</p>`;
    const profile = loadReporterProfile();
    try {
      const [resp, reliabilityResp] = await Promise.all([
        fetch(`${API_BASE}/catch-log?limit=40`),
        fetch(`${API_BASE}/reliability`),
      ]);
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const rows = await resp.json();
      const reliability = reliabilityResp.ok ? await reliabilityResp.json() : { n: 0, by_reach_n: {} };
      const uniqueReporters = new Set(rows.map((r) => (r.reporter_name || "").trim()).filter(Boolean));
      const reportsHtml = rows.length
        ? rows.map((r) => `
            <li class="report-card">
              <div class="report-card-head">
                <span class="report-card-score">${["😶", "🙂", "😄", "🤩"][r.success] || "📝"}</span>
                <div>
                  <div class="report-card-title">${escapeHtml(r.stream_name || r.reach_id)}</div>
                  <div class="report-card-meta">${escapeHtml(r.segment_name || "")}${r.segment_name ? " · " : ""}${timeLocal(r.fished_at)}${r.reporter_name ? ` · ${escapeHtml(r.reporter_name)}` : ""}</div>
                </div>
              </div>
              ${(r.insect_activity || r.fly_used) ? `<div class="report-card-copy">${r.insect_activity ? `<strong>Bugs:</strong> ${escapeHtml(r.insect_activity)}` : ""}${r.insect_activity && r.fly_used ? " · " : ""}${r.fly_used ? `<strong>Fly:</strong> ${escapeHtml(r.fly_used)}` : ""}</div>` : ""}
              ${(r.worked || r.didnt_work || r.notes) ? `<div class="report-card-outcome">${r.worked ? `<strong>Worked:</strong> ${escapeHtml(r.worked)}` : ""}${r.worked && r.didnt_work ? " · " : ""}${r.didnt_work ? `<strong>Didn't:</strong> ${escapeHtml(r.didnt_work)}` : ""}${(r.worked || r.didnt_work) && r.notes && r.notes !== "quick-rate" ? "<br/>" : ""}${r.notes && r.notes !== "quick-rate" ? escapeHtml(r.notes) : ""}</div>` : ""}
              <div class="report-card-chips">
                ${r.method ? `<span class="report-chip">${escapeHtml(r.method)}</span>` : ""}
                ${r.session_window ? `<span class="report-chip">${escapeHtml(formatSessionWindow(r.session_window))}</span>` : ""}
                ${r.topwater_level != null ? `<span class="report-chip report-chip-topwater-${Number(r.topwater_level)}">${escapeHtml(topwaterLabel(r.topwater_level) || "Topwater")}</span>` : ""}
                ${r.water_temp_f ? `<span class="report-chip">${Math.round(r.water_temp_f)}°F</span>` : ""}
                ${r.predicted_score != null ? `<span class="report-chip">Model ${Math.round(r.predicted_score * 100)}</span>` : ""}
              </div>
            </li>
          `).join("")
        : `<li class="muted small">No reports yet.</li>`;
      body.innerHTML = `
        <div class="reports-toolbar">
          <div class="reports-profile">
            <div class="reports-profile-copy">
              <span class="reports-profile-title">Your reporter profile</span>
              <span class="reports-profile-sub">Saved on this device so your next report is one tap. Shown with your reports.</span>
            </div>
            <div class="reports-profile-row">
              <input id="reports-profile-name" type="text" value="${escapeHtml(profile.name || "")}" placeholder="Name or alias" />
              <button id="reports-profile-save" class="cl-btn cl-btn-primary" type="button">Save</button>
            </div>
          </div>
          <div class="reports-summary-card">
            <div class="reports-profile-copy">
              <span class="reports-profile-title">Community feedback</span>
              <span class="reports-profile-sub">Recent field reports that can calibrate the model.</span>
            </div>
            <div class="reports-summary-grid">
              <div class="reports-stat"><span class="reports-stat-label">Reports</span><span class="reports-stat-value">${reliability.n || rows.length}</span></div>
              <div class="reports-stat"><span class="reports-stat-label">Reaches</span><span class="reports-stat-value">${Object.keys(reliability.by_reach_n || {}).length}</span></div>
              <div class="reports-stat"><span class="reports-stat-label">Reporters</span><span class="reports-stat-value">${uniqueReporters.size}</span></div>
            </div>
          </div>
        </div>
        <ul class="reports-feed">${reportsHtml}</ul>
      `;
      const saveBtn = document.getElementById("reports-profile-save");
      if (saveBtn) {
        saveBtn.addEventListener("click", () => {
          const name = document.getElementById("reports-profile-name").value.trim();
          saveReporterProfile({ name });
          saveBtn.textContent = "Saved";
          setTimeout(() => { saveBtn.textContent = "Save"; }, 1000);
        });
      }
    } catch (err) {
      body.innerHTML = `<p class="error">Failed to load reports: ${err.message}</p>`;
    }
  }

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s).replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function showOverlay(innerHtml) {
    const old = document.getElementById("explainer-overlay");
    if (old) old.remove();
    const overlay = document.createElement("div");
    overlay.id = "explainer-overlay";
    overlay.className = "explainer-overlay";
    overlay.innerHTML = `
      <div class="explainer-card" role="dialog" aria-modal="true">
        <button class="explainer-close" aria-label="Close">×</button>
        ${innerHtml}
      </div>
    `;
    // No inline handlers (blocked by the CSP): close on backdrop click only.
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector(".explainer-close").addEventListener("click", () => overlay.remove());
    document.body.appendChild(overlay);
  }

  function helpButton(topicId) {
    return `<button class="help-btn" data-help="${topicId}" aria-label="What does this mean?" title="What does this mean?">?</button>`;
  }

  // ── Score breakdown — show every multiplier so the score is auditable ─────
  function renderScoreBreakdown(hour) {
    const sb = hour.score_breakdown;
    const model = hour.score_model;
    const confidence = hour.confidence_model;
    const nymph = hour.nymph_score || 0;
    const dry   = hour.dry_score   || 0;
    const headline = hourScore(hour);
    if (!sb) {
      // Older predictions before the breakdown column was added — show
      // just the totals.
      return `
        <div class="score-row"><span>Headline ${helpButton("score_overview")}</span><span>${Math.round(headline * 100)}</span></div>
        <div class="score-row"><span>Nymph ${helpButton("flow_percentile")}</span><span>${Math.round(nymph * 100)}</span></div>
        <div class="score-row"><span>Dry ${helpButton("weather_match")}</span><span>${Math.round(dry * 100)}</span></div>
      `;
    }
    function bar(label, value, helpTopic) {
      const pct = Math.max(0, Math.min(1, value)) * 100;
      const color = scoreColor(value);
      return `
        <div class="sb-row">
          <span class="sb-label">${label} ${helpTopic ? helpButton(helpTopic) : ""}</span>
          <span class="sb-bar"><span class="sb-fill" style="width:${pct.toFixed(0)}%;background:${color}"></span></span>
          <span class="sb-val">${value.toFixed(2)}</span>
        </div>
      `;
    }
    function multiplier(label, value, helpTopic) {
      if (value == null || Number.isNaN(Number(value))) {
        return `
          <div class="sb-row sb-row-mult">
            <span class="sb-label">${label} ${helpTopic ? helpButton(helpTopic) : ""}</span>
            <span class="sb-bar sb-mult-bar"><span class="sb-fill" style="width:0%;background:#94a3b8"></span></span>
            <span class="sb-val sb-mult-neutral">n/a</span>
          </div>
        `;
      }
      value = Number(value);
      const isBoost = value > 1.0;
      const isDamp  = value < 1.0;
      const cls = isBoost ? "sb-mult-up" : isDamp ? "sb-mult-down" : "sb-mult-neutral";
      const pct = Math.max(0, Math.min(1, value / 1.05)) * 100;
      const marker = Math.max(0, Math.min(100, (1.0 / 1.05) * 100));
      const color = isBoost ? "#1f8a55" : isDamp ? "#d95f45" : "#94a3b8";
      return `
        <div class="sb-row sb-row-mult">
          <span class="sb-label">${label} ${helpTopic ? helpButton(helpTopic) : ""}</span>
          <span class="sb-bar sb-mult-bar">
            <span class="sb-mult-neutral-line" style="left:${marker.toFixed(1)}%"></span>
            <span class="sb-fill" style="width:${pct.toFixed(0)}%;background:${color}"></span>
          </span>
          <span class="sb-val ${cls}">×${value.toFixed(2)}</span>
        </div>
      `;
    }

    const lanes = (model && model.lanes) || {};
    const top = sb.top_species;
    const topLine = renderTopSpeciesDetails(top);
    return `
      <div class="sb-totals">
        <div class="sb-total sb-total-lane"><span class="sb-total-label">Nymph lane</span><span class="sb-total-num">${Math.round(((lanes.baseline_nymph != null ? lanes.baseline_nymph : nymph) || 0) * 100)}</span></div>
        <div class="sb-total sb-total-lane"><span class="sb-total-label">Surface</span><span class="sb-total-num">${Math.round(((lanes.surface_window != null ? lanes.surface_window : dry) || 0) * 100)}</span></div>
        <div class="sb-total sb-total-lane"><span class="sb-total-label">Activation</span><span class="sb-total-num">${Math.round(((lanes.activation != null ? lanes.activation : (model && model.aggression)) || 0) * 100)}</span></div>
        ${confidence && confidence.score != null ? `<div class="sb-total"><span class="sb-total-label">Confidence</span><span class="sb-total-num">${Math.round(confidence.score * 100)}</span></div>` : ""}
        <div class="sb-total"><span class="sb-total-label">Headline</span><span class="sb-total-num">${Math.round(headline * 100)}</span></div>
      </div>
      ${model ? `<p class="sb-model-note">${renderHeadlineModelNote(model)}</p>` : ""}
      <h4 class="sb-section-h">Component multipliers (1.0 = full credit)</h4>
      ${bar("Water temp", sb.temperature, "water_temp_zone")}
      ${renderThermalProfileNote(sb)}
      ${bar("Flow percentile", sb.flow_percentile, "flow_percentile")}
      ${bar("Flow trend", sb.flow_trend, "flow_trend")}
      ${bar("Diel activity", sb.diel_activity == null ? 1.0 : sb.diel_activity, "score_overview")}
      ${multiplier("Pressure trend", sb.pressure_factor, "barometric_pressure")}
      ${multiplier("Sun angle (dry only)", sb.sun_factor, "sun_angle")}
      ${renderFlowTauNote(sb)}
      ${model && model.aggression_factors ? renderAggressionFactors(model.aggression_factors) : ""}
      ${confidence ? renderConfidenceNote(confidence) : ""}
      ${topLine}
      <p class="sb-note">Lanes separate steady subsurface fishability from surface activity and short activation windows. Headline is calibrated from those lanes so nymph-only plateaus do not read like boiling-rises days. Click any ${helpButton("score_overview").replace(/<\/?button[^>]*>/g, '?')} to see how a component is computed.</p>
    `;
  }

  function renderTopSpeciesDetails(top) {
    if (!top) return "";
    const name = escapeHtml(top.common_name || top.id || "Top hatch");
    const probability = Math.round((top.probability || 0) * 100);
    const factors = [
      ["season", top.seasonal_score],
      ["DD", top.degree_day_score],
      ["weather", top.weather_score],
      ["timing", top.timing_score],
    ].filter(([, value]) => value != null);
    const factorText = factors.length
      ? ` · ${factors.map(([label, value]) => `${label} ${Math.round(Number(value) * 100)}%`).join(" × ")}`
      : "";
    const windowText = Array.isArray(top.emergence_window) && top.emergence_window.length === 2
      ? ` · window ${top.emergence_window[0]}:00-${top.emergence_window[1]}:00`
      : "";
    return `<div class="sb-top-species">Top hatch: ${name} · ${probability}%${factorText}${windowText}</div>`;
  }

  function renderHeadlineModelNote(model) {
    const source = model.source || "";
    if (source === "nymph_capped") {
      return "Headline is capped because subsurface conditions are strong but hatch/surface probability is weak.";
    }
    if (model.aggression != null && model.aggression >= 0.70) {
      return "Headline is lifted because hatch/surface, flow, pressure, and light conditions point to a more aggressive window.";
    }
    if (model.alignment_bonus_possible) {
      return "Headline is lifted because subsurface conditions and a meaningful surface/hatch signal overlap.";
    }
    if (source === "surface" || source === "dry") {
      return "Headline is led by surface activity because the hatch/rise lane is stronger than the nymph lane.";
    }
    if (source === "activation") {
      return "Headline is led by a short activation window from light, drift, pressure, or hatch-adjacent signals.";
    }
    if (source === "blowout") return "Headline is capped by blowout conditions.";
    if (source === "heat_stress") return "Headline is capped by trout heat-stress conditions.";
    return "Headline is led by subsurface conditions, then compressed at the top end for calibration.";
  }

  function renderAggressionFactors(factors) {
    const flow = factors.flow_change != null ? Math.round(factors.flow_change * 100) : null;
    const light = factors.light_protection != null ? Math.round(factors.light_protection * 100) : null;
    const pressure = factors.pressure_factor != null ? `×${Number(factors.pressure_factor).toFixed(2)}` : null;
    return `<div class="sb-top-species">Aggression inputs: surface ${Math.round((factors.surface || 0) * 100)}% · flow change ${flow}% · pressure ${pressure} · light protection ${light}%</div>`;
  }

  function renderFlowTauNote(sb) {
    if (!sb) return "";
    if (sb.flow_tau_source === "noaa_usgs_fused") {
      return `<div class="sb-top-species">Flow projection: local NOAA trend + USGS proxy percentile</div>`;
    }
    if (sb.flow_display_source === "local_noaa_current") {
      return `<div class="sb-top-species">Flow context: local NOAA flow shown · USGS proxy percentile scored</div>`;
    }
    if (sb.flow_tau_source === "noaa_forecast") {
      return `<div class="sb-top-species">Flow projection: NOAA streamflow forecast</div>`;
    }
    if (sb.flow_tau_hours == null) return "";
    const source = sb.flow_tau_source === "per_gauge_fit" ? "per-gauge fit" : "regional prior";
    return `<div class="sb-top-species">Flow recession: τ ${Number(sb.flow_tau_hours).toFixed(0)}h · ${source}</div>`;
  }

  function renderThermalProfileNote(sb) {
    if (!sb || !sb.thermal_profile || sb.thermal_profile === "class-level thermal model") return "";
    const strength = sb.thermal_spring_strength != null ? ` · strength ${Math.round(sb.thermal_spring_strength * 100)}%` : "";
    return `<div class="sb-top-species">Thermal profile: ${sb.thermal_profile}${strength}</div>`;
  }

  function renderConfidenceNote(confidence) {
    const lead = confidence.lead_hours != null ? ` · ${Math.round(confidence.lead_hours)}h lead` : "";
    const notes = (confidence.notes || []).join(" · ");
    return `<div class="sb-top-species">Confidence inputs: ${notes}${lead}</div>`;
  }

  function barometricChip(deltaMb) {
    // Surface 6h trend even though it already adjusts the score — anglers
    // want to know what the model is reacting to. Threshold matches the
    // forecast_builder.py _pressure_trend_factor cutoffs.
    if (deltaMb == null) return "";
    const abs = Math.abs(deltaMb);
    if (abs < 1.5) return "";
    const sign = deltaMb > 0 ? "rising" : "falling";
    const cls = deltaMb > 0
      ? (deltaMb >= 4 ? "baro-rising-fast" : "baro-rising")
      : (deltaMb <= -4 ? "baro-falling-fast" : "baro-falling");
    const arrow = deltaMb > 0 ? "↑" : "↓";
    return `<span class="baro-chip ${cls}" title="6h pressure trend">${arrow} ${sign} ${abs.toFixed(1)} mb</span>`;
  }

  // Single global handler so we don't have to wire one per topic.
  document.addEventListener("click", (e) => {
    const t = e.target.closest(".help-btn");
    if (t) {
      e.preventDefault();
      e.stopPropagation();
      showTopic(t.dataset.help);
    }
  });

  function closeDetail() {
    setView(currentView === "detail" ? "windows" : currentView);
    currentDetailReachId = null;
    focusReach(null);
  }

  // Selected stream: zoom to it, draw it on top and dim the rest.
  let focusedReachId = null;
  let pendingFly = false;
  function focusReach(reachId, noFly = false) {
    focusedReachId = reachId;
    Object.entries(markersByReach).forEach(([id, { line, halo, reach }]) => {
      if (!line.setStyle || line instanceof L.CircleMarker) return;
      const base = styleForReach(reach);
      const haloOpacity = isClosed(reach) ? 0.6 : 0.95;
      if (!reachId) {
        line.setStyle(base);
        if (halo) halo.setStyle({ opacity: haloOpacity });
        return;
      }
      if (id === reachId) {
        if (halo) { halo.setStyle({ opacity: 1, weight: base.weight + 7 }); halo.bringToFront(); }
        line.setStyle({ ...base, weight: base.weight + 3, opacity: 1 });
        line.bringToFront();
      } else {
        line.setStyle({ ...base, opacity: 0.22 });
        if (halo) halo.setStyle({ opacity: 0.15 });
      }
    });
    if (!reachId || noFly) return;
    const m = markersByReach[reachId];
    if (!m || !m.line.getBounds) { pendingFly = true; return; }
    pendingFly = false;
    const desktop = window.innerWidth > 720;
    map.flyToBounds(m.line.getBounds(), {
      paddingTopLeft: desktop ? [460, 80] : [30, 80],
      paddingBottomRight: desktop ? [80, 100] : [30, Math.round(window.innerHeight * 0.55)],
      maxZoom: 12, duration: 0.6,
    });
  }

  function shortSpeciesName(name) {
    // "Tan Caddis (Hydropsyche / Cinnamon Sedge)" → "Tan Caddis";
    // "Grannom / Black Caddis" → "Grannom"; keeps the label inside the column.
    const s = String(name).split(" (")[0].split(" / ")[0];
    return s.length > 17 ? s.slice(0, 16) + "…" : s;
  }


  function calendarColor(activity) {
    if (activity == null || activity < 0.05) return cssVar("--surface-3", "#edf1ef");
    // Off-white through teal toward saturated
    const a = Math.min(1, activity);
    const t = Math.round(40 + a * 40);   // hue
    const l = Math.round(85 - a * 30);   // lightness
    return `hsl(${t}, 60%, ${l}%)`;
  }

  async function loadCalendar(reachId) {
    const body = document.getElementById("calendar-body");
    body.innerHTML = `<p class="muted">loading…</p>`;
    try {
      const url = reachId ? `${API_BASE}/calendar?reach_id=${reachId}` : `${API_BASE}/calendar`;
      const resp = await fetch(url);
      const data = await resp.json();
      const todayDoy = data.today_doy;
      const samples = data.species[0]?.samples?.length || 1;
      const w = 320, h = 16, padL = 88, padR = 6;
      const inner = w - padL - padR;
      // Month grid (every ~30.4 days)
      const monthMarkers = [];
      const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
      for (let m = 1; m <= 12; m++) {
        // Day-of-year of the 1st of month m (non-leap is fine for a label).
        const doy = Math.round((Date.UTC(2025, m - 1, 1) - Date.UTC(2025, 0, 1)) / 86400000) + 1;
        monthMarkers.push({ doy, x: padL + (doy / 365) * inner, label: MONTHS[m - 1] });
      }
      const todayX = padL + (todayDoy / 365) * inner;
      const MINOR_WEIGHT = 0.2;
      const minor = data.species.filter((sp) => (sp.driftless_weight ?? 1) < MINOR_WEIGHT);
      const rowsHtml = data.species.filter((sp) => (sp.driftless_weight ?? 1) >= MINOR_WEIGHT).map((sp) => {
        const cells = sp.samples.map((s, i) => {
          const x = padL + (s.doy / 365) * inner;
          const wCell = inner / samples + 0.5;
          return `<rect x="${x}" y="0" width="${wCell}" height="${h - 4}" fill="${calendarColor(s.activity)}" />`;
        }).join("");
        return `<div class="cal-row" data-sp="${sp.species_id}">
          <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="cal-svg">
            <text x="0" y="${h-5}" font-size="10" class="cal-label">${escapeHtml(shortSpeciesName(sp.common_name || sp.species_id))}</text>
            ${cells}
            <line x1="${todayX}" y1="0" x2="${todayX}" y2="${h-3}" class="cal-today" stroke-width="1" />
          </svg>
        </div>`;
      }).join("");
      const monthRow = `<svg viewBox="0 0 ${w} 14" preserveAspectRatio="none" class="cal-svg cal-month-row">
        ${monthMarkers.map(m => `<text x="${m.x}" y="11" font-size="9" class="cal-month">${m.label}</text>`).join("")}
      </svg>`;
      const reachNote = reachId
        ? `<p class="muted">For ${reachId} · shifted ${data.shift_days >= 0 ? "+" : ""}${data.shift_days.toFixed(1)}d by recent thermal anomaly</p>`
        : `<p class="muted">Driftless seasonal averages · click a reach for shift adjustment</p>`;
      body.innerHTML = `
        ${reachNote}
        ${monthRow}
        <div class="cal-rows">${rowsHtml}</div>
        <p class="muted spark-legend">Vertical line = today. Shaded band = how likely the bug is active.</p>
        ${minor.length ? `<p class="muted spark-legend">Not shown: ${minor.map((sp) => escapeHtml(shortSpeciesName(sp.common_name || sp.species_id))).join(", ")} — documented elsewhere but not meaningful Driftless hatches.</p>` : ""}
      `;
      body.querySelectorAll(".cal-row").forEach((row) => {
        row.addEventListener("click", () => {
          const sp = data.species.find(s => s.species_id === row.dataset.sp);
          if (sp) showSpeciesDetail(sp);
        });
      });
    } catch (err) {
      body.innerHTML = `<p class="error">${err.message}</p>`;
    }
  }

  function showSpeciesDetail(sp) {
    const flies = (sp.fly_patterns || []).map(p =>
      `<div class="fly"><span class="fly-role">${p.stage || "fly"}</span><span class="fly-name">${p.pattern}${p.size ? ` #${p.size}` : ""}</span></div>`
    ).join("");
    const peakLabel = `${["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"][sp.peak_month - 1]} ${sp.peak_day}`;
    const window = sp.first_present && sp.last_present
      ? `${sp.first_present.slice(5)} → ${sp.last_present.slice(5)}`
      : "—";
    const body = document.getElementById("calendar-body");
    body.querySelectorAll(".cal-detail-overlay").forEach((el) => el.remove());
    body.insertAdjacentHTML("beforeend", `
      <div class="cal-detail-overlay">
        <div class="cal-detail-card" role="dialog" aria-modal="true">
          <button class="cal-close" aria-label="Close">×</button>
          <h3>${escapeHtml(sp.common_name)}</h3>
          <p class="muted">${escapeHtml(sp.scientific_name || "")}</p>
          <p><strong>Peak:</strong> ${peakLabel}</p>
          <p><strong>Window:</strong> ${window}</p>
          <h4>Patterns</h4>
          <div class="flies">${flies}</div>
        </div>
      </div>
    `);
    const overlay = body.querySelector(".cal-detail-overlay:last-child");
    overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector(".cal-close").addEventListener("click", () => overlay.remove());
  }

  // Escape closes any open explainer / species card.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll(".explainer-overlay, .cal-detail-overlay").forEach((el) => el.remove());
  });

  initMap();
  // Mobile bottom-sheet toggle — desktop ignores this (display:none in CSS).
  const toggleBtn = document.getElementById("sidebar-toggle");
  if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
      const open = document.body.classList.toggle("sidebar-open");
      toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }
  document.getElementById("close-detail").addEventListener("click", closeDetail);
  document.querySelectorAll(".tab").forEach((b) => {
    b.addEventListener("click", () => setView(b.dataset.view));
  });
  document.querySelectorAll("[data-view-link]").forEach((b) => {
    b.addEventListener("click", () => setView(b.dataset.viewLink));
  });
  attachScrubber();
  // Offline shell + last-good forecast (web/sw.js). Same-origin prod only.
  if ("serviceWorker" in navigator && API_BASE === "") {
    window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => {}));
  }
  const searchEl = document.getElementById("stream-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      if (currentView !== "windows") setView("windows");
      renderAllStreams();
    });
  }
  const nearBtn = document.getElementById("near-me-btn");
  if (nearBtn) nearBtn.addEventListener("click", loadNearMe);
  // Residuals first so the very first paint of /reaches uses the calibrated
  // score; the call is cheap (one row per reach with >=3 reports).
  loadResiduals().then(() => {
    loadPlanningSummary();
    loadReaches();
    loadBestWindows();
    loadHatchWatch();
    loadScrubGrid();
  });
  // Hash routing — handle inbound link AND back/forward buttons.
  window.addEventListener("hashchange", routeFromHash);
  routeFromHash();
  setInterval(() => {
    loadResiduals();
    loadPlanningSummary();
    loadReaches();
    loadBestWindows();
    loadHatchWatch();
    loadScrubGrid();
    if (currentView === "reports") loadReports();
  }, REFRESH_MS);
})();
