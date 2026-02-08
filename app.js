/* ================================================================
   app.js  –  Urban Heat Island Map (Waterloo Region)
   ================================================================
   Sections:
     1. Constants & Configuration
     2. DOM References
     3. State
     4. Map Initialisation
     5. Geometry Helpers
     6. Color Scale
     7. Filtering
     8. Layer Rendering
     9. Detail Panel
    10. Filter Control Bindings
    11. View Toggle & Draw Tool
    12. Data Loading
    13. Chat Module
   ================================================================ */

// ── 1. Constants & Configuration ──────────────────────────
const MAP_CENTER = [43.42, -80.38];
const MAP_ZOOM   = 10;
const CHAT_API   = 'http://localhost:8001';

const HEAT_OPTS = {
  radius: 35, blur: 28, maxZoom: 13, minOpacity: 0.35, max: 1,
  gradient: {
    0.0: '#2166ac', 0.2: '#4393c3', 0.4: '#92c5de', 0.5: '#d1e5f0',
    0.6: '#f7f7f7', 0.7: '#fddbc7', 0.8: '#f4a582', 0.9: '#e08870', 1.0: '#d6604d'
  }
};

const COLOR_STOPS = [
  [0,    [33,102,172]],   // #2166ac  — coolest
  [12.5, [67,147,195]],   // #4393c3
  [25,   [146,197,222]],  // #92c5de
  [37.5, [209,229,240]],  // #d1e5f0
  [50,   [247,247,247]],  // #f7f7f7  — neutral
  [62.5, [253,219,199]],  // #fddbc7
  [75,   [244,165,130]],  // #f4a582
  [87.5, [214,96,77]],    // #d6604d
  [100,  [178,24,43]]     // #b2182b  — hottest
];

const GAMMA = 0.30; // power curve — lower = red grows faster in denser areas
const AUTO_GRID_ZOOM = 13; // zoom level at which we auto-switch from heatmap to grid

const CSV_HEADERS = [
  'OBJECTID','Municipality','Settlement','FootprintSqft','Storeys',
  'TotalSqft','BuildingType','size_eligible','storey_category','svr_proxy'
];

// ── 2. DOM References ─────────────────────────────────────
const $ = id => document.getElementById(id);

const dom = {
  covMin:          $('covMin'),
  covMinVal:       $('covMinVal'),
  minBuildings:    $('minBuildings'),
  minBuildingsVal: $('minBuildingsVal'),
  neighborhood:    $('neighborhood'),
  sizeEligibleOnly:$('sizeEligibleOnly'),
  buildingType:    $('buildingType'),
  storeyTier:      $('storeyTier'),
  showBuildings:   $('showBuildings'),
  searchInput:     $('searchInput'),
  searchBtn:       $('searchBtn'),
  viewHeatBtn:     $('viewHeatBtn'),
  viewGridBtn:     $('viewGridBtn'),
  viewOffBtn:      $('viewOffBtn'),
  drawAreaBtn:     $('drawAreaBtn'),
  exportBtn:       $('exportBtn'),
  resetBtn:        $('resetBtn'),
  detailPanel:     $('detailPanel'),
  detailContent:   $('detailContent'),
  closeDetail:     $('closeDetail'),
  chatToggle:      $('chatToggle'),
  chatPanel:       $('chatPanel'),
  chatMessages:    $('chatMessages'),
  chatInput:       $('chatInput'),
  chatSendBtn:     $('chatSendBtn'),
  chatNewBtn:      $('chatNewBtn'),
  chatTyping:      $('chatTyping'),
};

// ── 3. State ──────────────────────────────────────────────
let rawFeatures       = [];
let buildingsData     = [];
let neighborhoodStats = [];
let gridLayer         = null;
let heatLayer         = null;
let buildingsLayer    = null;
let viewMode          = 'heat';
let drawRect          = null;
let drawMode          = false;
let drawStart         = null;
let maxCoverage       = 60;
let userLockedView    = false;
let searchMarker      = null;
let chatThreadId      = null;

// ── 4. Map Initialisation ─────────────────────────────────
const map = L.map('map').setView(MAP_CENTER, MAP_ZOOM);
L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  attribution: '&copy; OpenStreetMap'
}).addTo(map);

// ── 5. Geometry Helpers ───────────────────────────────────
function getCentroid(feature) {
  const ring = feature.geometry.coordinates[0];
  let sx = 0, sy = 0;
  const n = ring.length - 1;
  for (let i = 0; i < n; i++) { sx += ring[i][0]; sy += ring[i][1]; }
  return [sy / n, sx / n];
}

function pointInPolygon(lat, lng, feature) {
  const ring = feature.geometry.coordinates[0];
  const x = lng, y = lat;
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const xi = ring[i][0], yi = ring[i][1];
    const xj = ring[j][0], yj = ring[j][1];
    if (((yi > y) !== (yj > y)) && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) {
      inside = !inside;
    }
  }
  return inside;
}

// ── 6. Color Scale ────────────────────────────────────────
function getColor(pct) {
  const ratio = Math.min(1, Math.max(0, pct / maxCoverage));
  const normalized = Math.pow(ratio, GAMMA) * 100;

  for (let i = 0; i < COLOR_STOPS.length - 1; i++) {
    if (normalized <= COLOR_STOPS[i + 1][0]) {
      const t  = (normalized - COLOR_STOPS[i][0]) / (COLOR_STOPS[i + 1][0] - COLOR_STOPS[i][0]);
      const c0 = COLOR_STOPS[i][1];
      const c1 = COLOR_STOPS[i + 1][1];
      const r  = Math.round(c0[0] + t * (c1[0] - c0[0]));
      const g  = Math.round(c0[1] + t * (c1[1] - c0[1]));
      const b  = Math.round(c0[2] + t * (c1[2] - c0[2]));
      return `rgb(${r},${g},${b})`;
    }
  }
  return 'rgb(178,24,43)';
}

function gridStyle(feature) {
  const pct = feature.properties.coverage_pct || 0;
  return { fillColor: getColor(pct), fillOpacity: 0.65, weight: 0.5, color: '#fff' };
}

// ── 7. Filtering ──────────────────────────────────────────
function getFilteredFeatures() {
  const covMin     = +dom.covMin.value;
  const minB       = +dom.minBuildings.value;
  const settlement = dom.neighborhood.value;
  return rawFeatures.filter(f => {
    const p = f.properties;
    if (p.coverage_pct < covMin || p.building_count < minB) return false;
    if (settlement && (p.settlement || p.Settlement) !== settlement) return false;
    return true;
  });
}

function getFilteredBuildings() {
  const settlement = dom.neighborhood.value;
  const sizeOnly   = dom.sizeEligibleOnly.checked;
  const btype      = dom.buildingType.value;
  const storey     = dom.storeyTier.value;
  return buildingsData.filter(b => {
    const p = b.properties || {};
    if (settlement && p.Settlement !== settlement) return false;
    if (sizeOnly && !p.size_eligible) return false;
    if (btype && p.BuildingType !== btype) return false;
    if (storey && p.storey_category !== storey) return false;
    return true;
  });
}

// ── 8. Layer Rendering ────────────────────────────────────
function removeLayer(layer) {
  if (layer) map.removeLayer(layer);
  return null;
}

function renderHeatmap(features) {
  gridLayer = removeLayer(gridLayer);
  const points = features.map(f => {
    const [lat, lng] = getCentroid(f);
    const intensity = Math.min(1, (f.properties.coverage_pct || 0) / maxCoverage);
    return [lat, lng, intensity];
  });
  heatLayer = removeLayer(heatLayer);
  heatLayer = L.heatLayer(points, HEAT_OPTS).addTo(map);
}

function renderGrid(features) {
  heatLayer = removeLayer(heatLayer);
  gridLayer = removeLayer(gridLayer);
  gridLayer = L.geoJSON({ type: 'FeatureCollection', features }, {
    style: gridStyle,
    onEachFeature(feature, layer) {
      const p = feature.properties;
      layer.bindTooltip(
        `<b>${p.coverage_pct.toFixed(1)}%</b> coverage<br>${p.building_count} buildings`,
        { sticky: true, className: 'grid-tooltip', direction: 'top', offset: [0, -8] }
      );
      layer.on('click', () => showCellDetail(feature));
    }
  }).addTo(map);
}

function applyBuildingsLayer() {
  buildingsLayer = removeLayer(buildingsLayer);
  if (!dom.showBuildings.checked || !buildingsData.length) return;

  const filtered = getFilteredBuildings();
  buildingsLayer = L.geoJSON({ type: 'FeatureCollection', features: filtered }, {
    pointToLayer(f, latlng) {
      return L.circleMarker(latlng, {
        radius: 3,
        fillColor: f.properties.size_eligible ? '#22a722' : '#c44',
        color: '#fff', weight: 0.5, fillOpacity: 0.7
      });
    },
    onEachFeature(feature, layer) {
      const p = feature.properties;
      layer.bindTooltip(
        `<b>${p.TotalSqft || p.FootprintSqft} sqft</b> · ${p.Storeys} storeys · ` +
        `${p.size_eligible ? 'Eligible' : 'Over cap'} · SVR ${(p.svr_proxy || 0).toFixed(2)}`,
        { sticky: true }
      );
    }
  }).addTo(map);
}

function applyFilters() {
  const filtered = getFilteredFeatures();
  if (viewMode === 'off') {
    heatLayer = removeLayer(heatLayer);
    gridLayer = removeLayer(gridLayer);
  } else if (viewMode === 'heat') {
    renderHeatmap(filtered);
  } else {
    renderGrid(filtered);
  }
  applyBuildingsLayer();
}

// ── 9. Detail Panel ───────────────────────────────────────
function showDetailPanel(title, html) {
  dom.detailPanel.querySelector('h4').textContent = title;
  dom.detailContent.innerHTML = html;
  dom.detailPanel.classList.add('visible');
}

function hideDetailPanel() {
  dom.detailPanel.classList.remove('visible');
}

function showCellDetail(feature) {
  const p = feature.properties;
  showDetailPanel('Cell / Area Stats',
    `<div class="stat">Coverage: <strong>${p.coverage_pct.toFixed(1)}%</strong></div>` +
    `<div class="stat">Buildings: <strong>${p.building_count}</strong></div>`
  );
}

function showAreaDetail(bounds) {
  const inBounds = getFilteredFeatures().filter(f => {
    const c = f.geometry.coordinates[0];
    const lngs = c.map(p => p[0]), lats = c.map(p => p[1]);
    const cx = (Math.min(...lngs) + Math.max(...lngs)) / 2;
    const cy = (Math.min(...lats) + Math.max(...lats)) / 2;
    return cx >= bounds.getWest() && cx <= bounds.getEast() &&
           cy >= bounds.getSouth() && cy <= bounds.getNorth();
  });
  const tot = inBounds.length;
  const avg = tot ? inBounds.reduce((s, f) => s + f.properties.coverage_pct, 0) / tot : 0;
  const buildings = inBounds.reduce((s, f) => s + f.properties.building_count, 0);
  showDetailPanel('Cell / Area Stats',
    `<div class="area-stats">` +
    `<div class="stat">Cells: <strong>${tot}</strong></div>` +
    `<div class="stat">Avg coverage: <strong>${avg.toFixed(1)}%</strong></div>` +
    `<div class="stat">Total buildings: <strong>${buildings}</strong></div></div>`
  );
}

function showNeighborhoodDetail(ns) {
  showDetailPanel('Neighborhood Stats',
    `<div class="area-stats">` +
    `<div class="stat">Buildings: <strong>${ns.building_count}</strong></div>` +
    `<div class="stat">Size-eligible: <strong>${ns.size_eligible_count}</strong></div>` +
    `<div class="stat">Avg coverage: <strong>${ns.avg_coverage.toFixed(1)}%</strong></div>` +
    `<div class="stat">Priority score: <strong>${ns.priority_score.toFixed(2)}</strong></div></div>`
  );
}

// ── 10. Filter Control Bindings ───────────────────────────
dom.covMin.oninput = function () {
  dom.covMinVal.textContent = this.value;
  applyFilters();
};

dom.minBuildings.oninput = function () {
  dom.minBuildingsVal.textContent = this.value;
  applyFilters();
};

dom.neighborhood.onchange = function () {
  applyFilters();
  const s = this.value;
  if (s && neighborhoodStats.length) {
    const ns = neighborhoodStats.find(n => n.Settlement === s);
    if (ns) showNeighborhoodDetail(ns);
  } else {
    dom.detailPanel.querySelector('h4').textContent = 'Cell / Area Stats';
    hideDetailPanel();
  }
};

dom.sizeEligibleOnly.onchange = applyFilters;
dom.buildingType.onchange     = applyFilters;
dom.storeyTier.onchange       = applyFilters;
dom.showBuildings.onchange    = applyFilters;

dom.exportBtn.onclick = function () {
  const filtered = getFilteredBuildings();
  if (!filtered.length) {
    showToast('No buildings to export — enable "Show building points" and apply filters.', 'warning');
    return;
  }
  const rows = filtered.map(f => {
    const p = f.properties || {};
    return CSV_HEADERS.map(h => (p[h] != null ? p[h] : '')).join(',');
  });
  const csv  = CSV_HEADERS.join(',') + '\n' + rows.join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = 'filtered_buildings.csv';
  a.click();
  URL.revokeObjectURL(a.href);
};

// ── Toast Notification Helper ─────────────────────────────
function showToast(message, type = '', duration = 3000) {
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = message;
  document.body.appendChild(el);
  requestAnimationFrame(() => el.classList.add('visible'));
  setTimeout(() => {
    el.classList.remove('visible');
    setTimeout(() => el.remove(), 300);
  }, duration);
}

// Waterloo Region bounding box (SW → NE)
const SEARCH_VIEWBOX = '-80.75,43.25,-80.20,43.60';

async function doSearch() {
  const q = dom.searchInput.value.trim();
  if (!q) return;
  try {
    const params = new URLSearchParams({
      format: 'json',
      q: q + ', Waterloo Ontario',
      limit: '1',
      viewbox: SEARCH_VIEWBOX,
      bounded: '1',
    });
    const data = await fetch(`https://nominatim.openstreetmap.org/search?${params}`).then(r => r.json());
    if (!data.length) {
      // Retry without bounding box in case the query is just outside the region
      params.delete('bounded');
      const fallback = await fetch(`https://nominatim.openstreetmap.org/search?${params}`).then(r => r.json());
      if (!fallback.length) { showToast('No results found — try a different address or place name.', 'warning'); return; }
      data.push(fallback[0]);
    }
    // Clear previous search marker
    if (searchMarker) { map.removeLayer(searchMarker); searchMarker = null; }
    const lat = parseFloat(data[0].lat);
    const lon = parseFloat(data[0].lon);
    map.flyTo([lat, lon], 14);
    searchMarker = L.marker([lat, lon]).addTo(map).bindPopup(data[0].display_name).openPopup();
  } catch { showToast('Search failed — please try again.', 'error'); }
}

dom.searchBtn.onclick = doSearch;
dom.searchInput.addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); doSearch(); }
});

// ── 11. View Toggle & Draw Tool ───────────────────────────
function setActiveViewBtn(activeBtn) {
  [dom.viewHeatBtn, dom.viewGridBtn, dom.viewOffBtn].forEach(btn => {
    btn.classList.remove('active');
    btn.classList.add('secondary');
  });
  activeBtn.classList.add('active');
  activeBtn.classList.remove('secondary');
}

dom.viewHeatBtn.onclick = () => {
  userLockedView = false;  // re-enable auto-switch (heatmap is the default auto mode)
  viewMode = 'heat';
  setActiveViewBtn(dom.viewHeatBtn);
  applyFilters();
};

dom.viewGridBtn.onclick = () => {
  userLockedView = true;
  viewMode = 'grid';
  setActiveViewBtn(dom.viewGridBtn);
  applyFilters();
};

dom.viewOffBtn.onclick = () => {
  userLockedView = true;
  viewMode = 'off';
  setActiveViewBtn(dom.viewOffBtn);
  applyFilters();
};

// ── Auto-switch view based on zoom level ──
map.on('zoomend', () => {
  if (userLockedView) return;
  const z = map.getZoom();
  if (z >= AUTO_GRID_ZOOM && viewMode !== 'grid') {
    viewMode = 'grid';
    setActiveViewBtn(dom.viewGridBtn);
    applyFilters();
  } else if (z < AUTO_GRID_ZOOM && viewMode !== 'heat') {
    viewMode = 'heat';
    setActiveViewBtn(dom.viewHeatBtn);
    applyFilters();
  }
});

// ── Draw rectangle tool ──
function onDrawStart(e) {
  drawStart = e.latlng;
  drawRect  = L.rectangle([drawStart, drawStart], { color: '#2166ac', weight: 2, fillOpacity: 0.1 }).addTo(map);
  map.on('mousemove', onDrawMove);
  map.on('mouseup', onDrawEnd);
}

function onDrawMove(e) {
  if (drawRect && drawStart) drawRect.setBounds([drawStart, e.latlng]);
}

function onDrawEnd() {
  map.off('mousedown', onDrawStart);
  map.off('mousemove', onDrawMove);
  map.off('mouseup', onDrawEnd);
  if (drawRect && drawStart) {
    showAreaDetail(drawRect.getBounds());
    dom.drawAreaBtn.classList.remove('active');
    drawMode = false;
    map.dragging.enable();
    setTimeout(() => { drawRect = removeLayer(drawRect); }, 3000);
  }
  drawStart = null;
}

dom.drawAreaBtn.onclick = function () {
  drawMode = !drawMode;
  this.classList.toggle('active', drawMode);
  if (drawMode) {
    map.dragging.disable();
    map.on('mousedown', onDrawStart);
  } else {
    map.dragging.enable();
    map.off('mousedown', onDrawStart);
    map.off('mousemove', onDrawMove);
    map.off('mouseup', onDrawEnd);
    drawRect = removeLayer(drawRect);
  }
};

dom.closeDetail.onclick = hideDetailPanel;

dom.resetBtn.onclick = () => {
  dom.covMin.value           = 0.1;
  dom.covMinVal.textContent  = '0.1';
  dom.minBuildings.value     = 0;
  dom.minBuildingsVal.textContent = '0';
  dom.neighborhood.value     = '';
  dom.sizeEligibleOnly.checked = false;
  dom.buildingType.value     = '';
  dom.storeyTier.value       = '';
  userLockedView = false;
  viewMode = 'heat';
  setActiveViewBtn(dom.viewHeatBtn);
  hideDetailPanel();
  applyFilters();
  map.flyTo(MAP_CENTER, MAP_ZOOM);
};

map.on('click', e => {
  // Dismiss search marker on any map click
  if (searchMarker) { map.removeLayer(searchMarker); searchMarker = null; }

  if (viewMode !== 'heat' || drawMode) return;
  const cell = getFilteredFeatures().find(f => pointInPolygon(e.latlng.lat, e.latlng.lng, f));
  if (cell) showCellDetail(cell);
});

// ── 12. Data Loading ──────────────────────────────────────
Promise.all([
  fetch('uhi_grid.geojson').then(r => r.json()),
  fetch('neighborhood_stats.json').then(r => r.json()).catch(() => []),
  fetch('buildings_enriched_sample.json').then(r => r.json()).catch(() => ({ features: [] }))
]).then(([geojson, stats, buildingsGeo]) => {
  rawFeatures       = geojson.features;
  neighborhoodStats = stats;
  buildingsData     = buildingsGeo.features || [];

  maxCoverage = Math.ceil(Math.max(...rawFeatures.map(f => f.properties.coverage_pct))) || 60;
  dom.covMin.max = maxCoverage;

  const maxBuildings = Math.min(100, Math.ceil(Math.max(...rawFeatures.map(f => f.properties.building_count || 0))) || 50);
  dom.minBuildings.max = maxBuildings;

  neighborhoodStats.forEach(n => {
    const opt = document.createElement('option');
    opt.value = n.Settlement;
    opt.textContent = n.Settlement;
    dom.neighborhood.appendChild(opt);
  });

  applyFilters();

  // Dismiss loading overlay
  const overlay = document.getElementById('loadingOverlay');
  if (overlay) { overlay.classList.add('hidden'); setTimeout(() => overlay.remove(), 500); }
}).catch(err => {
  document.body.insertAdjacentHTML('beforeend',
    '<p style="position:fixed;top:10px;left:50%;transform:translateX(-50%);background:#c00;color:#fff;padding:8px 16px;border-radius:8px;z-index:9999;">' +
    'Run compute_uhi.py, build_building_scores.py, build_neighborhood_stats.py, enrich_uhi_grid.py, then serve (python -m http.server 8000)</p>'
  );
  console.error(err);
});

// ── 13. Chat Module ───────────────────────────────────────
dom.chatToggle.onclick = () => {
  dom.chatToggle.classList.add('interacted');
  const isOpen = dom.chatPanel.classList.contains('visible');
  if (isOpen) {
    dom.chatPanel.classList.add('closing');
    dom.chatToggle.classList.remove('open');
    dom.chatPanel.addEventListener('animationend', function handler() {
      dom.chatPanel.removeEventListener('animationend', handler);
      dom.chatPanel.classList.remove('visible', 'closing');
    });
  } else {
    dom.chatPanel.classList.remove('closing');
    dom.chatPanel.classList.add('visible');
    dom.chatToggle.classList.add('open');
    dom.chatInput.focus();
  }
};

// ── Collapsible filter panel ──────────────────────────────
document.querySelector('.panel h3').onclick = () => {
  document.querySelector('.panel').classList.toggle('collapsed');
};

dom.chatNewBtn.onclick = () => {
  chatThreadId = null;
  dom.chatMessages.innerHTML = '<div class="chat-msg system">New conversation started. Ask me anything!</div>';
};

function addChatMsg(role, text, actions) {
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;

  if (role === 'assistant') {
    let html = marked.parse(text || '', { breaks: true, gfm: true });
    if (actions && actions.length) {
      html += '<div style="margin-top:6px">';
      actions.forEach(a => {
        const label  = a.type.replace(/_/g, ' ');
        const detail = a.settlement || a.building_type || '';
        html += `<span class="action-tag">${label}${detail ? ': ' + detail : ''}</span>`;
      });
      html += '</div>';
    }
    div.innerHTML = html;
  } else {
    div.textContent = text;
  }

  dom.chatMessages.appendChild(div);
  dom.chatMessages.scrollTop = dom.chatMessages.scrollHeight;
}

function getMapState() {
  return {
    settlement:         dom.neighborhood.value,
    size_eligible_only: dom.sizeEligibleOnly.checked,
    building_type:      dom.buildingType.value,
    storey_tier:        dom.storeyTier.value,
    min_coverage:       parseFloat(dom.covMin.value),
    show_buildings:     dom.showBuildings.checked,
  };
}

function executeActions(actions) {
  if (!actions || !actions.length) return;
  actions.forEach(action => {
    switch (action.type) {
      case 'highlight_settlement': {
        const opt = Array.from(dom.neighborhood.options).find(o => o.value === action.settlement);
        if (opt) {
          dom.neighborhood.value = action.settlement;
          dom.neighborhood.dispatchEvent(new Event('change'));
        }
        break;
      }
      case 'zoom_to_settlement': {
        const ns = neighborhoodStats.find(n => n.Settlement === action.settlement);
        if (ns && ns.centroid_lat && ns.centroid_lng) {
          map.flyTo([ns.centroid_lat, ns.centroid_lng], 13);
        } else {
          dom.neighborhood.value = action.settlement;
          dom.neighborhood.dispatchEvent(new Event('change'));
        }
        break;
      }
      case 'apply_filters': {
        if (action.size_eligible_only != null) dom.sizeEligibleOnly.checked = action.size_eligible_only;
        if (action.building_type != null)      dom.buildingType.value       = action.building_type;
        if (action.storey_tier != null)        dom.storeyTier.value         = action.storey_tier;
        if (action.min_coverage != null) {
          dom.covMin.value          = action.min_coverage;
          dom.covMinVal.textContent = action.min_coverage;
        }
        applyFilters();
        break;
      }
      case 'show_building_points': {
        dom.showBuildings.checked = !!action.visible;
        applyFilters();
        break;
      }
    }
  });
}

async function sendChatMessage() {
  const text = dom.chatInput.value.trim();
  if (!text) return;

  addChatMsg('user', text);
  dom.chatInput.value   = '';
  dom.chatSendBtn.disabled = true;
  dom.chatTyping.classList.add('visible');

  try {
    const resp = await fetch(`${CHAT_API}/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        message:   text,
        thread_id: chatThreadId,
        map_state: getMapState(),
      }),
    });

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: 'Unknown error' }));
      throw new Error(err.detail || `HTTP ${resp.status}`);
    }

    const data = await resp.json();
    chatThreadId = data.thread_id;
    addChatMsg('assistant', data.message, data.actions);
    executeActions(data.actions);
  } catch (err) {
    const msg = err.message || '';
    // Corrupted thread — auto-reset and tell the user
    if (msg.includes('tool_call') || msg.includes('tool_calls')) {
      chatThreadId = null;
      addChatMsg('system', 'The conversation hit a glitch and has been reset. Please try your message again.');
    } else {
      addChatMsg('system', `Error: ${msg}. Is the backend running? (uvicorn chat_backend:app --port 8001)`);
    }
    console.error('Chat error:', err);
  } finally {
    dom.chatSendBtn.disabled = false;
    dom.chatTyping.classList.remove('visible');
  }
}

dom.chatSendBtn.onclick = sendChatMessage;
dom.chatInput.addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChatMessage(); }
});
