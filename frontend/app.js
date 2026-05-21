/* ═══════════════════════════════════════════════════════════════
   AI Intrusion & Virtual Tripwire System — dashboard logic
   ═══════════════════════════════════════════════════════════════ */
"use strict";

const $  = (id) => document.getElementById(id);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

async function api(url, opts) {
  const r = await fetch(url, opts);
  let data = {};
  try { data = await r.json(); } catch (_) {}
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}

/* shared state */
let cameras = [];          // [{id,name,status,running,...}]
let pipelines = {};        // id -> {status,fps,...}
let currentView = "live";
let activeZones = [];      // cached zones for the selected camera
let editingZoneId = null;  // active zone ID being updated

/* ───────────────── NAVIGATION ───────────────── */
$$(".nav-item").forEach((btn) =>
  btn.addEventListener("click", () => showView(btn.dataset.view))
);

function showView(view) {
  currentView = view;
  $$(".nav-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === view)
  );
  $$(".view").forEach((v) =>
    v.classList.toggle("active", v.id === "view-" + view)
  );
  if (view === "live")    renderLive();
  if (view === "cameras") renderCameras();
  if (view === "rules") {
    renderRules();
    setTimeout(syncCanvas, 100);
  }
  if (view === "alerts")  loadAlerts();
}

/* ───────────────── SYSTEM / DEVICE ───────────────── */
async function refreshSystem() {
  try {
    const s = await api("/api/system");
    const d = s.device;
    $("device-name").textContent = `${d.name}`;
    $("device-dot").className = "dot " + d.kind;
    pipelines = {};
    (s.pipelines || []).forEach((p) => (pipelines[p.camera_id] = p));
  } catch (_) {
    $("device-name").textContent = "unavailable";
  }
}

async function refreshCameras() {
  try { cameras = await api("/api/cameras"); }
  catch (_) { cameras = []; }
}

/* ───────────────── LIVE STREAMING ───────────────── */
async function renderLive() {
  await refreshSystem();
  await refreshCameras();
  const grid = $("live-grid");
  const running = cameras.filter((c) => c.running);

  $("live-empty").classList.toggle("show", running.length === 0);

  // Only rebuild cards when the set of cameras changes (keeps streams alive).
  const ids = running.map((c) => c.id).join(",");
  if (grid.dataset.ids !== ids) {
    grid.dataset.ids = ids;
    grid.innerHTML = running
      .map(
        (c) => `
      <div class="cam-card" data-id="${c.id}">
        <div class="feed-wrap">
          <span class="live-tag">● LIVE</span>
          <span class="fps-tag" id="fps-${c.id}">-- fps</span>
          <img src="/api/cameras/${c.id}/stream?t=${Date.now()}" alt="${c.name}" />
        </div>
        <div class="cam-bar">
          <b>${c.name}</b>
          <span class="status-online" id="st-${c.id}">online</span>
        </div>
      </div>`
      )
      .join("");
  }
  // Live-update fps / status without touching the <img>.
  running.forEach((c) => {
    const p = pipelines[c.id] || {};
    const fps = $("fps-" + c.id);
    const st = $("st-" + c.id);
    if (fps) fps.textContent = (p.fps ?? 0).toFixed(1) + " fps";
    if (st) {
      st.textContent = p.status || "online";
      st.className = "status-" + (p.status || "online");
    }
  });
}

$("live-refresh").addEventListener("click", () => {
  $("live-grid").dataset.ids = "";          // force rebuild
  renderLive();
});

/* ───────────────── ADD CAMERAS ───────────────── */

/* ── Mode toggle: IP+Credentials vs Full RTSP URL ── */
let _camMode = "ip";  // "ip" | "full"

$("cam-mode-toggle").addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn) return;
  _camMode = btn.dataset.mode;
  $$("#cam-mode-toggle .seg-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");

  // Show/hide the right set of fields
  const ipFields  = $("ip-mode-fields").querySelectorAll("label");
  const fullField = $("full-url-field");

  if (_camMode === "ip") {
    ipFields.forEach(l => { l.style.display = ""; });
    fullField.style.display = "none";
    // Clear full URL input to avoid confusion
    $("cam-url").value = "";
  } else {
    ipFields.forEach(l => { l.style.display = "none"; });
    fullField.style.display = "";
    // Clear IP fields
    $("cam-host").value = "";
    $("cam-port").value = "";
    $("cam-user").value = "";
    $("cam-pass").value = "";
  }
});

async function renderCameras() {
  await refreshSystem();
  await refreshCameras();
  $("camera-rows").innerHTML = cameras
    .map((c) => {
      const p      = pipelines[c.id] || {};
      const status = p.status || c.status || "offline";
      // Show host/IP stored in the url field (no credentials exposed)
      const hostDisplay = c.url || "—";
      return `<tr>
        <td>${c.id}</td>
        <td>${c.name}</td>
        <td style="font-family:monospace;font-size:12px">${hostDisplay}</td>
        <td class="status-${status}">${status}</td>
        <td>${p.fps != null ? p.fps.toFixed(1) : "—"}</td>
        <td>
          <button class="btn ghost sm" data-act="test"  data-id="${c.id}">Test</button>
          ${c.running
            ? `<button class="btn ghost sm" data-act="stop" data-id="${c.id}">Stop</button>`
            : `<button class="btn primary sm" data-act="start" data-id="${c.id}">Start</button>`}
          <button class="btn danger sm" data-act="del" data-id="${c.id}">Delete</button>
        </td>
      </tr>`;
    })
    .join("") ||
    `<tr><td colspan="6" style="color:var(--muted)">No cameras configured.</td></tr>`;
}

$("camera-rows").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const id = btn.dataset.id;
  btn.disabled = true;
  try {
    if (btn.dataset.act === "test") {
      const r = await api(`/api/cameras/${id}/test`, { method: "POST" });
      alert(r.ok ? "✅ Connection OK" : "❌ Connection failed");
    } else if (btn.dataset.act === "start") {
      await api(`/api/cameras/${id}/start`, { method: "POST" });
    } else if (btn.dataset.act === "stop") {
      await api(`/api/cameras/${id}/stop`, { method: "POST" });
    } else if (btn.dataset.act === "del") {
      if (!confirm("Delete this camera and its zones?")) { btn.disabled = false; return; }
      await api(`/api/cameras/${id}`, { method: "DELETE" });
    }
    renderCameras();
  } catch (err) {
    alert("Error: " + err.message);
    btn.disabled = false;
  }
});

$("camera-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const fd = new FormData(e.target);

  // Build the payload depending on which mode the user is in.
  let payload;
  if (_camMode === "ip") {
    const host = (fd.get("host") || "").trim();
    if (!host) { alert("IP Address is required."); return; }
    payload = {
      name:      (fd.get("name") || host).trim(),
      url:       host,                              // just the host — backend builds RTSP URL
      port:      (fd.get("port") || "").trim() || null,
      username:  (fd.get("username") || "").trim() || null,
      password:  fd.get("password") || null,        // stored encrypted in DB
      latitude:  parseFloat(fd.get("latitude")) || null,
      longitude: parseFloat(fd.get("longitude")) || null,
      enabled:   true,
    };
  } else {
    // Full URL mode — user pasted a complete rtsp:// URL
    const fullUrl = (fd.get("url") || "").trim();
    if (!fullUrl) { alert("RTSP URL is required."); return; }
    payload = {
      name:      (fd.get("name") || fullUrl).trim(),
      url:       fullUrl,       // full URL stored verbatim; build_rtsp returns it as-is
      port:      null,
      username:  null,
      password:  null,
      latitude:  parseFloat(fd.get("latitude")) || null,
      longitude: parseFloat(fd.get("longitude")) || null,
      enabled:   true,
    };
  }

  const btn = $("cam-submit");
  btn.disabled = true;
  btn.textContent = "Adding...";
  try {
    await api("/api/cameras", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    e.target.reset();
    btn.textContent = "+ Add Camera";
    renderCameras();
  } catch (err) {
    alert("Error: " + err.message);
    btn.disabled = false;
    btn.textContent = "+ Add Camera";
  }
});

/* ───────────────── LINES & RULES ───────────────── */
let drawMode = "none";
let drawPoints = [];           // canvas display-space points
let polyClosed = false;
let mousePos = null;           // tracks interactive mouse preview point

const feed = $("feed");
const overlay = $("overlay");

async function renderRules() {
  await refreshCameras();
  const sel = $("rule-cam");
  const running = cameras.filter((c) => c.running);
  const prev = sel.value;
  sel.innerHTML = running
    .map((c) => `<option value="${c.id}">#${c.id} ${c.name}</option>`)
    .join("");
  if (running.length) {
    const keep = running.some((c) => String(c.id) === prev);
    sel.value = keep ? prev : String(running[0].id);
    loadStream(sel.value);
  } else {
    feed.removeAttribute("src");
    $("stage-empty").style.display = "flex";
    $("zone-list").innerHTML =
      `<div class="zone-row" style="color:var(--muted)">Start a camera first.</div>`;
  }
}

$("rule-cam").addEventListener("change", (e) => loadStream(e.target.value));

function loadStream(camId) {
  if (!camId) return;
  $("stage-empty").style.display = "none";
  feed.src = `/api/cameras/${camId}/stream?t=${Date.now()}`;
  resetDraw();
  loadZones();
  syncCanvas();
  setTimeout(syncCanvas, 100);
}

/* draw tool selector */
$("draw-tools").addEventListener("click", (e) => {
  const b = e.target.closest(".seg-btn");
  if (!b) return;
  $$("#draw-tools .seg-btn").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  drawMode = b.dataset.mode;
  resetDraw();
  const hints = {
    none: "Select mode — pick a tool to draw.",
    line: "Line: click 2 points to set the tripwire.",
    circle: "Circle: click the centre, then a point on the edge.",
    polygon: "Polygon: click each corner, double-click to finish.",
  };
  $("draw-hint").textContent = hints[drawMode];
});

$("clear-draw").addEventListener("click", resetDraw);

function resetDraw() {
  drawPoints = [];
  polyClosed = false;
  mousePos = null;
  editingZoneId = null;
  $("save-zone").textContent = "Save Zone";
  syncCanvas();
}

/* keep canvas pixel size synced to the displayed image */
function syncCanvas() {
  const stage = $("stage");
  const w = feed.clientWidth || (stage ? stage.clientWidth : 0) || 920;
  const h = feed.clientHeight || (stage ? stage.clientHeight : 0) || 517;
  
  if (overlay.width !== w || overlay.height !== h) {
    overlay.width = w;
    overlay.height = h;
  }
  redraw();
}
window.addEventListener("resize", syncCanvas);
feed.addEventListener("load", syncCanvas);

/* map a canvas display point to native image pixels */
function toNative(pt) {
  const sx = (feed.naturalWidth || feed.clientWidth || 1) / (feed.clientWidth || 1);
  const sy = (feed.naturalHeight || feed.clientHeight || 1) / (feed.clientHeight || 1);
  return [Math.round(pt[0] * sx), Math.round(pt[1] * sy)];
}

/* map a native point to canvas display-space coordinates */
function toDisplay(pt) {
  const sx = (feed.naturalWidth || feed.clientWidth || 1) / (feed.clientWidth || 1);
  const sy = (feed.naturalHeight || feed.clientHeight || 1) / (feed.clientHeight || 1);
  return [Math.round(pt[0] / sx), Math.round(pt[1] / sy)];
}

overlay.addEventListener("click", (e) => {
  if (drawMode === "none") return;
  const r = overlay.getBoundingClientRect();
  const pt = [e.clientX - r.left, e.clientY - r.top];
  if (drawMode === "line" || drawMode === "circle") {
    if (drawPoints.length >= 2) drawPoints = [];
    drawPoints.push(pt);
  } else if (drawMode === "polygon") {
    if (polyClosed) { drawPoints = []; polyClosed = false; }
    drawPoints.push(pt);
  }
  redraw();
});

overlay.addEventListener("mousemove", (e) => {
  if (drawMode === "none" || drawPoints.length === 0 || polyClosed) {
    mousePos = null;
    return;
  }
  const r = overlay.getBoundingClientRect();
  mousePos = [e.clientX - r.left, e.clientY - r.top];
  redraw();
});

overlay.addEventListener("mouseleave", () => {
  mousePos = null;
  redraw();
});

overlay.addEventListener("dblclick", () => {
  if (drawMode === "polygon" && drawPoints.length >= 3) {
    // If the last point is extremely close to the second-to-last point (from the double click event), pop it.
    if (drawPoints.length > 3) {
      const last = drawPoints[drawPoints.length - 1];
      const prev = drawPoints[drawPoints.length - 2];
      if (dist(last, prev) < 6) {
        drawPoints.pop();
      }
    }
    polyClosed = true;
    mousePos = null;
    redraw();
  }
});

function redraw() {
  const ctx = overlay.getContext("2d");
  ctx.clearRect(0, 0, overlay.width, overlay.height);
  ctx.lineWidth = 2.5;
  ctx.strokeStyle = "#2dd4bf";
  ctx.fillStyle = "#2dd4bf";

  drawPoints.forEach((p) => {
    ctx.beginPath();
    ctx.arc(p[0], p[1], 5, 0, Math.PI * 2);
    ctx.fill();
  });

  if (drawMode === "line" && drawPoints.length === 2) {
    line(ctx, drawPoints[0], drawPoints[1]);
  } else if (drawMode === "circle" && drawPoints.length === 2) {
    const rad = dist(drawPoints[0], drawPoints[1]);
    ctx.beginPath();
    ctx.arc(drawPoints[0][0], drawPoints[0][1], rad, 0, Math.PI * 2);
    ctx.stroke();
  } else if (drawMode === "polygon" && drawPoints.length >= 2) {
    ctx.beginPath();
    ctx.moveTo(...drawPoints[0]);
    drawPoints.slice(1).forEach((p) => ctx.lineTo(...p));
    if (polyClosed) ctx.closePath();
    ctx.stroke();
  }

  // Draw interactive guide/preview while drawing
  if (mousePos && !polyClosed) {
    ctx.save();
    ctx.setLineDash([6, 4]);
    ctx.strokeStyle = "rgba(45, 212, 191, 0.65)";
    if (drawMode === "line" && drawPoints.length === 1) {
      line(ctx, drawPoints[0], mousePos);
    } else if (drawMode === "circle" && drawPoints.length === 1) {
      const rad = dist(drawPoints[0], mousePos);
      ctx.beginPath();
      ctx.arc(drawPoints[0][0], drawPoints[0][1], rad, 0, Math.PI * 2);
      ctx.stroke();
    } else if (drawMode === "polygon" && drawPoints.length >= 1) {
      ctx.beginPath();
      ctx.moveTo(...drawPoints[0]);
      drawPoints.slice(1).forEach((p) => ctx.lineTo(...p));
      ctx.lineTo(...mousePos);
      ctx.stroke();
    }
    ctx.restore();
  }
}
const dist = (a, b) => Math.hypot(b[0] - a[0], b[1] - a[1]);
function line(ctx, a, b) {
  ctx.beginPath(); ctx.moveTo(...a); ctx.lineTo(...b); ctx.stroke();
}

$("save-zone").addEventListener("click", async () => {
  const camId = $("rule-cam").value;
  if (!camId) return alert("Select a running camera first.");
  let coords = null;

  if (drawMode === "line" && drawPoints.length === 2) {
    const [a, b] = drawPoints.map(toNative);
    coords = { x1: a[0], y1: a[1], x2: b[0], y2: b[1] };
  } else if (drawMode === "circle" && drawPoints.length === 2) {
    const c = toNative(drawPoints[0]);
    // Scale the radius by the horizontal display->native factor so it
    // matches the cx/cy coordinate space. Measuring dist() between two
    // separately x/y-scaled native points distorts the radius when the
    // video is shown at a non-native size, which can place the circle
    // off-target so intrusions are never detected.
    const sx = (feed.naturalWidth || feed.clientWidth || 1) / (feed.clientWidth || 1);
    coords = {
      cx: c[0],
      cy: c[1],
      radius: Math.round(dist(drawPoints[0], drawPoints[1]) * sx),
    };
  } else if (drawMode === "polygon" && drawPoints.length >= 3) {
    coords = { points: drawPoints.map(toNative) };
  } else {
    return alert("Finish drawing the zone first.");
  }

  try {
    if (editingZoneId) {
      await api(`/api/zones/${editingZoneId}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: $("zone-name").value || drawMode + " zone",
          coordinates: coords,
        }),
      });
    } else {
      await api(`/api/cameras/${camId}/zones`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          zone_type: drawMode,
          name: $("zone-name").value || drawMode + " zone",
          coordinates: coords,
        }),
      });
    }
    $("zone-name").value = "";
    resetDraw();
    loadZones();
  } catch (err) {
    alert("Error: " + err.message);
  }
});

async function loadZones() {
  const camId = $("rule-cam").value;
  if (!camId) return;
  const zones = await api(`/api/cameras/${camId}/zones`);
  activeZones = zones;
  $("zone-list").innerHTML = zones.length
    ? zones
        .map(
          (z) => `<div class="zone-row" data-id="${z.id}">
            <span>
              <span class="zone-badge zb-${z.zone_type}">${z.zone_type}</span>
              ${z.name}
            </span>
            <div style="display: flex; gap: 8px;">
              <button class="btn sm edit-zone-btn" data-id="${z.id}">✏️ Edit</button>
              <button class="btn danger sm remove-zone-btn" data-id="${z.id}">Remove</button>
            </div>
          </div>`
        )
        .join("")
    : `<div class="zone-row" style="color:var(--muted)">No zones yet.</div>`;
}

$("zone-list").addEventListener("click", async (e) => {
  const editBtn = e.target.closest(".edit-zone-btn");
  const removeBtn = e.target.closest(".remove-zone-btn");

  if (editBtn) {
    const id = parseInt(editBtn.dataset.id, 10);
    const zone = activeZones.find(z => z.id === id);
    if (!zone) return;

    editingZoneId = zone.id;
    $("zone-name").value = zone.name;

    drawMode = zone.zone_type;
    $$("#draw-tools .seg-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.mode === drawMode);
    });

    const sx = (feed.naturalWidth || feed.clientWidth || 1) / (feed.clientWidth || 1);

    if (drawMode === "line") {
      drawPoints = [
        toDisplay([zone.coordinates.x1, zone.coordinates.y1]),
        toDisplay([zone.coordinates.x2, zone.coordinates.y2])
      ];
    } else if (drawMode === "circle") {
      const center = toDisplay([zone.coordinates.cx, zone.coordinates.cy]);
      const radiusDisplay = zone.coordinates.radius / sx;
      drawPoints = [
        center,
        [center[0] + radiusDisplay, center[1]]
      ];
    } else if (drawMode === "polygon") {
      drawPoints = zone.coordinates.points.map(pt => toDisplay(pt));
      polyClosed = true;
    }

    $("save-zone").textContent = "✏️ Update Zone";
    $("draw-hint").textContent = `Editing: Redraw coordinates on the video feed or change the name, then click 'Update Zone'.`;
    redraw();
  }

  if (removeBtn) {
    const id = removeBtn.dataset.id;
    if (!confirm("Are you sure you want to delete this zone?")) return;
    try {
      await api(`/api/zones/${id}`, { method: "DELETE" });
      if (editingZoneId === parseInt(id, 10)) {
        resetDraw();
      }
      loadZones();
    } catch (err) {
      alert("Error deleting zone: " + err.message);
    }
  }
});

/* ───────────────── ALERTS ───────────────── */
async function loadAlerts() {
  let alerts = [];
  try { alerts = await api("/api/alerts?limit=40"); } catch (_) {}
  $("alert-count").textContent = alerts.length;
  $("alerts-empty").classList.toggle("show", alerts.length === 0);
  $("alerts-grid").innerHTML = alerts
    .map((a) => {
      const file = a.image_path
        ? a.image_path.replace(/\\/g, "/").split("/").pop()
        : null;
      return `<div class="alert-card" data-id="${a.id}">
        <button class="btn-delete-alert" data-id="${a.id}" title="Delete alert">🗑️</button>
        ${file ? `<img src="/snapshots/${file}" alt="evidence" />` : ""}
        <div class="a-meta">
          <span class="a-type">${a.alert_type}</span><br/>
          <b>${a.label} #${a.track_id}</b><br/>
          <small>Camera ${a.camera_id} · ${new Date(a.timestamp).toLocaleString()}</small>
        </div>
      </div>`;
    })
    .join("");
}
$("alerts-refresh").addEventListener("click", loadAlerts);

// Click delegation for deleting individual alerts
$("alerts-grid").addEventListener("click", async (e) => {
  const btn = e.target.closest(".btn-delete-alert");
  if (!btn) return;
  const alertId = btn.dataset.id;
  if (!confirm("Are you sure you want to delete this alert?")) return;
  try {
    await api(`/api/alerts/${alertId}`, { method: "DELETE" });
    loadAlerts();
  } catch (err) {
    alert("Error deleting alert: " + err.message);
  }
});

// Click handler for clearing all alerts
$("alerts-clear").addEventListener("click", async () => {
  if (!confirm("Are you sure you want to clear all alerts? This will delete all snapshot images on disk permanently.")) return;
  try {
    await api("/api/alerts", { method: "DELETE" });
    loadAlerts();
  } catch (err) {
    alert("Error clearing alerts: " + err.message);
  }
});

/* ───────────────── BOOT / POLLING ───────────────── */
async function tick() {
  await refreshSystem();
  if (currentView === "live")    renderLive();
  if (currentView === "cameras") renderCameras();
  if (currentView === "alerts" || currentView === "live") {
    try {
      const a = await api("/api/alerts?limit=40");
      $("alert-count").textContent = a.length;
    } catch (_) {}
  }
}

refreshSystem().then(() => showView("live"));
loadAlerts();
setInterval(tick, 5000);
