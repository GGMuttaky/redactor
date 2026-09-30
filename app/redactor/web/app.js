/* SPDX-License-Identifier: GPL-3.0-or-later
   Copyright (C) 2026 Hafiz */
"use strict";
/* Redactor review UI. Plain JS, no build step: the page is served by redactor/server.py. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TOKEN = window.TOKEN;
const CHUNK = 300; // frames of boxes fetched per request

const video = $("#video");
const canvas = $("#canvas");
const ctx = canvas.getContext("2d");
const tiny = document.createElement("canvas");
const tctx = tiny.getContext("2d");

const S = {
  id: null, project: null, ready: false, loadingReview: false,
  fps: 30, n: 0, w: 1, h: 1, scale: 1, times: [],
  tracks: [], byId: new Map(), disabled: new Set(), density: [],
  regions: [], chunks: new Map(), loading: new Set(),
  frame: 0, view: "blur", style: { mode: "blur", shape: "ellipse" },
  selected: null, mode: null, drag: null, filter: "all",
  exportOpen: false, statusStart: null,
};

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: { "X-Token": TOKEN, "Content-Type": "application/json" },
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const media = (path) => `/media/${S.id}/${path}?token=${encodeURIComponent(TOKEN)}`;

/* ---------------------------------------------------------------- formatting */

function fmtTime(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60);
  const s = (sec % 60).toFixed(2).padStart(5, "0");
  return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${String(m).padStart(2, "0")}:${s}`;
}
const frameTime = (f) => (S.times[f] !== undefined ? S.times[f] : f / S.fps);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

/* ---------------------------------------------------------------- views */

function showView(name) {
  $("#home").classList.toggle("hidden", name !== "home");
  $("#project").classList.toggle("hidden", name !== "project");
  $("#topActions").classList.toggle("hidden", !(name === "project" && S.ready));
}

async function loadHome() {
  stopPolling();
  resetProject();
  history.replaceState(null, "", "/");
  $("#crumb").innerHTML = "";
  showView("home");
  try {
    const st = await api("/api/status");
    $("#hwInfo").textContent = st.ffmpeg
      ? `Finding faces on the ${st.gpu ? "graphics card" : "CPU (no usable graphics card found)"} · ` +
        `exporting with the ${st.encoder === "CPU" ? "CPU video encoder" : `${st.encoder} graphics card's video encoder`}.`
      : "";
    const warn = $("#toolWarn");
    warn.classList.toggle("hidden", st.ffmpeg && st.ffprobe);
    warn.innerHTML = "<b>ffmpeg is missing.</b> Redactor needs it to read and write video. Install it with " +
      "<kbd>winget install Gyan.FFmpeg</kbd>, or put <code>ffmpeg.exe</code> and <code>ffprobe.exe</code> in the app's <code>bin</code> folder, then restart.";
    const list = await api("/api/projects");
    const ul = $("#recentList");
    ul.innerHTML = list.length ? "" : '<li class="empty">Videos you open appear here.</li>';
    for (const p of list) {
      const li = document.createElement("li");
      li.className = "item";
      const chip = { ready: "ready", error: "error", analyzing: "busy", queued: "busy" }[p.status] || "";
      const label = { ready: "Ready", error: "Error", analyzing: "Analysing", queued: "Queued", cancelled: "Cancelled", new: "New" }[p.status] || p.status;
      li.innerHTML = `<span class="nm" title="${esc(p.source)}">${esc(p.name)}</span><span class="when">${esc(p.created || "")}</span><span class="chip ${chip}">${label}</span>`;
      li.onclick = () => openProject(p.id);
      ul.appendChild(li);
    }
  } catch (e) {
    showHomeError(e.message);
  }
}

function showHomeError(msg) {
  const el = $("#homeError");
  el.textContent = msg;
  el.classList.remove("hidden");
}

async function openPath(path) {
  $("#homeError").classList.add("hidden");
  try {
    const d = await api("/api/projects", { method: "POST", body: { path, sensitivity: $("#sens").value, stride: +$("#stride").value } });
    openProject(d.id);
  } catch (e) {
    showHomeError(e.message);
  }
}

$("#pickBtn").onclick = async () => {
  const btn = $("#pickBtn");
  btn.disabled = true;
  btn.textContent = "Choose in the window that opened…";
  try {
    const r = await api("/api/pick", { method: "POST" });
    if (r.path) await openPath(r.path);
  } catch (e) {
    showHomeError(e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Choose a video…";
  }
};
$("#pathForm").onsubmit = (e) => {
  e.preventDefault();
  const v = $("#pathInput").value.trim().replace(/^"(.*)"$/, "$1");
  if (v) openPath(v);
};
$("#homeBtn").onclick = () => loadHome();

/* ---------------------------------------------------------------- project lifecycle */

function resetProject() {
  S.id = null; S.project = null; S.ready = false; S.loadingReview = false;
  S.chunks.clear(); S.loading.clear(); S.tracks = []; S.byId.clear(); S.disabled.clear();
  S.regions = []; S.selected = null; S.mode = null; S.drag = null; S.frame = 0; S.times = [];
  video.pause();
  video.removeAttribute("src");
  video.load();
  canvas.classList.add("hidden");
  $("#drawHint").classList.add("hidden");
  closeModal();
}

async function openProject(id) {
  resetProject();
  S.id = id;
  history.replaceState(null, "", "#" + id);
  showView("project");
  try {
    const d = await api(`/api/projects/${id}`);
    applyProject(d);
    if (d.status === "ready") await loadReview(d);
    else startPolling();
  } catch (e) {
    loadHome().then(() => showHomeError(e.message));
  }
}

function applyProject(d) {
  S.project = d;
  const p = d.probe;
  const meta = p ? `${d.width || p.width}×${d.height || p.height} · ${(d.fps || p.fps).toFixed(2)} fps · ${fmtTime(p.duration)}` : "";
  $("#crumb").innerHTML = `<span class="name" title="${esc(d.source)}">${esc(d.name)}</span><span class="meta">${meta}</span>`;
  S.style = d.style || S.style;
  $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === S.style.mode));
  $$("#shapeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.shape === S.style.shape));
}

function showStatus(d) {
  const card = $("#statusCard");
  card.classList.remove("hidden");
  faceList.empty = "Faces appear here when the analysis has finished.";
  faceList.set([]);
  $("#faceCount").textContent = "";
  $("#allOn").classList.add("hidden");
  $("#weakOff").classList.add("hidden");
  const busy = d.status === "queued" || d.status === "analyzing";
  const pr = d.progress || { stage: "Queued", value: 0 };
  $("#statusStage").textContent = d.status === "error" ? "Something went wrong"
    : d.status === "cancelled" ? "Analysis cancelled" : d.status === "queued" ? "Waiting to start…"
    : pr.stage.includes("·") ? pr.stage : pr.stage + "…";
  $("#statusBar").style.width = `${Math.round((busy ? pr.value : d.status === "ready" ? 1 : 0) * 100)}%`;
  $("#statusPct").textContent = busy ? `${Math.round(pr.value * 100)}%` : d.error || "";
  $("#cancelBtn").classList.toggle("hidden", !busy);
  $("#retryBtn").classList.toggle("hidden", busy);
  // ETA from this stage's own rate.
  const now = performance.now();
  if (!S.statusStart || S.statusStart.stage !== pr.stage || pr.value < S.statusStart.value) {
    S.statusStart = { stage: pr.stage, value: pr.value, t: now };
  }
  const dv = pr.value - S.statusStart.value, dt = (now - S.statusStart.t) / 1000;
  $("#statusEta").textContent = busy && dv > 0.02 && dt > 3 ? `about ${fmtDur(((1 - pr.value) * dt) / dv)} left` : "";
}
const fmtDur = (s) => (s < 60 ? `${Math.ceil(s)} s` : s < 3600 ? `${Math.round(s / 60)} min` : `${(s / 3600).toFixed(1)} h`);

$("#cancelBtn").onclick = () => api(`/api/projects/${S.id}/cancel`, { method: "POST" });
$("#retryBtn").onclick = async () => {
  await api(`/api/projects/${S.id}/analyze`, { method: "POST", body: {} });
  startPolling();
};

let pollTimer = null;
function startPolling() {
  stopPolling();
  pollTimer = setInterval(poll, 700);
  poll();
}
function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}
async function poll() {
  if (!S.id) return stopPolling();
  let d;
  try {
    d = await api(`/api/projects/${S.id}`);
  } catch {
    return;
  }
  S.project = d;
  const analysing = d.status === "queued" || d.status === "analyzing";
  const exporting = d.export_job && d.export_job.status === "running";
  if (analysing || d.status === "error" || d.status === "cancelled") {
    if (S.ready) leaveReview();
    applyProject(d);
    showStatus(d);
  } else if (d.status === "ready" && !S.ready) {
    await loadReview(d);
  }
  if (S.exportOpen) renderExportModal(d.export_job);
  if (!analysing && !exporting) stopPolling();
}

function leaveReview() {
  S.ready = false;
  video.pause();
  canvas.classList.add("hidden");
  showView("project");
}

async function loadReview(d) {
  if (S.loadingReview) return;
  S.loadingReview = true;
  try {
    applyProject(d);
    S.fps = d.fps; S.n = d.n_frames; S.w = d.width; S.h = d.height;
    const [times, tr, dens] = await Promise.all([
      api(`/api/projects/${S.id}/times`), api(`/api/projects/${S.id}/tracks`), api(`/api/projects/${S.id}/density`),
    ]);
    S.times = times;
    S.tracks = tr.tracks.slice().sort((a, b) => a.start - b.start || a.id - b.id);
    S.byId = new Map(S.tracks.map((t) => [t.id, t]));
    S.disabled = new Set(tr.disabled);
    S.density = dens;
    S.regions = d.regions || [];
    S.chunks.clear();
    video.src = media("proxy.mp4");
    await new Promise((ok, fail) => {
      video.onloadeddata = ok;
      video.onerror = () => fail(new Error("The preview copy could not be played."));
    });
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    S.scale = canvas.width / S.w;
    $("#statusCard").classList.add("hidden");
    canvas.classList.remove("hidden");
    S.ready = true;
    showView("project");
    layoutCanvas();
    if ("requestVideoFrameCallback" in video) video.requestVideoFrameCallback(onVideoFrame);
    seekFrame(0);
    refreshFaces(true);
    renderRegions();
    drawTimeline();
  } catch (e) {
    showStatus({ status: "error", error: e.message, progress: {} });
  } finally {
    S.loadingReview = false;
  }
}

/* ---------------------------------------------------------------- player */

function layoutCanvas() {
  if (!S.ready) return;
  const r = $("#player").getBoundingClientRect();
  const a = canvas.width / canvas.height;
  let w = r.width, h = w / a;
  if (h > r.height) { h = r.height; w = h * a; }
  canvas.style.width = `${Math.floor(w)}px`;
  canvas.style.height = `${Math.floor(h)}px`;
}
new ResizeObserver(() => { layoutCanvas(); drawTimeline(); render(); }).observe($("#player"));

function frameAt(t) {
  const T = S.times;
  if (!T.length) return clamp(Math.round(t * S.fps), 0, Math.max(0, S.n - 1));
  let lo = 0, hi = T.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (T[mid] <= t + 1e-4) lo = mid; else hi = mid - 1;
  }
  return lo;
}

function onVideoFrame(now, meta) {
  if (!S.ready) return;
  const f = frameAt(meta.mediaTime);
  if (f !== S.frame) setFrame(f);
  render();
  video.requestVideoFrameCallback(onVideoFrame);
}
video.addEventListener("seeked", () => render());
video.addEventListener("play", () => ($("#playIcon").setAttribute("d", "M6 5h4v14H6zM14 5h4v14h-4z")));
video.addEventListener("pause", () => ($("#playIcon").setAttribute("d", "M8 5v14l11-7z")));

let lastListRefresh = 0;
function setFrame(f) {
  S.frame = f;
  $("#tc").textContent = fmtTime(frameTime(f));
  $("#dur").textContent = `/ ${fmtTime(S.project?.probe?.duration || frameTime(S.n - 1))}`;
  $("#frameNo").textContent = `frame ${f + 1} of ${S.n}`;
  if (S.filter === "now" && performance.now() - lastListRefresh > 250) {
    lastListRefresh = performance.now();
    refreshFaces();
  }
  drawTimeline();
}

function seekFrame(f) {
  if (!S.ready) return;
  f = clamp(Math.round(f), 0, S.n - 1);
  const t0 = frameTime(f), t1 = f + 1 < S.n ? frameTime(f + 1) : t0 + 1 / S.fps;
  video.currentTime = t0 + (t1 - t0) * 0.5; // land inside frame f, not on its edge
  setFrame(f);
  render();
}
const step = (d) => { video.pause(); seekFrame(S.frame + d); };
const togglePlay = () => (video.paused ? video.play() : video.pause());
$("#playBtn").onclick = togglePlay;
$("#prevBtn").onclick = () => step(-1);
$("#nextBtn").onclick = () => step(1);

/* ---------------------------------------------------------------- boxes */

function boxesAt(f) {
  const k = Math.floor(f / CHUNK);
  if (!S.chunks.has(k + 1)) loadChunk(k + 1);
  const c = S.chunks.get(k);
  if (!c) { loadChunk(k); return null; }
  return c[f] || [];
}
async function loadChunk(k) {
  if (k < 0 || k * CHUNK >= S.n || S.chunks.has(k) || S.loading.has(k)) return;
  S.loading.add(k);
  try {
    const id = S.id;
    const d = await api(`/api/projects/${id}/boxes?start=${k * CHUNK}&end=${(k + 1) * CHUNK}`);
    if (id !== S.id) return;
    S.chunks.set(k, d);
    if (Math.floor(S.frame / CHUNK) === k) render();
  } finally {
    S.loading.delete(k);
  }
}

function regionBox(r, f) {
  if (r.enabled === false || f < r.start || f > r.end) return null;
  const keys = Object.entries(r.keys).map(([k, v]) => [+k, v]).sort((a, b) => a[0] - b[0]);
  if (!keys.length) return null;
  if (f <= keys[0][0]) return keys[0][1];
  for (let i = 0; i + 1 < keys.length; i++) {
    const [fa, a] = keys[i], [fb, b] = keys[i + 1];
    if (f >= fa && f <= fb) {
      const t = (f - fa) / (fb - fa);
      return a.map((v, j) => v + (b[j] - v) * t);
    }
  }
  return keys[keys.length - 1][1];
}
// Box shown for a region at f even when it is switched off, so it can still be edited.
function regionBoxAny(r, f) {
  return regionBox({ ...r, enabled: true, start: Math.min(r.start, f), end: Math.max(r.end, f) }, f);
}

/* ---------------------------------------------------------------- drawing */

function effect(x, y, w, h, mode, oval) {
  if (w < 1 || h < 1) return;
  ctx.save();
  ctx.beginPath();
  if (oval) ctx.ellipse(x + w / 2, y + h / 2, w / 2, h / 2, 0, 0, Math.PI * 2);
  else ctx.rect(x, y, w, h);
  ctx.clip();
  if (mode === "solid") {
    ctx.fillStyle = "#000";
    ctx.fillRect(x, y, w, h);
  } else if (mode === "pixelate") {
    const cell = Math.max(1, Math.min(w, h) / 8);
    tiny.width = Math.max(1, Math.round(w / cell));
    tiny.height = Math.max(1, Math.round(h / cell));
    tctx.drawImage(video, x, y, w, h, 0, 0, tiny.width, tiny.height);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(tiny, 0, 0, tiny.width, tiny.height, x, y, w, h);
  } else {
    const r = Math.max(2, Math.min(w, h) / 5), m = r * 2;
    ctx.filter = `blur(${r}px)`;
    ctx.drawImage(video, x - m, y - m, w + 2 * m, h + 2 * m, x - m, y - m, w + 2 * m, h + 2 * m);
  }
  ctx.restore();
}

function render() {
  if (!S.ready || canvas.classList.contains("hidden")) return;
  const sc = S.scale, lw = Math.max(1.5, canvas.width / 600);
  ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
  const boxes = boxesAt(S.frame) || [];
  const sel = S.selected;
  const preview = S.view === "blur";
  for (const [tid, x1, y1, x2, y2, det] of boxes) {
    const on = !S.disabled.has(tid);
    const x = x1 * sc, y = y1 * sc, w = (x2 - x1) * sc, h = (y2 - y1) * sc;
    if (preview) {
      if (on) effect(x, y, w, h, S.style.mode, S.style.shape === "ellipse");
    } else {
      ctx.setLineDash(on ? [] : [5, 4]);
      ctx.lineWidth = lw;
      ctx.strokeStyle = on ? (det ? "rgba(45,212,191,1)" : "rgba(45,212,191,.55)") : "rgba(245,158,11,.95)";
      ctx.strokeRect(x, y, w, h);
    }
    if (sel && sel.type === "face" && sel.id === tid) {
      ctx.setLineDash([]);
      ctx.lineWidth = lw * 2;
      ctx.strokeStyle = "#fff";
      ctx.strokeRect(x - lw, y - lw, w + 2 * lw, h + 2 * lw);
    }
  }
  ctx.setLineDash([]);
  for (const r of S.regions) {
    const isSel = sel && sel.type === "region" && sel.id === r.id;
    let b = S.drag && S.drag.reg && S.drag.reg.id === r.id && S.drag.box ? S.drag.box : regionBox(r, S.frame);
    if (!b && isSel) b = regionBoxAny(r, S.frame);
    if (!b) continue;
    const x = b[0] * sc, y = b[1] * sc, w = (b[2] - b[0]) * sc, h = (b[3] - b[1]) * sc;
    const active = regionBox(r, S.frame);
    if (preview && active) effect(x, y, w, h, S.style.mode, false);
    if (!preview || isSel) {
      ctx.lineWidth = isSel ? lw * 1.6 : lw;
      ctx.setLineDash(active ? [] : [5, 4]);
      ctx.strokeStyle = "#a78bfa";
      ctx.strokeRect(x, y, w, h);
      ctx.setLineDash([]);
    }
    if (isSel) {
      ctx.fillStyle = "#a78bfa";
      const hs = lw * 4;
      for (const [hx, hy] of [[x, y], [x + w, y], [x, y + h], [x + w, y + h]]) ctx.fillRect(hx - hs / 2, hy - hs / 2, hs, hs);
    }
  }
  if (S.drag && S.drag.kind === "new") {
    const { x0, y0, x1, y1 } = S.drag;
    ctx.lineWidth = lw;
    ctx.strokeStyle = "#a78bfa";
    ctx.setLineDash([6, 4]);
    ctx.strokeRect(Math.min(x0, x1) * sc, Math.min(y0, y1) * sc, Math.abs(x1 - x0) * sc, Math.abs(y1 - y0) * sc);
    ctx.setLineDash([]);
  }
}

/* ---------------------------------------------------------------- canvas interaction */

function toSrc(e) {
  const r = canvas.getBoundingClientRect();
  return {
    x: clamp(((e.clientX - r.left) / r.width) * S.w, 0, S.w),
    y: clamp(((e.clientY - r.top) / r.height) * S.h, 0, S.h),
  };
}
const inside = (b, p, m = 0) => p.x >= b[0] - m && p.x <= b[2] + m && p.y >= b[1] - m && p.y <= b[3] + m;

function faceAt(p) {
  let best = null, area = Infinity;
  for (const [tid, x1, y1, x2, y2] of boxesAt(S.frame) || []) {
    const a = (x2 - x1) * (y2 - y1);
    if (inside([x1, y1, x2, y2], p) && a < area) { best = tid; area = a; }
  }
  return best;
}
function regionAt(p) {
  for (const r of [...S.regions].reverse()) {
    const b = regionBox(r, S.frame) || (S.selected?.type === "region" && S.selected.id === r.id ? regionBoxAny(r, S.frame) : null);
    if (b && inside(b, p)) return { r, b };
  }
  return null;
}
function handleAt(b, p) {
  const t = 10 * (S.w / canvas.getBoundingClientRect().width);
  const near = (x, y) => Math.abs(p.x - x) <= t && Math.abs(p.y - y) <= t;
  if (near(b[0], b[1])) return "nw";
  if (near(b[2], b[1])) return "ne";
  if (near(b[0], b[3])) return "sw";
  if (near(b[2], b[3])) return "se";
  return null;
}

canvas.addEventListener("pointerdown", (e) => {
  if (!S.ready) return;
  const p = toSrc(e);
  if (S.mode === "draw") {
    S.drag = { kind: "new", x0: p.x, y0: p.y, x1: p.x, y1: p.y };
    canvas.setPointerCapture(e.pointerId);
    return;
  }
  const selReg = S.selected?.type === "region" ? S.regions.find((r) => r.id === S.selected.id) : null;
  if (selReg) {
    const b = regionBoxAny(selReg, S.frame);
    const h = b && handleAt(b, p);
    if (h) {
      video.pause();
      S.drag = { kind: h, reg: selReg, orig: b.slice(), x0: p.x, y0: p.y, box: b.slice() };
      canvas.setPointerCapture(e.pointerId);
      return;
    }
  }
  const hit = regionAt(p);
  if (hit) {
    video.pause();
    selectRegion(hit.r.id);
    S.drag = { kind: "move", reg: hit.r, orig: hit.b.slice(), x0: p.x, y0: p.y, box: hit.b.slice() };
    canvas.setPointerCapture(e.pointerId);
    return;
  }
  const fid = faceAt(p);
  if (fid !== null) selectFace(fid, false);
  else { S.selected = null; refreshFaces(); renderRegions(); render(); drawTimeline(); }
});

canvas.addEventListener("pointermove", (e) => {
  if (!S.drag) {
    if (S.mode !== "draw" && S.ready) {
      const p = toSrc(e);
      const selReg = S.selected?.type === "region" ? S.regions.find((r) => r.id === S.selected.id) : null;
      const b = selReg && regionBoxAny(selReg, S.frame);
      const h = b && handleAt(b, p);
      canvas.style.cursor = h ? (h === "nw" || h === "se" ? "nwse-resize" : "nesw-resize")
        : regionAt(p) ? "move" : faceAt(p) !== null ? "pointer" : "default";
    }
    return;
  }
  const p = toSrc(e), d = S.drag;
  if (d.kind === "new") { d.x1 = p.x; d.y1 = p.y; render(); return; }
  const dx = p.x - d.x0, dy = p.y - d.y0, o = d.orig;
  let b = o.slice();
  if (d.kind === "move") {
    const w = o[2] - o[0], h = o[3] - o[1];
    const nx = clamp(o[0] + dx, 0, S.w - w), ny = clamp(o[1] + dy, 0, S.h - h);
    b = [nx, ny, nx + w, ny + h];
  } else {
    if (d.kind.includes("w")) b[0] = clamp(o[0] + dx, 0, o[2] - 4);
    if (d.kind.includes("e")) b[2] = clamp(o[2] + dx, o[0] + 4, S.w);
    if (d.kind.includes("n")) b[1] = clamp(o[1] + dy, 0, o[3] - 4);
    if (d.kind.includes("s")) b[3] = clamp(o[3] + dy, o[1] + 4, S.h);
  }
  d.box = b;
  render();
});

canvas.addEventListener("pointerup", async () => {
  const d = S.drag;
  S.drag = null;
  if (!d) return;
  if (d.kind === "new") {
    const b = [Math.min(d.x0, d.x1), Math.min(d.y0, d.y1), Math.max(d.x0, d.x1), Math.max(d.y0, d.y1)];
    setDrawMode(false);
    if (b[2] - b[0] < 6 || b[3] - b[1] < 6) { render(); return; }
    const r = await api(`/api/projects/${S.id}/regions`, {
      method: "POST", body: { keys: { [S.frame]: b }, start: S.frame, end: S.n - 1 },
    });
    S.regions.push(r);
    switchTab("regions");
    selectRegion(r.id);
    return;
  }
  const moved = d.box.some((v, i) => Math.abs(v - d.orig[i]) > 0.5);
  if (!moved) { render(); return; }
  const r = d.reg;
  r.keys[S.frame] = d.box.map((v) => Math.round(v * 10) / 10);
  r.start = Math.min(r.start, S.frame);
  r.end = Math.max(r.end, S.frame);
  await saveRegion(r);
});

function setDrawMode(on) {
  S.mode = on ? "draw" : null;
  canvas.classList.toggle("draw", on);
  $("#drawHint").classList.toggle("hidden", !on);
  if (on) { video.pause(); canvas.style.cursor = "crosshair"; }
}

/* ---------------------------------------------------------------- faces list (virtual: crowds give thousands) */

class VList {
  constructor(el, itemH, renderItem) {
    this.el = el; this.h = itemH; this.renderItem = renderItem; this.items = [];
    this.inner = document.createElement("div");
    this.inner.className = "inner";
    el.appendChild(this.inner);
    el.addEventListener("scroll", () => this.draw());
    new ResizeObserver(() => this.draw(true)).observe(el);
  }
  set(items) {
    this.items = items;
    this.inner.style.height = `${items.length * this.h}px`;
    this.draw(true);
  }
  draw(force) {
    const top = this.el.scrollTop, vh = this.el.clientHeight || 600;
    const a = Math.max(0, Math.floor(top / this.h) - 6);
    const b = Math.min(this.items.length, Math.ceil((top + vh) / this.h) + 6);
    if (!force && a === this.a && b === this.b) return;
    this.a = a; this.b = b;
    const nodes = this.items.slice(a, b).map((it, i) => {
      const n = this.renderItem(it);
      n.style.top = `${(a + i) * this.h}px`;
      return n;
    });
    this.inner.replaceChildren(...nodes);
    if (!this.items.length) {
      const p = document.createElement("p");
      p.className = "hint";
      p.style.padding = "12px";
      p.textContent = this.empty || "Nothing here.";
      this.inner.replaceChildren(p);
    }
  }
  reveal(index) {
    const top = index * this.h;
    if (top < this.el.scrollTop || top + this.h > this.el.scrollTop + this.el.clientHeight) {
      this.el.scrollTop = top - this.el.clientHeight / 2 + this.h / 2;
    }
    this.draw(true);
  }
}

const faceList = new VList($("#faceList"), 64, (t) => {
  const el = document.createElement("div");
  const on = !S.disabled.has(t.id);
  const isSel = S.selected?.type === "face" && S.selected.id === t.id;
  el.className = `face${on ? "" : " off"}${isSel ? " sel" : ""}`;
  el.innerHTML = `<div class="th" style="background-image:url('${media(`thumbs/${t.id}.jpg`)}')"></div>
    <div class="txt"><div class="t1">Face ${t.id + 1}${t.weak ? '<span class="badge">maybe not a face</span>' : ""}</div>
    <div class="t2">${fmtTime(frameTime(t.start))} – ${fmtTime(frameTime(t.end))}</div></div>
    <label class="toggle" title="${on ? "Blurred: click to leave visible" : "Left visible: click to blur"}"><input type="checkbox" ${on ? "checked" : ""}><span></span></label>`;
  el.onclick = (e) => { if (!e.target.closest(".toggle")) selectFace(t.id, true); };
  el.querySelector("input").onchange = (e) => setFaces([t.id], e.target.checked);
  return el;
});

function visibleFaces() {
  if (S.filter === "now") return S.tracks.filter((t) => t.start <= S.frame && t.end >= S.frame);
  if (S.filter === "off") return S.tracks.filter((t) => S.disabled.has(t.id));
  return S.tracks;
}
function refreshFaces(reset) {
  faceList.empty = S.tracks.length ? (S.filter === "off" ? "Every face is blurred." : "No faces on screen at this moment.")
    : "No faces were found in this video. You can still hide areas by hand under Regions.";
  const items = visibleFaces();
  if (reset) $("#faceList").scrollTop = 0;
  faceList.set(items);
  const onCount = S.tracks.length - S.disabled.size;
  $("#faceCount").textContent = S.tracks.length ? `${onCount}/${S.tracks.length}` : "0";
  const weakOn = S.tracks.filter((t) => t.weak && !S.disabled.has(t.id)).length;
  $("#weakOff").textContent = `Leave ${weakOn} low-confidence detection${weakOn === 1 ? "" : "s"} visible`;
  $("#weakOff").classList.toggle("hidden", !weakOn);
  $("#allOn").classList.toggle("hidden", !S.disabled.size);
}

async function setFaces(ids, enabled) {
  if (!ids.length) return;
  const r = await api(`/api/projects/${S.id}/tracks`, { method: "POST", body: { ids, enabled } });
  S.disabled = new Set(r.disabled);
  refreshFaces();
  render();
  S.density = await api(`/api/projects/${S.id}/density`);
  drawTimeline();
}

function selectFace(id, jump) {
  S.selected = { type: "face", id };
  const t = S.byId.get(id);
  if (jump && t) { video.pause(); seekFrame(t.best_frame); }
  switchTab("faces");
  const idx = visibleFaces().findIndex((x) => x.id === id);
  if (idx >= 0) faceList.reveal(idx); else refreshFaces();
  renderRegions();
  render();
  drawTimeline();
}

$$("#faceFilter button").forEach((b) => (b.onclick = () => {
  S.filter = b.dataset.f;
  $$("#faceFilter button").forEach((x) => x.classList.toggle("on", x === b));
  refreshFaces(true);
}));
$("#allOn").onclick = () => setFaces([...S.disabled], true);
$("#weakOff").onclick = () => setFaces(S.tracks.filter((t) => t.weak && !S.disabled.has(t.id)).map((t) => t.id), false);

/* ---------------------------------------------------------------- regions list */

function renderRegions() {
  const ul = $("#regionList");
  ul.innerHTML = "";
  $("#regionCount").textContent = S.regions.length ? String(S.regions.length) : "";
  for (const r of S.regions) {
    const li = document.createElement("li");
    const isSel = S.selected?.type === "region" && S.selected.id === r.id;
    const on = r.enabled !== false;
    li.className = `region${isSel ? " sel" : ""}`;
    const nKeys = Object.keys(r.keys).length;
    li.innerHTML = `<div class="row"><span class="sw"></span><input class="label" value="${esc(r.label)}" aria-label="Region name">
      <label class="toggle" title="${on ? "Hidden in export" : "Off"}"><input type="checkbox" ${on ? "checked" : ""}><span></span></label></div>
      <div class="sub">${fmtTime(frameTime(r.start))} – ${fmtTime(frameTime(r.end))} · ${nKeys} position${nKeys === 1 ? "" : "s"}</div>
      <div class="acts"><button class="ghost" data-a="start" title="Region begins at the current frame">Starts here</button>
      <button class="ghost" data-a="end" title="Region ends at the current frame">Ends here</button>
      <button class="ghost" data-a="all" title="From the first frame to the last">Whole video</button>
      <button class="ghost" data-a="jump">Go to start</button><button class="ghost del" data-a="del">Delete</button></div>`;
    li.onclick = (e) => { if (!e.target.closest("button,input,label")) selectRegion(r.id); };
    li.querySelector("input.label").onchange = (e) => { r.label = e.target.value.trim() || r.label; saveRegion(r); };
    li.querySelector(".toggle input").onchange = (e) => { r.enabled = e.target.checked; saveRegion(r); };
    li.querySelector(".acts").onclick = async (e) => {
      const a = e.target.dataset.a;
      if (!a) return;
      if (a === "start") { r.start = Math.min(S.frame, r.end); await saveRegion(r); }
      if (a === "end") { r.end = Math.max(S.frame, r.start); await saveRegion(r); }
      if (a === "all") { r.start = 0; r.end = S.n - 1; await saveRegion(r); }
      if (a === "jump") { selectRegion(r.id); video.pause(); seekFrame(r.start); }
      if (a === "del") await deleteRegion(r.id);
    };
    ul.appendChild(li);
  }
}

async function saveRegion(r) {
  const saved = await api(`/api/projects/${S.id}/regions`, { method: "POST", body: r });
  S.regions = S.regions.map((x) => (x.id === saved.id ? saved : x));
  renderRegions();
  render();
  drawTimeline();
}
async function deleteRegion(id) {
  await api(`/api/projects/${S.id}/regions/${id}`, { method: "DELETE" });
  S.regions = S.regions.filter((r) => r.id !== id);
  if (S.selected?.type === "region" && S.selected.id === id) S.selected = null;
  renderRegions();
  render();
  drawTimeline();
}
function selectRegion(id) {
  S.selected = { type: "region", id };
  switchTab("regions");
  renderRegions();
  refreshFaces();
  render();
  drawTimeline();
}
$("#addRegion").onclick = () => setDrawMode(true);

/* ---------------------------------------------------------------- tabs, view, style */

function switchTab(name) {
  $$(".tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  $("#facesPane").classList.toggle("hidden", name !== "faces");
  $("#regionsPane").classList.toggle("hidden", name !== "regions");
  if (name === "faces") faceList.draw(true);
}
$$(".tabs button").forEach((b) => (b.onclick = () => switchTab(b.dataset.tab)));

function setView(v) {
  S.view = v;
  $$("#viewSeg button").forEach((b) => b.classList.toggle("on", b.dataset.view === v));
  render();
}
$$("#viewSeg button").forEach((b) => (b.onclick = () => setView(b.dataset.view)));

async function setStyle(patch) {
  S.style = { ...S.style, ...patch };
  $$("#modeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.mode === S.style.mode));
  $$("#shapeSeg button").forEach((b) => b.classList.toggle("on", b.dataset.shape === S.style.shape));
  if (S.view !== "blur") setView("blur"); else render();
  await api(`/api/projects/${S.id}/style`, { method: "POST", body: S.style });
}
$$("#modeSeg button").forEach((b) => (b.onclick = () => setStyle({ mode: b.dataset.mode })));
$$("#shapeSeg button").forEach((b) => (b.onclick = () => setStyle({ shape: b.dataset.shape })));

/* ---------------------------------------------------------------- timeline */

const tl = $("#timeline");
function drawTimeline() {
  if (!S.ready) return;
  const dpr = window.devicePixelRatio || 1, W = tl.clientWidth, H = tl.clientHeight;
  if (!W || !H) return;
  if (tl.width !== Math.round(W * dpr) || tl.height !== Math.round(H * dpr)) {
    tl.width = Math.round(W * dpr);
    tl.height = Math.round(H * dpr);
  }
  const c = tl.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  c.clearRect(0, 0, W, H);
  const xOf = (f) => (S.n > 1 ? (f / (S.n - 1)) * W : 0);
  const lane = H - 12;
  const sel = S.selected;
  if (sel?.type === "face" && S.byId.get(sel.id)) {
    const t = S.byId.get(sel.id);
    c.fillStyle = "rgba(255,255,255,.10)";
    c.fillRect(xOf(t.start), 0, Math.max(2, xOf(t.end) - xOf(t.start)), lane);
  }
  const d = S.density;
  let max = 1;
  for (const v of d) if (v > max) max = v;
  c.fillStyle = "rgba(20,184,166,.6)";
  const bw = W / d.length;
  for (let i = 0; i < d.length; i++) {
    if (!d[i]) continue;
    const h = Math.max(2, Math.sqrt(d[i] / max) * (lane - 6));
    c.fillRect(i * bw, lane - h, Math.max(1, bw + 0.3), h);
  }
  for (const r of S.regions) {
    const isSel = sel?.type === "region" && sel.id === r.id;
    c.fillStyle = r.enabled === false ? "#4b5563" : isSel ? "#c4b5fd" : "#a78bfa";
    c.fillRect(xOf(r.start), H - 8, Math.max(2, xOf(r.end) - xOf(r.start)), 5);
  }
  const px = xOf(S.frame);
  c.fillStyle = "#fff";
  c.fillRect(Math.round(px) - 1, 0, 2, H);
}
function timelineSeek(e) {
  const r = tl.getBoundingClientRect();
  const x = e.clientX - r.left;
  video.pause();
  // Snap near the ends so the very first and last frames are easy to reach.
  seekFrame(x <= 6 ? 0 : x >= r.width - 6 ? S.n - 1 : (x / r.width) * (S.n - 1));
}
tl.addEventListener("pointerdown", (e) => {
  if (!S.ready) return;
  tl.setPointerCapture(e.pointerId);
  timelineSeek(e);
  tl.onpointermove = timelineSeek;
});
tl.addEventListener("pointerup", () => (tl.onpointermove = null));

/* ---------------------------------------------------------------- modals: export, re-analyse */

function openModal(html) {
  $("#modalCard").innerHTML = html;
  $("#modal").classList.remove("hidden");
}
function closeModal() {
  $("#modal").classList.add("hidden");
  S.exportOpen = false;
}
$("#modal").addEventListener("pointerdown", (e) => {
  if (e.target.id === "modal" && !(S.project?.export_job?.status === "running" && S.exportOpen)) closeModal();
});

const modeName = { blur: "Blur", pixelate: "Pixelate", solid: "Black box" };

$("#exportBtn").onclick = () => {
  if (!S.ready) return;
  S.exportOpen = true;
  const job = S.project.export_job;
  if (job && job.status === "running") { renderExportModal(job); startPolling(); return; }
  const off = S.disabled.size, regs = S.regions.filter((r) => r.enabled !== false).length;
  const stem = S.project.name.replace(/\.[^.]+$/, "");
  openModal(`<h3>Export a redacted copy</h3>
    <p>${S.tracks.length - off} of ${S.tracks.length} faces blurred${regs ? `, ${regs} region${regs > 1 ? "s" : ""}` : ""}.
    Style: <b>${modeName[S.style.mode]}</b>${S.style.mode !== "solid" ? `, faces as ${S.style.shape === "ellipse" ? "ovals" : "rectangles"}` : ""}.</p>
    ${off ? `<p class="bad">${off} face${off > 1 ? "s are" : " is"} left visible on purpose.</p>` : ""}
    <p class="dim">Saved beside the original. The original is never changed, and an existing export is never overwritten.</p>
    <div class="path">${esc(stem)}_redacted.mp4 + report</div>
    <div class="actions"><button class="ghost" id="mCancel">Cancel</button><button class="primary" id="mGo">Export</button></div>`);
  $("#mCancel").onclick = closeModal;
  $("#mGo").onclick = async () => {
    $("#mGo").disabled = true;
    try {
      await api(`/api/projects/${S.id}/export`, { method: "POST", body: S.style });
      S.project.export_job = { status: "running", progress: 0, stage: "Starting" };
      renderExportModal(S.project.export_job);
      startPolling();
    } catch (e) {
      $("#mGo").disabled = false;
      alert(e.message);
    }
  };
};

function renderExportModal(job) {
  if (!S.exportOpen || !job) return;
  if (job.status === "running") {
    const pct = Math.round((job.progress || 0) * 100);
    openModal(`<h3>Exporting…</h3><p>${esc(job.stage || "Exporting")}</p>
      <div class="bar"><div style="width:${pct}%"></div></div><div class="status-meta"><span>${pct}%</span><span class="dim">Full resolution, original audio</span></div>
      <div class="actions"><button class="ghost" id="mStop">Stop</button></div>`);
    $("#mStop").onclick = () => api(`/api/projects/${S.id}/cancel`, { method: "POST" });
  } else if (job.status === "done") {
    const ok = job.frames_ok;
    openModal(`<h3>Export finished</h3>
      <div class="path">${esc(job.path)}</div>
      <p class="${ok ? "ok" : "bad"}">${ok ? `✓ All ${S.n} frames written and checked.` : `Frame count mismatch: ${job.out_frames} written, ${S.n} expected. Check this file before using it.`}</p>
      ${job.encoder ? `<p class="dim">Encoded with the ${esc(job.encoder)} encoder${job.seconds ? ` in ${fmtDur(job.seconds)}` : ""}.</p>` : ""}
      <p class="dim">Automatic detection can miss faces. Watch the export before you publish it.</p>
      <div class="actions"><button class="ghost" id="mReport">Open report</button><button class="ghost" id="mReveal">Show in folder</button><button class="primary" id="mClose">Done</button></div>`);
    $("#mClose").onclick = closeModal;
    $("#mReveal").onclick = () => api(`/api/projects/${S.id}/reveal`, { method: "POST", body: { path: job.path } });
    $("#mReport").onclick = () => api(`/api/projects/${S.id}/open`, { method: "POST", body: { path: job.report } });
  } else {
    openModal(`<h3>${job.status === "cancelled" ? "Export stopped" : "Export failed"}</h3>
      ${job.error ? `<p class="bad">${esc(job.error)}</p>` : "<p>No file was kept.</p>"}
      <div class="actions"><button class="primary" id="mClose">Close</button></div>`);
    $("#mClose").onclick = closeModal;
  }
}

$("#reanalyzeBtn").onclick = () => {
  const s = S.project.settings;
  openModal(`<h3>Analyse again</h3>
    <p class="dim">Runs detection again with new settings. Regions you drew are kept. Face on/off choices reset, because the faces are found again.</p>
    <label>Sensitivity <select id="mSens"><option value="high">High: catch more, more false alarms</option><option value="normal">Normal</option><option value="low">Low: fewer false alarms</option></select></label>
    <label>Speed <select id="mStride"><option value="1">Thorough: check every frame</option><option value="2">Faster: every 2nd frame (about 2× quicker; can miss brief or fast-moving faces)</option></select></label>
    <div class="actions"><button class="ghost" id="mCancel">Cancel</button><button class="primary" id="mGo">Analyse</button></div>`);
  $("#mSens").value = s.sensitivity;
  $("#mStride").value = String(s.stride);
  $("#mCancel").onclick = closeModal;
  $("#mGo").onclick = async () => {
    await api(`/api/projects/${S.id}/analyze`, { method: "POST", body: { sensitivity: $("#mSens").value, stride: +$("#mStride").value } });
    closeModal();
    leaveReview();
    S.chunks.clear();
    startPolling();
  };
};

/* ---------------------------------------------------------------- keyboard */

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, select, textarea")) return;
  if (e.key === "Escape") {
    if (!$("#modal").classList.contains("hidden") && !(S.project?.export_job?.status === "running" && S.exportOpen)) closeModal();
    else if (S.mode === "draw") { setDrawMode(false); render(); }
    else if (S.selected) { S.selected = null; refreshFaces(); renderRegions(); render(); drawTimeline(); }
    return;
  }
  if (!S.ready || !$("#modal").classList.contains("hidden")) return;
  if (e.key === " ") { e.preventDefault(); togglePlay(); }
  else if (e.key === "ArrowLeft") { e.preventDefault(); step(e.shiftKey ? -Math.round(S.fps) : -1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); step(e.shiftKey ? Math.round(S.fps) : 1); }
  else if (e.key === "Home") { e.preventDefault(); video.pause(); seekFrame(0); }
  else if (e.key === "End") { e.preventDefault(); video.pause(); seekFrame(S.n - 1); }
  else if (e.key === "b" || e.key === "B") setView(S.view === "blur" ? "boxes" : "blur");
  else if ((e.key === "x" || e.key === "X") && S.selected?.type === "face") setFaces([S.selected.id], S.disabled.has(S.selected.id));
  else if ((e.key === "Delete" || e.key === "Backspace") && S.selected?.type === "region") deleteRegion(S.selected.id);
});

/* ---------------------------------------------------------------- start */

window.addEventListener("hashchange", () => {
  const id = location.hash.slice(1);
  if (id && id !== S.id) openProject(id);
  else if (!id && S.id) loadHome();
});

const initial = location.hash.slice(1);
if (initial) openProject(initial); else loadHome();
