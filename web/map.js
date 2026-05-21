(function () {
  // API base: same-origin in production (single-port FastAPI mounts web/ at /),
  // explicit localhost:8000 when the frontend is being served separately on
  // :8080 by serve_static.py during local dev.
  const API_BASE = (location.port === "8080" || location.protocol === "file:")
    ? "http://localhost:8000"
    : "";
  const REFRESH_MS = 5 * 60 * 1000;

  let map, markersByReach = {};
  let speciesById = null;  // populated once from /calendar — used to render full pattern lists per active species
  let scrubGrid = null;    // { hours: [...], scores: { reach_id: [...] } } for the time scrubber
  let scrubOffset = 0;     // hour index into scrubGrid.hours; 0 = "now"
  let residualsByReach = {}; // { reach_id: { residual: -.20..+.20, n: int } } from /residuals

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

  function scoreColor(score) {
    if (score == null) return "#9aa5b1";
    const s = Math.max(0, Math.min(1, score));
    let lo = SCORE_STOPS[0], hi = SCORE_STOPS[SCORE_STOPS.length - 1];
    for (let i = 0; i < SCORE_STOPS.length - 1; i++) {
      if (s >= SCORE_STOPS[i].t && s <= SCORE_STOPS[i + 1].t) {
        lo = SCORE_STOPS[i]; hi = SCORE_STOPS[i + 1]; break;
      }
    }
    const span = hi.t - lo.t || 1;
    const k = (s - lo.t) / span;
    const r = Math.round(lo.c[0] + (hi.c[0] - lo.c[0]) * k);
    const g = Math.round(lo.c[1] + (hi.c[1] - lo.c[1]) * k);
    const b = Math.round(lo.c[2] + (hi.c[2] - lo.c[2]) * k);
    return `rgb(${r},${g},${b})`;
  }

  // Redundant encoding: line/marker thickness grows with score so the map
  // still ranks correctly when printed grayscale or read by colorblind users.
  // Tuned to be visible against the busy USGS Topo basemap at zoom 8 (the
  // regional default); thin lines disappeared into the topographic shading.
  function scoreWeight(score) {
    if (score == null) return 5;
    const s = Math.max(0, Math.min(1, score));
    return 5 + Math.round(s * 4);  // 5..9 px
  }
  function dotRadius(score) {
    if (score == null) return 6;
    const s = Math.max(0, Math.min(1, score));
    return 7 + Math.round(s * 4);  // 7..11 px
  }

  function scoreLabel(score) {
    if (score == null) return "?";
    if (score >= 0.65) return "Go";
    if (score >= 0.4) return "Fair";
    return "Skip";
  }

  function hourScore(hour) {
    if (!hour) return 0;
    if (hour.combined_score != null) return hour.combined_score;
    return Math.max(hour.nymph_score || 0, hour.dry_score || 0);
  }

  function initMap() {
    map = L.map("map", { zoomControl: true }).setView([43.9, -91.7], 8);
    const osm = L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      attribution: "© OpenStreetMap contributors",
      maxZoom: 18,
    });
    // USGS Topo basemap reads gradient and parking-relevant terrain better than
    // OSM for fishing planning. Default to topo; expose OSM as a toggle for the
    // few reaches where the topo tiles get blocky at z16+.
    const usgsTopo = L.tileLayer(
      "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}",
      {
        attribution: "USGS The National Map",
        maxZoom: 16,
      }
    );
    usgsTopo.addTo(map);
    L.control.layers(
      { "USGS Topo": usgsTopo, "OpenStreetMap": osm },
      {},
      { position: "topright", collapsed: true }
    ).addTo(map);

    addLegendControl();
  }

  function addLegendControl() {
    const legend = L.control({ position: "bottomleft" });
    legend.onAdd = function () {
      const div = L.DomUtil.create("div", "score-legend");
      div.innerHTML = `
        <div class="legend-title">Forecast score</div>
        <div class="legend-row"><span class="legend-swatch" style="background:${scoreColor(0.85)}"></span>Go (65+)</div>
        <div class="legend-row"><span class="legend-swatch" style="background:${scoreColor(0.5)}"></span>Fair (40–64)</div>
        <div class="legend-row"><span class="legend-swatch" style="background:${scoreColor(0.2)}"></span>Skip (&lt;40)</div>
        <div class="legend-row"><span class="legend-swatch" style="background:#9aa5b1"></span>No data</div>
        <div class="legend-foot">thicker line = higher score</div>
      `;
      L.DomEvent.disableClickPropagation(div);
      return div;
    };
    legend.addTo(map);
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
      const [statusResp, windowsResp] = await Promise.all([
        fetch(`${API_BASE}/status`),
        fetch(`${API_BASE}/best-windows?hours=48&limit=4`),
      ]);
      const status = statusResp.ok ? await statusResp.json() : {};
      const windows = windowsResp.ok ? await windowsResp.json() : [];
      const top = windows[0] || null;
      const isFresh = status.is_stale === false;
      const statusClass = isFresh ? "fresh" : "stale";
      const updated = relativeAge(status.stale_minutes);
      const topScore = top ? applyResidual(top.reach_id, top.score) : null;
      const topHtml = top
        ? `<button type="button" class="summary-pick" data-reach="${top.reach_id}">
             <span class="summary-label">Best next 48h</span>
             <strong>${top.stream_name}</strong>
             <span>${top.segment_name || top.state || ""} · ${timeLocal(top.valid_at)} · ${Math.round(topScore * 100)}/100</span>
           </button>`
        : `<div class="summary-pick summary-pick-empty"><span class="summary-label">Best next 48h</span><strong>Forecast warming up</strong><span>No ranked windows yet.</span></div>`;
      el.innerHTML = `
        <div class="freshness freshness-${statusClass}">
          <span class="freshness-dot"></span>
          <span>${isFresh ? "Fresh forecast" : "Stale forecast"}</span>
          <span class="freshness-age">updated ${updated}</span>
        </div>
        ${topHtml}
      `;
      const btn = el.querySelector(".summary-pick[data-reach]");
      if (btn) btn.addEventListener("click", () => showReachDetail(btn.dataset.reach));
    } catch (err) {
      el.innerHTML = `<p class="error">Forecast status unavailable: ${err.message}</p>`;
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

  function styleForReach(reach) {
    const score = effectiveScore(reach);
    return {
      color: scoreColor(score),
      weight: scoreWeight(score),
      opacity: score == null ? 0.55 : 0.95,
      lineCap: "round",
      lineJoin: "round",
    };
  }

  function reachTooltip(reach) {
    const score = effectiveScore(reach);
    const segment = reach.segment_name ? `${reach.segment_name} · ` : "";
    if (score == null) return `${reach.stream_name} ${segment}(no forecast yet)`;
    return `${reach.stream_name} — ${scoreLabel(score)} (${Math.round(score * 100)})`;
  }

  async function loadReaches() {
    const summaryEl = document.getElementById("summary");
    try {
      const resp = await fetch(`${API_BASE}/reaches`);
      const reaches = await resp.json();
      const group = L.layerGroup();
      markersByReach = {};
      let withScore = 0;
      reaches.forEach((reach) => {
        if (reach.centroid_lat == null || reach.centroid_lon == null) return;
        const score = effectiveScore(reach);
        if (score != null) withScore += 1;

        let geom = null;
        if (reach.geometry_geojson) {
          try { geom = JSON.parse(reach.geometry_geojson); } catch {}
        }
        let primaryLayer;
        const latlngs = geoLatLngs(geom);
        if (latlngs && latlngs.length >= 1) {
          // Render the reach as a polyline. Keep it as a separate layer (not
          // L.geoJSON) so we can update its style cheaply when the scrubber moves.
          primaryLayer = L.polyline(latlngs, styleForReach(reach));
          // Halo line behind it, fixed white, so the colored line reads on
          // any basemap (USGS topo green hillshade was washing out yellows).
          const halo = L.polyline(latlngs, {
            color: "#ffffff",
            weight: scoreWeight(score) + 6,
            opacity: 0.85,
            lineCap: "round",
          });
          group.addLayer(halo);
          group.addLayer(primaryLayer);
        } else {
          primaryLayer = L.circleMarker([reach.centroid_lat, reach.centroid_lon], {
            radius: dotRadius(score),
            color: scoreColor(score),
            weight: 3,
            fillColor: scoreColor(score),
            fillOpacity: score == null ? 0.45 : 0.85,
          });
          group.addLayer(primaryLayer);
        }
        primaryLayer.bindTooltip(reachTooltip(reach), { sticky: true });
        primaryLayer.on("click", () => showReachDetail(reach.reach_id));

        // Centroid marker — primary tap target. Sized with score, filled with
        // score color, dark border for contrast against the topo basemap.
        const dot = L.circleMarker([reach.centroid_lat, reach.centroid_lon], {
          radius: dotRadius(score),
          color: "#1f2933",
          weight: 2,
          fillColor: scoreColor(score),
          fillOpacity: score == null ? 0.5 : 0.95,
        });
        dot.bindTooltip(reachTooltip(reach));
        dot.on("click", () => showReachDetail(reach.reach_id));
        group.addLayer(dot);

        markersByReach[reach.reach_id] = { line: primaryLayer, dot, reach };
      });
      group.addTo(map);
      summaryEl.textContent = `${reaches.length} reaches · ${withScore} scored`;
    } catch (err) {
      summaryEl.innerHTML = `<span class="error">Error loading reaches: ${err.message}</span>`;
    }
  }

  function repaintReaches() {
    Object.values(markersByReach).forEach(({ line, dot, reach }) => {
      const score = effectiveScore(reach);
      if (line.setStyle) line.setStyle(styleForReach(reach));
      if (line.setTooltipContent) line.setTooltipContent(reachTooltip(reach));
      if (dot) {
        if (dot.setStyle) {
          dot.setStyle({
            color: "#1f2933",
            fillColor: scoreColor(score),
            fillOpacity: score == null ? 0.5 : 0.95,
          });
        }
        // setRadius is on circleMarker — updates the dot size with the new score.
        if (dot.setRadius) dot.setRadius(dotRadius(score));
        if (dot.setTooltipContent) dot.setTooltipContent(reachTooltip(reach));
      }
    });
  }

  async function loadBestWindows() {
    const listEl = document.getElementById("windows-list");
    try {
      const resp = await fetch(`${API_BASE}/best-windows?hours=72&limit=10`);
      let windows = await resp.json();
      if (!windows.length) {
        listEl.innerHTML = `<li class="muted">forecast pipeline still warming up — retrying…</li>`;
        return;
      }
      // Apply per-reach residual on the displayed score and re-sort so the
      // ranked list agrees with the on-map colors.
      windows = windows
        .map((w) => {
          const score = applyResidual(w.reach_id, w.score);
          const rank_score = w.rank_score != null ? applyResidual(w.reach_id, w.rank_score) : score;
          return { ...w, score, rank_score };
        })
        .sort((a, b) => b.rank_score - a.rank_score);
      listEl.innerHTML = windows.map((w) => {
        const score = w.score;
        const verdict = scoreLabel(score);
        const color = scoreColor(score);
        const proxyText = w.gauge_is_proxy
          ? ` · proxy ${w.proxy_distance_km != null ? `~${Math.round(w.proxy_distance_km)} km` : "gauge"}`
          : "";
        return `
          <li data-reach="${w.reach_id}">
            <button type="button" class="window-row">
              <span class="score-pill" style="background:${color}">${verdict}</span>
              <span class="window-main">
                <span class="window-name">${w.stream_name}</span>
                <span class="window-sub">${w.segment_name || ""} · ${timeLocal(w.valid_at)}${w.aggression_score != null ? ` · aggression ${Math.round(w.aggression_score * 100)}` : ""}${w.confidence_score != null ? ` · confidence ${Math.round(w.confidence_score * 100)}` : ""}${proxyText}</span>
              </span>
              <span class="window-score">${(score * 100).toFixed(0)}</span>
            </button>
          </li>
        `;
      }).join("");
      listEl.querySelectorAll("li[data-reach]").forEach((li) => {
        li.querySelector("button").addEventListener("click", () => showReachDetail(li.dataset.reach));
      });
    } catch (err) {
      listEl.innerHTML = `<li class="error">${err.message}</li>`;
    }
  }

  function renderFlies(flies, activeSpecies, allSpecies) {
    // Show full per-active-species pattern lists, grouped, plus the primary/dropper pick.
    const rows = [];
    if (flies && flies.primary) {
      rows.push(`<div class="fly fly-pick"><span class="fly-role">★ Primary</span><span class="fly-name">${flies.primary.pattern}${flies.primary.size ? ` #${flies.primary.size}` : ""}</span></div>`);
    }
    if (flies && flies.dropper) {
      rows.push(`<div class="fly fly-pick"><span class="fly-role">+ Dropper</span><span class="fly-name">${flies.dropper.pattern}${flies.dropper.size ? ` #${flies.dropper.size}` : ""}</span></div>`);
    }
    return `<div class="flies">${rows.join("")}</div>` +
      renderHatchPatternBoxes(activeSpecies, allSpecies);
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

  async function showReachDetail(reachId, opts = {}) {
    currentDetailReachId = reachId;
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
      const topActive = (now.active_species || [])
        .slice().sort((a, b) => (b.probability || 0) - (a.probability || 0));
      const activeHTML = topActive.length
        ? `<ul class="hatches">${topActive.map(s => {
            const name = s.common_name || s.id;
            const dd = s.dd_progress != null ? ` · DD ${Math.round(s.dd_progress * 100)}%` : "";
            return `<li><strong>${name}</strong> · ${Math.round((s.probability || 0) * 100)}%${dd}</li>`;
          }).join("")}</ul>`
        : `<p class="muted">No dry-fly hatch active right now.</p>`;

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
      const bestDay = days.reduce((a, b) => (b.peak > a.peak ? b : a), { peak: 0 });
      const bestWindowHTML = bestWindow && bestWindow.score > nowScore + 0.1
        ? `<p class="best-window">Best ahead: <strong>${formatWindowRange(bestWindow.start, bestWindow.end)}</strong> · ${Math.round(bestWindow.score * 100)}/100</p>`
        : "";

      body.innerHTML = `
        <h2>${fc.stream_name}</h2>
        <p class="segment">${fc.segment_name || ""}</p>
        ${stressBanner}
        <div class="verdict" style="border-left-color:${scoreColor(nowScore)}">
          <div class="verdict-head">
            <span class="score-pill" style="background:${scoreColor(nowScore)}">${scoreLabel(nowScore)}</span>
            <span class="score-num">${Math.round(nowScore * 100)} / 100</span>
            ${helpButton("score_overview")}
          </div>
          <div class="chips">${waterBadge}${anomalyChip}${baroChip}${regimeChip}</div>
          ${(now.regime && now.regime.code !== "NORMAL" && now.regime.fly_hint) ? `<p class="regime-hint">↳ ${now.regime.fly_hint}</p>` : ""}
          <p class="explanation">${now.explanation || ""}</p>
          ${bestWindowHTML}
        </div>
        <section class="section">
          <h3>7-day outlook</h3>
          ${renderDayStrip(days)}
          ${renderSparkline(hours)}
          <div id="spark-tooltip" class="spark-tooltip"></div>
          <p class="spark-legend muted">hover or drag across the chart for any hour</p>
        </section>
        <section class="section">
          <h3>Active hatches</h3>
          ${activeHTML}
        </section>
        <section class="section">
          <h3>Recommended flies</h3>
          ${renderFlies(now.flies, now.active_species, speciesById)}
        </section>
        <section class="section scores">
          <h3>Score breakdown</h3>
          ${renderScoreBreakdown(now)}
        </section>
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
            <span class="quick-rate-prompt">Just got off the water?</span>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="0" title="Skunked">😶</button>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="1" title="A few">🙂</button>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="2" title="Solid">😄</button>
            <button class="quick-rate-btn" data-reach="${fc.reach_id}" data-v="3" title="Great">🤩</button>
          </div>
          <button id="open-catch-log" class="open-catch-log" type="button">+ Add details</button>
          <div id="catch-log-form" class="catch-log-form hidden"></div>
          <ul id="catch-log-list" class="catch-log-list"><li class="muted small">loading recent reports…</li></ul>
        </section>
      `;
      attachSparklineHover(hours);
      attachCatchLog(fc.reach_id);
    } catch (err) {
      body.innerHTML = `<p class="error">Failed to load: ${err.message}</p>`;
    }
  }

  let currentView = "windows";  // "windows" | "calendar" | "detail" | "learn"
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
  // #/calendar    → hatch calendar tab
  // #/learn       → learn tab
  // #/learn/{id}  → learn tab + topic overlay
  // (no hash)     → best-windows
  function routeFromHash() {
    const raw = (location.hash || "").replace(/^#\/?/, "");
    if (!raw) { setView("windows", { silent: true }); return; }
    const [head, ...rest] = raw.split("/");
    if (head === "reach" && rest[0]) { showReachDetail(rest[0], { silent: true }); return; }
    if (head === "calendar") { setView("calendar", { silent: true }); return; }
    if (head === "learn") {
      setView("learn", { silent: true });
      if (rest[0]) showTopic(rest[0]);
      return;
    }
    setView("windows", { silent: true });
  }

  function writeHash(hash) {
    if (location.hash === hash) return;
    history.replaceState(null, "", hash || location.pathname);
  }

  function setView(view, opts = {}) {
    currentView = view;
    const sections = ["best-windows", "calendar", "detail", "learn"];
    const map_ = { "best-windows": "windows", "calendar": "calendar", "detail": "detail", "learn": "learn" };
    sections.forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.classList.toggle("hidden", map_[id] !== view);
    });
    document.querySelectorAll(".tab").forEach((b) => {
      b.classList.toggle("tab-active", b.dataset.view === view);
    });
    // Mobile: collapse the sidebar overlay state into a class so styles can target it.
    document.body.classList.toggle("view-detail", view === "detail");
    if (view === "calendar") loadCalendar(currentDetailReachId);
    if (view === "learn") loadLearn();
    if (!opts.silent) {
      if (view === "windows") writeHash("");
      else if (view === "calendar") writeHash("#/calendar");
      else if (view === "learn") writeHash("#/learn");
    }
  }

  // Education content — grouped into categories so the page reads as a
  // navigable index, not a flat list of jargon.
  let educationTopics = null;
  let educationCache = {};
  const LEARN_CATEGORIES = [
    { id: "score",   title: "The score",          icon: "🎯", ids: ["score_overview", "regimes", "validation", "honest_limits"] },
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
    // Quick-rate: one-tap submission with default fields. Bypasses the form
    // entirely. The right reflex post-trip is "did I have a good time? tap"
    // — anything more friction-y than that and people don't log.
    document.querySelectorAll(`#detail-body .quick-rate-btn[data-reach="${reachId}"]`).forEach((btn) => {
      btn.addEventListener("click", async () => {
        const success = parseInt(btn.dataset.v, 10);
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
    formEl.classList.remove("hidden");
    if (openBtn) openBtn.classList.add("hidden");
    const nowIso = new Date().toISOString().slice(0, 16);  // for datetime-local
    formEl.innerHTML = `
      <label class="cl-label">How was it?
        <div class="cl-rating" role="radiogroup" aria-label="Trip success">
          <button type="button" class="cl-rate" data-v="0" title="Skunked">😶</button>
          <button type="button" class="cl-rate" data-v="1" title="A few">🙂</button>
          <button type="button" class="cl-rate" data-v="2" title="Solid day">😄</button>
          <button type="button" class="cl-rate" data-v="3" title="Great">🤩</button>
        </div>
      </label>
      <label class="cl-label">When
        <input type="datetime-local" id="cl-fished-at" value="${nowIso}" />
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
      <label class="cl-label">Fly used (optional)
        <input type="text" id="cl-fly" placeholder="e.g. Pheasant Tail #14" />
      </label>
      <label class="cl-label">Water temp °F (optional)
        <input type="number" id="cl-water" min="32" max="90" step="1" />
      </label>
      <label class="cl-label">Notes (optional)
        <textarea id="cl-notes" rows="2" placeholder="What worked, what didn't"></textarea>
      </label>
      <div class="cl-actions">
        <button type="button" id="cl-cancel" class="cl-btn cl-btn-ghost">Cancel</button>
        <button type="button" id="cl-submit" class="cl-btn cl-btn-primary" disabled>Submit</button>
      </div>
      <p id="cl-error" class="error small hidden"></p>
    `;
    let chosenRating = null;
    formEl.querySelectorAll(".cl-rate").forEach((b) => {
      b.addEventListener("click", () => {
        chosenRating = parseInt(b.dataset.v, 10);
        formEl.querySelectorAll(".cl-rate").forEach(x => x.classList.toggle("cl-rate-on", x === b));
        document.getElementById("cl-submit").disabled = false;
      });
    });
    document.getElementById("cl-cancel").addEventListener("click", () => closeCatchLogForm(formEl, openBtn));
    document.getElementById("cl-submit").addEventListener("click", async () => {
      if (chosenRating == null) return;
      const fished = document.getElementById("cl-fished-at").value;
      const fishedIso = fished ? new Date(fished).toISOString() : new Date().toISOString();
      const payload = {
        reach_id: reachId,
        fished_at: fishedIso,
        success: chosenRating,
        method: document.getElementById("cl-method").value || null,
        fly_used: document.getElementById("cl-fly").value || null,
        water_temp_f: parseFloat(document.getElementById("cl-water").value) || null,
        notes: document.getElementById("cl-notes").value || null,
      };
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
      const emoji = ["😶", "🙂", "😄", "🤩"];
      list.innerHTML = rows.map((r) => {
        const when = new Date(r.fished_at).toLocaleDateString(undefined, { weekday: "short", month: "numeric", day: "numeric" });
        const method = r.method ? `<span class="cl-method">${r.method}</span>` : "";
        const fly = r.fly_used ? ` · ${escapeHtml(r.fly_used)}` : "";
        const wt = r.water_temp_f ? ` · ${Math.round(r.water_temp_f)}°F` : "";
        const notes = r.notes ? `<div class="cl-notes-line">${escapeHtml(r.notes)}</div>` : "";
        const predicted = r.predicted_score != null ? ` <span class="cl-vs muted small">(model said ${Math.round(r.predicted_score * 100)})</span>` : "";
        return `
          <li class="cl-row">
            <div class="cl-row-head">
              <span class="cl-emoji">${emoji[r.success] || ""}</span>
              <span class="cl-when">${when}</span>
              ${method}${predicted}
            </div>
            <div class="cl-row-sub">${escapeHtml(r.fly_used || "")}${fly && wt ? "" : ""}${wt}</div>
            ${notes}
          </li>
        `;
      }).join("");
    } catch (err) {
      list.innerHTML = `<li class="error small">${err.message}</li>`;
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
      <div class="explainer-card" onclick="event.stopPropagation()">
        <button class="explainer-close" aria-label="Close">×</button>
        ${innerHtml}
      </div>
    `;
    overlay.addEventListener("click", () => overlay.remove());
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

    const top = sb.top_species;
    const topLine = renderTopSpeciesDetails(top);
    return `
      <div class="sb-totals">
        <div class="sb-total"><span class="sb-total-label">Nymph</span><span class="sb-total-num">${Math.round(nymph * 100)}</span></div>
        <div class="sb-total"><span class="sb-total-label">Dry</span><span class="sb-total-num">${Math.round(dry * 100)}</span></div>
        ${model && model.aggression != null ? `<div class="sb-total"><span class="sb-total-label">Aggression</span><span class="sb-total-num">${Math.round(model.aggression * 100)}</span></div>` : ""}
        ${confidence && confidence.score != null ? `<div class="sb-total"><span class="sb-total-label">Confidence</span><span class="sb-total-num">${Math.round(confidence.score * 100)}</span></div>` : ""}
        <div class="sb-total"><span class="sb-total-label">Headline</span><span class="sb-total-num">${Math.round(headline * 100)}</span></div>
      </div>
      ${model ? `<p class="sb-model-note">${renderHeadlineModelNote(model)}</p>` : ""}
      <h4 class="sb-section-h">Component multipliers (1.0 = full credit)</h4>
      ${bar("Water temp", sb.temperature, "water_temp_zone")}
      ${renderThermalProfileNote(sb)}
      ${bar("Flow percentile", sb.flow_percentile, "flow_percentile")}
      ${bar("Flow trend", sb.flow_trend, "flow_trend")}
      ${multiplier("Pressure trend", sb.pressure_factor, "barometric_pressure")}
      ${multiplier("Sun angle (dry only)", sb.sun_factor, "sun_angle")}
      ${renderFlowTauNote(sb)}
      ${model && model.aggression_factors ? renderAggressionFactors(model.aggression_factors) : ""}
      ${confidence ? renderConfidenceNote(confidence) : ""}
      ${topLine}
      <p class="sb-note">Nymph and dry are physical component scores. Headline is calibrated from those components so nymph-only plateaus do not read like boiling-rises days. Click any ${helpButton("score_overview").replace(/<\/?button[^>]*>/g, '?')} to see how a component is computed.</p>
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
    if (source === "dry") {
      return "Headline is led by surface activity because the hatch model is stronger than the nymph model.";
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
  }

  function monthLabel(doy) {
    const d = new Date(new Date().getFullYear(), 0, doy);
    return d.toLocaleDateString(undefined, { month: "short" });
  }

  function calendarColor(activity) {
    if (activity == null || activity < 0.05) return "#f3f5f7";
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
      for (let m = 1; m <= 12; m++) {
        const doy = Math.round((m - 1) * 30.4) + 1;
        monthMarkers.push({ doy, x: padL + (doy / 365) * inner, label: monthLabel(doy) });
      }
      const todayX = padL + (todayDoy / 365) * inner;
      const rowsHtml = data.species.map((sp) => {
        const cells = sp.samples.map((s, i) => {
          const x = padL + (s.doy / 365) * inner;
          const wCell = inner / samples + 0.5;
          return `<rect x="${x}" y="0" width="${wCell}" height="${h - 4}" fill="${calendarColor(s.activity)}" />`;
        }).join("");
        return `<div class="cal-row" data-sp="${sp.species_id}">
          <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" class="cal-svg">
            <text x="0" y="${h-5}" font-size="10" fill="#374151">${sp.common_name || sp.species_id}</text>
            ${cells}
            <line x1="${todayX}" y1="0" x2="${todayX}" y2="${h-3}" stroke="#264653" stroke-width="1" opacity="0.7" />
          </svg>
        </div>`;
      }).join("");
      const monthRow = `<svg viewBox="0 0 ${w} 14" preserveAspectRatio="none" class="cal-svg cal-month-row">
        ${monthMarkers.map(m => `<text x="${m.x}" y="11" font-size="9" fill="#6b7280">${m.label}</text>`).join("")}
      </svg>`;
      const reachNote = reachId
        ? `<p class="muted">For ${reachId} · shifted ${data.shift_days >= 0 ? "+" : ""}${data.shift_days.toFixed(1)}d by recent thermal anomaly</p>`
        : `<p class="muted">Driftless seasonal averages · click a reach for shift adjustment</p>`;
      body.innerHTML = `
        ${reachNote}
        ${monthRow}
        <div class="cal-rows">${rowsHtml}</div>
        <p class="muted spark-legend">vertical line = today. Shaded band = activity probability.</p>
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
    const peakLabel = `${monthLabel(Math.round(((sp.peak_month - 1) * 30.4) + sp.peak_day))} ${sp.peak_day}`;
    const window = sp.first_present && sp.last_present
      ? `${sp.first_present.slice(5)} → ${sp.last_present.slice(5)}`
      : "—";
    const body = document.getElementById("calendar-body");
    body.insertAdjacentHTML("beforeend", `
      <div class="cal-detail-overlay" onclick="this.remove()">
        <div class="cal-detail-card" onclick="event.stopPropagation()">
          <button class="cal-close" onclick="this.parentElement.parentElement.remove()">×</button>
          <h3>${sp.common_name}</h3>
          <p class="muted">${sp.scientific_name || ""}</p>
          <p><strong>Peak:</strong> ${peakLabel}</p>
          <p><strong>Window:</strong> ${window}</p>
          <h4>Patterns</h4>
          <div class="flies">${flies}</div>
        </div>
      </div>
    `);
  }

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
  attachScrubber();
  // Residuals first so the very first paint of /reaches uses the calibrated
  // score; the call is cheap (one row per reach with >=3 reports).
  loadResiduals().then(() => {
    loadPlanningSummary();
    loadReaches();
    loadBestWindows();
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
    loadScrubGrid();
  }, REFRESH_MS);
})();
