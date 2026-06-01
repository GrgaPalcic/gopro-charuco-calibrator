const form = document.getElementById("configForm");
const previewBtn = document.getElementById("previewBtn");
const startRunBtn = document.getElementById("startRunBtn");
const stopBtn = document.getElementById("stopBtn");
const pauseBtn = document.getElementById("pauseBtn");
const resumeBtn = document.getElementById("resumeBtn");
const nextCameraBtn = document.getElementById("nextCameraBtn");
const captureBtn = document.getElementById("captureBtn");
const solveBtn = document.getElementById("solveBtn");
const statusLine = document.getElementById("statusLine");
const preview = document.getElementById("preview");
const results = document.getElementById("results");
const scatter = document.getElementById("scatter");
const guideOverlay = document.getElementById("guideOverlay");
const guidePrompt = document.getElementById("guidePrompt");

let defaults = null;
let pollTimer = null;

function setDeep(obj, path, value) {
  const parts = path.split(".");
  let cur = obj;
  for (const part of parts.slice(0, -1)) {
    cur[part] = cur[part] || {};
    cur = cur[part];
  }
  cur[parts[parts.length - 1]] = value;
}

function getDeep(obj, path) {
  return path.split(".").reduce((cur, part) => (cur ? cur[part] : undefined), obj);
}

function optionLabel(value, label) {
  return `${label} (${value})`;
}

function populateGoProOptions(options) {
  for (const select of form.querySelectorAll("[data-gopro-options]")) {
    const key = select.dataset.goproOptions;
    const def = options[key];
    select.innerHTML = "";
    if (select.dataset.optional === "true") {
      select.append(new Option("Leave unchanged", ""));
    }
    for (const [value, label] of Object.entries(def.options || {})) {
      select.append(new Option(optionLabel(value, label), value));
    }
  }
}

function setFormValue(name, value) {
  const input = form.elements[name];
  if (!input) return;
  if (input.type === "checkbox") {
    input.checked = Boolean(value);
  } else if (input.dataset.mm === "true") {
    input.value = Number(value * 1000).toFixed(1);
  } else if (input.dataset.optional === "true" && value === null) {
    input.value = "";
  } else {
    input.value = value ?? "";
  }
}

function populateForm(config, dicts, goproOptions) {
  populateGoProOptions(goproOptions);
  const dictSelect = form.elements["board.aruco_dict"];
  dictSelect.innerHTML = "";
  for (const name of dicts) {
    dictSelect.append(new Option(name, name));
  }
  for (const input of form.elements) {
    if (!input.name) continue;
    setFormValue(input.name, getDeep(config, input.name));
  }
}

function readForm() {
  const config = structuredClone(defaults.config);
  for (const input of form.elements) {
    if (!input.name) continue;
    let value = input.type === "checkbox" ? input.checked : input.value;
    if (input.dataset.optional === "true" && value === "") {
      value = null;
    } else if (input.type === "number" || input.dataset.goproOptions) {
      value = value === "" ? null : Number(value);
    }
    if (input.dataset.mm === "true") value = Number(input.value) / 1000;
    setDeep(config, input.name, value);
  }
  return config;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function percent(value) {
  return `${Math.round((value || 0) * 100)}%`;
}

function updateBars(coverage) {
  document.getElementById("barX").value = coverage?.x?.progress || 0;
  document.getElementById("barY").value = coverage?.y?.progress || 0;
  document.getElementById("barSize").value = coverage?.size?.progress || 0;
  document.getElementById("barSkew").value = coverage?.skew?.progress || 0;
}

function drawScatter(points) {
  const ctx = scatter.getContext("2d");
  const w = scatter.width;
  const h = scatter.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#080a0d";
  ctx.fillRect(0, 0, w, h);
  ctx.strokeStyle = "#46505c";
  ctx.lineWidth = 1;
  for (let i = 1; i < 5; i++) {
    const x = (i / 5) * w;
    const y = (i / 5) * h;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    ctx.lineTo(x, h);
    ctx.moveTo(0, y);
    ctx.lineTo(w, y);
    ctx.stroke();
  }
  ctx.strokeStyle = "#d49a37";
  ctx.lineWidth = 2;
  ctx.strokeRect(0.2 * w, 0.2 * h, 0.6 * w, 0.6 * h);
  for (const point of points || []) {
    const x = point.x * w;
    const y = point.y * h;
    const r = 3 + 8 * Math.min(point.size || 0, 0.7);
    ctx.fillStyle = `rgba(74, 166, 255, ${0.45 + 0.45 * Math.min(point.skew || 0, 1)})`;
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fill();
  }
}

function drawGuideOverlay(status) {
  const ctx = guideOverlay.getContext("2d");
  const rect = guideOverlay.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, rect.width);
  const cssH = Math.max(1, rect.height);
  const pixelW = Math.round(cssW * dpr);
  const pixelH = Math.round(cssH * dpr);
  if (guideOverlay.width !== pixelW || guideOverlay.height !== pixelH) {
    guideOverlay.width = pixelW;
    guideOverlay.height = pixelH;
  }
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  const checkpoints = status.guide?.checkpoints || [];
  if (!checkpoints.length) return;

  const naturalW = preview.naturalWidth || status.image_size?.[0] || cssW;
  const naturalH = preview.naturalHeight || status.image_size?.[1] || cssH;
  const scale = Math.min(cssW / naturalW, cssH / naturalH);
  const imageW = naturalW * scale;
  const imageH = naturalH * scale;
  const offsetX = (cssW - imageW) / 2;
  const offsetY = (cssH - imageH) / 2;

  function px(x) {
    return offsetX + x * imageW;
  }

  function py(y) {
    return offsetY + y * imageH;
  }

  function drawTarget(point, color, radius) {
    const x = px(point.x);
    const y = py(point.y);
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.save();
  ctx.strokeStyle = "rgba(255, 211, 105, 0.95)";
  ctx.lineWidth = 4;
  ctx.setLineDash([14, 10]);
  ctx.strokeRect(px(0.2), py(0.2), imageW * 0.6, imageH * 0.6);
  ctx.setLineDash([]);
  ctx.strokeStyle = "rgba(255, 211, 105, 0.35)";
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(px(0.5), py(0.5));
  ctx.lineTo(px(0.2), py(0.5));
  ctx.moveTo(px(0.5), py(0.5));
  ctx.lineTo(px(0.8), py(0.5));
  ctx.moveTo(px(0.5), py(0.5));
  ctx.lineTo(px(0.5), py(0.2));
  ctx.moveTo(px(0.5), py(0.5));
  ctx.lineTo(px(0.5), py(0.8));
  ctx.stroke();
  ctx.restore();

  for (const point of checkpoints) {
    const radius = point.current ? 12 : 7;
    const color = point.complete ? "#53d769" : point.current ? "#ffcc4d" : "#6bb6ff";
    drawTarget(point, color, radius);
  }

  const current = status.guide?.current;
  if (current) {
    const boxW = Math.max(80, current.size * imageW);
    const boxH = Math.max(50, current.size * imageH);
    const x = px(current.x) - boxW / 2;
    const y = py(current.y) - boxH / 2;
    ctx.strokeStyle = current.live_match ? "#53d769" : "#ffcc4d";
    ctx.lineWidth = 5;
    ctx.strokeRect(x, y, boxW, boxH);
  }
  const pose = status.pose;
  if (pose) {
    ctx.fillStyle = "#ff4f64";
    ctx.beginPath();
    ctx.arc(px(pose.x), py(pose.y), 10, 0, Math.PI * 2);
    ctx.fill();
  }
}

function guideText(status) {
  const guide = status.guide;
  if (!guide?.current) return "Follow the guide line with the board center.";
  const current = guide.current;
  const base = `Route ${guide.complete_count}/${guide.total_count}: ${current.label}`;
  if (current.live_match) return `${base} - hold still`;
  return `${base} - move board center to the highlighted point and match the box size`;
}

function updateStatus(status) {
  statusLine.textContent = `${status.state || "idle"}: ${status.message || ""}`;
  const captures = status.captures || 0;
  const target = status.target_samples || 0;
  document.getElementById("captureCount").textContent = `${captures}/${target}`;
  document.getElementById("markerCount").textContent = status.markers || 0;
  document.getElementById("coverageOverall").textContent = percent(status.coverage?.overall);
  document.getElementById("guideCount").textContent =
    `${status.guide?.complete_count || 0}/${status.guide?.total_count || 0}`;
  guidePrompt.textContent = guideText(status);
  updateBars(status.coverage || {});
  drawScatter(status.coverage?.points || []);
  drawGuideOverlay(status);

  const state = status.state || "idle";
  const previewOpen = !["idle", "error"].includes(state);
  const capturing = state === "capturing";
  const paused = state === "paused";
  previewBtn.disabled = previewOpen;
  startRunBtn.disabled = state === "capturing" || state === "solving";
  stopBtn.disabled = state === "idle";
  pauseBtn.disabled = !capturing;
  resumeBtn.disabled = !paused;
  captureBtn.disabled = !capturing;
  solveBtn.disabled = captures < Math.max(3, Number(form.elements["solver.min_frames"].value || 25));
  if (previewOpen || captures > 0) {
    preview.src = `/api/session/latest.jpg?t=${Date.now()}`;
  }
  if (status.results) {
    results.textContent = JSON.stringify(status.results, null, 2);
  }
  if (status.gopro && status.gopro.enabled && !status.gopro.ok) {
    results.textContent = JSON.stringify(status.gopro, null, 2);
  }
  if (status.video_bridge && status.video_bridge.enabled && !status.video_bridge.ok) {
    results.textContent = JSON.stringify(status.video_bridge, null, 2);
  }
}

async function poll() {
  try {
    updateStatus(await api("/api/session/status"));
  } catch (err) {
    statusLine.textContent = String(err);
  }
}

function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(poll, 500);
}

previewBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/preview", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
});

startRunBtn.addEventListener("click", async () => {
  results.textContent = "";
  updateStatus(await api("/api/session/run", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
});

pauseBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/pause", {method: "POST"}));
});

resumeBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/resume", {method: "POST"}));
});

stopBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/stop", {method: "POST"}));
  clearInterval(pollTimer);
});

captureBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/capture", {method: "POST"}));
});

solveBtn.addEventListener("click", async () => {
  statusLine.textContent = "solving...";
  const summary = await api("/api/session/solve", {method: "POST"});
  results.textContent = JSON.stringify(summary.results, null, 2);
  await poll();
});

nextCameraBtn.addEventListener("click", async () => {
  const config = readForm();
  config.camera.camera_name = `${config.camera.camera_name}_next`;
  setFormValue("camera.camera_name", config.camera.camera_name);
  results.textContent = "";
  updateStatus(await api("/api/session/next-camera", {
    method: "POST",
    body: JSON.stringify({config}),
  }));
  startPolling();
});

async function init() {
  defaults = await api("/api/defaults");
  populateForm(defaults.config, defaults.aruco_dictionaries, defaults.gopro_options);
  statusLine.textContent = "Ready";
  drawScatter([]);
  await poll();
}

init().catch((err) => {
  statusLine.textContent = String(err);
});
