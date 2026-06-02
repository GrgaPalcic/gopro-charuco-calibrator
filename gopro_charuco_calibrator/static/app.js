const form = document.getElementById("configForm");
const previewBtn = document.getElementById("previewBtn");
const startRunBtn = document.getElementById("startRunBtn");
const stopBtn = document.getElementById("stopBtn");
const pauseResumeBtn = document.getElementById("pauseResumeBtn");
const nextCameraBtn = document.getElementById("nextCameraBtn");
const captureBtn = document.getElementById("captureBtn");
const solveBtn = document.getElementById("solveBtn");
const statusLine = document.getElementById("statusLine");
const preview = document.getElementById("preview");
const results = document.getElementById("results");
const resultsPanel = document.getElementById("resultsPanel");
const verdictBox = document.getElementById("verdict");
const verdictBadge = document.getElementById("verdictBadge");
const verdictText = document.getElementById("verdictText");
const verdictReasons = document.getElementById("verdictReasons");
const resultGrid = document.getElementById("resultGrid");
const resultsRaw = document.getElementById("resultsRaw");
const scatter = document.getElementById("scatter");
const guideOverlay = document.getElementById("guideOverlay");
const guidePrompt = document.getElementById("guidePrompt");
const streamStatus = document.getElementById("streamStatus");
const streamStatusText = document.getElementById("streamStatusText");
const goproControls = document.getElementById("goproControls");

const presetSelect = document.getElementById("presetSelect");
const presetLoad = document.getElementById("presetLoad");
const presetSave = document.getElementById("presetSave");
const presetMsg = document.getElementById("presetMsg");

const firewallPanel = document.getElementById("firewallPanel");
const firewallHint = document.getElementById("firewallHint");
const firewallCmd = document.getElementById("firewallCmd");
const firewallFirewalld = document.getElementById("firewallFirewalld");
const firewallIptables = document.getElementById("firewallIptables");
const firewallRaw = document.getElementById("firewallRaw");
const firewallCopy = document.getElementById("firewallCopy");
const firewallCopyResult = document.getElementById("firewallCopyResult");
const firewallDismiss = document.getElementById("firewallDismiss");

const STREAM_URL = "/api/session/stream.mjpg";
const FALLBACK_URL = "/api/session/latest.jpg";

let defaults = null;
let pollTimer = null;
let streamActive = false;
let fallbackTimer = null;
let firewallDismissed = false;
let lastFirewallCmd = "";
let autoSolved = false;

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
    if (!def) continue;
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
  syncGoproVisibility();
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

function syncGoproVisibility() {
  const enabled = form.elements["gopro.enabled"];
  if (goproControls && enabled) goproControls.hidden = !enabled.checked;
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

function setStreamStatus(state, text) {
  streamStatus.dataset.state = state;
  streamStatusText.textContent = text;
}

function startStream() {
  if (streamActive) return;
  streamActive = true;
  clearInterval(fallbackTimer);
  fallbackTimer = null;
  setStreamStatus("connecting", "connecting...");
  preview.src = STREAM_URL;
}

function stopStream() {
  if (!streamActive && !fallbackTimer) {
    setStreamStatus("none", "no stream");
    return;
  }
  streamActive = false;
  clearInterval(fallbackTimer);
  fallbackTimer = null;
  preview.removeAttribute("src");
  setStreamStatus("none", "no stream");
}

function startFallback() {
  if (fallbackTimer) return;
  setStreamStatus("error", "stream error, using snapshots");
  fallbackTimer = setInterval(() => {
    preview.src = `${FALLBACK_URL}?t=${Date.now()}`;
  }, 500);
}

function syncStream(state) {
  const previewOpen = !["idle", "error"].includes(state);
  if (previewOpen) startStream();
  else stopStream();
}

preview.addEventListener("load", () => {
  if (!streamActive) return;
  if (fallbackTimer) setStreamStatus("error", "stream blocked, snapshots");
  else setStreamStatus("streaming", "streaming");
});

preview.addEventListener("error", () => {
  if (streamActive && !fallbackTimer) startFallback();
});

function extractUfwFromError(error) {
  const match = /sudo ufw allow[^\n]*/.exec(error || "");
  return match ? match[0] : "";
}

function renderFirewall(bridge, fwCmd) {
  const blocked = bridge && bridge.enabled && bridge.ok === false && fwCmd && !firewallDismissed;
  if (!blocked) {
    firewallPanel.hidden = true;
    return;
  }
  firewallPanel.hidden = false;
  firewallHint.textContent =
    bridge.firewall_hint ||
    "HTTP control works but incoming GoPro UDP video is blocked. Allow it once with:";
  firewallCmd.textContent = fwCmd;
  lastFirewallCmd = fwCmd;
  firewallFirewalld.textContent = bridge.firewalld_command || "(unavailable)";
  firewallIptables.textContent = bridge.iptables_command || "(unavailable)";
  firewallRaw.textContent = JSON.stringify(bridge, null, 2);
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
  if (current && !status.guide?.complete) {
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
  if (guide?.complete) {
    return `Route complete (${guide.complete_count}/${guide.total_count}). Quality check below; `
      + "Resume to add or repeat poses, then Solve again.";
  }
  if (!guide?.current) return "Follow the guide line with the board center.";
  const current = guide.current;
  const base = `Route ${guide.complete_count}/${guide.total_count}: ${current.label}`;
  if (current.live_match) return `${base}. Hold still`;
  if ((current.skew || 0) > 0) {
    return `${base}. Move to the point, then TILT the board until the box turns green`;
  }
  return `${base}. Move board center to the highlighted point and match the box size`;
}

function num(value, digits = 2) {
  return value == null || Number.isNaN(value) ? "n/a" : Number(value).toFixed(digits);
}

function pickRecommended(results) {
  // Lower median reprojection error wins (rational_polynomial usually wins on
  // wide GoPro lenses); fall back to RMS.
  return results.slice().sort((a, b) => {
    const am = a.median_view_error_px ?? a.rms ?? 1e9;
    const bm = b.median_view_error_px ?? b.rms ?? 1e9;
    return am - bm;
  })[0];
}

function coverageReasons(cov) {
  const reasons = [];
  if (!cov) return reasons;
  if ((cov.overall ?? 0) < 0.8) {
    reasons.push(`Coverage ${Math.round((cov.overall || 0) * 100)}%, aim for 80% or more`);
  }
  if (cov.x && !(cov.x.low_hit && cov.x.high_hit)) reasons.push("Push the board to the left & right edges");
  if (cov.y && !(cov.y.low_hit && cov.y.high_hit)) reasons.push("Push the board to the top & bottom edges");
  if (cov.size && !(cov.size.low_hit && cov.size.high_hit)) {
    reasons.push("Add both near (large) and far (small) board views");
  }
  if (cov.skew && !cov.skew.hit) reasons.push("Add more tilted / skewed views");
  return reasons;
}

function modeSummary(mode) {
  if (!mode) return "";
  const parts = [];
  if (mode.lens_fov) parts.push(`lens ${mode.lens_fov}`);
  if (mode.webcam_resolution) parts.push(`res ${mode.webcam_resolution}`);
  if (mode.frame_size) parts.push(mode.frame_size);
  if (mode.fps) parts.push(`${mode.fps} fps`);
  if (mode.protocol) parts.push(mode.protocol);
  return parts.join(", ");
}

function renderResults(modelResults, coverage, mode) {
  if (!modelResults || !modelResults.length) {
    resultsPanel.hidden = true;
    return;
  }
  resultsPanel.hidden = false;
  const rec = pickRecommended(modelResults);
  const cm = rec.camera_matrix || [[null, null, null], [null, null, null]];
  const fx = cm[0]?.[0];
  const fy = cm[1]?.[1];
  const cx = cm[0]?.[2];
  const cy = cm[1]?.[2];
  const selected = rec.selected || rec;
  const allFrames = rec.all_frames || {};
  const used = selected.frame_count ?? allFrames.frame_count;
  const total = allFrames.frame_count ?? used;

  const reasons = coverageReasons(coverage);
  if (rec.worst_view_error_px != null && rec.worst_view_error_px > 2.5) {
    reasons.push(`Worst reprojection ${num(rec.worst_view_error_px)} px is high`);
  }
  const pass = reasons.length === 0;

  verdictBox.className = `verdict ${pass ? "pass" : "retake"}`;
  verdictBadge.textContent = pass ? "PASS" : "RETAKE";
  verdictText.textContent = pass
    ? "Coverage and reprojection look good."
    : "Consider another pass to improve:";
  verdictReasons.innerHTML = "";
  for (const reason of reasons) {
    const li = document.createElement("li");
    li.textContent = reason;
    verdictReasons.append(li);
  }

  const rows = [];
  const modeStr = modeSummary(mode);
  if (modeStr) rows.push(["Acquisition mode", `${modeStr} (record datasets in this exact mode)`]);
  rows.push(
    ["Recommended model", String(rec.model)],
    ["Reprojection error", `median ${num(rec.median_view_error_px)} px, worst ${num(rec.worst_view_error_px)} px`],
    ["RMS", `${num(rec.rms)} px`],
    ["Focal length", `fx ${num(fx, 1)}, fy ${num(fy, 1)}`],
    ["Principal point", `cx ${num(cx, 1)}, cy ${num(cy, 1)}`],
    ["Frames used", `${used ?? "n/a"} / ${total ?? "n/a"}`],
  );
  const other = modelResults.find((r) => r !== rec);
  if (other) {
    rows.push([
      `Alternate (${other.model})`,
      `median ${num(other.median_view_error_px)} px, worst ${num(other.worst_view_error_px)} px`,
    ]);
  }
  if (rec.yaml) rows.push(["Output YAML", rec.yaml]);

  resultGrid.innerHTML = "";
  for (const [key, value] of rows) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    resultGrid.append(dt, dd);
  }
  resultsRaw.textContent = JSON.stringify(modelResults, null, 2);
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
  const capturing = state === "capturing";
  const paused = state === "paused";
  const solving = state === "solving";
  const live = !["idle", "error"].includes(state);
  const done = state === "complete" || state === "solved";
  previewBtn.disabled = live;                 // open only when nothing is live
  stopBtn.disabled = !live;                    // stop only when something is live
  startRunBtn.disabled = capturing || solving; // start/restart a run otherwise
  nextCameraBtn.disabled = !live || solving;   // switch only from a live session
  // Resume re-enables capturing from paused or after the route completed/solved,
  // so the operator can add or repeat poses on the same run.
  pauseResumeBtn.disabled = !(capturing || paused || done);
  pauseResumeBtn.textContent = capturing ? "Pause" : "Resume";
  pauseResumeBtn.setAttribute("aria-pressed", String(paused));
  captureBtn.disabled = !capturing;
  solveBtn.disabled = captures < Math.max(3, Number(form.elements["solver.min_frames"].value || 25));

  // When the route first completes, run a checkpoint solve automatically so the
  // operator gets an early quality readout (and can then Resume to fix weak areas).
  if (state === "complete" && !autoSolved) {
    autoSolved = true;
    runSolve();
  }

  syncStream(state);

  const gopro = status.gopro;
  const bridge = status.video_bridge;
  const fwCmd = bridge ? bridge.ufw_command || extractUfwFromError(bridge.error) : "";
  renderFirewall(bridge, fwCmd);

  if (status.results && status.results.length) {
    renderResults(status.results, status.coverage, status.acquisition_mode);
    results.textContent = "";
  } else {
    resultsPanel.hidden = true;
    if (gopro && gopro.enabled && gopro.ok === false) {
      results.textContent = JSON.stringify(gopro, null, 2);
    } else if (bridge && bridge.enabled && bridge.ok === false && !fwCmd) {
      results.textContent = JSON.stringify(bridge, null, 2);
    } else {
      results.textContent = "";
    }
  }
}

async function poll() {
  try {
    updateStatus(await api("/api/session/status"));
  } catch {
    statusLine.textContent = "server not reachable (is it still running?)";
    setStreamStatus("error", "server offline");
  }
}

function startPolling() {
  clearInterval(pollTimer);
  // Poll fast so the guide overlay (target box, dots, pose dot, green match)
  // tracks the board closely. The video itself is a separate MJPEG stream; this
  // only fetches the lightweight status JSON.
  pollTimer = setInterval(poll, 150);
}

async function loadPresetList() {
  try {
    const data = await api("/api/presets");
    presetSelect.innerHTML = '<option value="">none</option>';
    for (const preset of data.presets || []) {
      const label = preset.title ? `${preset.title}` : preset.name;
      presetSelect.append(new Option(label, preset.name));
    }
  } catch {
    // Presets endpoint unavailable; leave the bar inert.
  }
}

async function applyPreset(name) {
  if (!name) return;
  try {
    const preset = await api(`/api/presets/${encodeURIComponent(name)}`);
    const cfg = preset.config || preset;
    populateForm(cfg, defaults.aruco_dictionaries, defaults.gopro_options);
    presetMsg.textContent = `Loaded preset "${preset.title || name}"`;
  } catch (err) {
    presetMsg.textContent = `Could not load preset: ${err}`;
  }
}

async function savePreset() {
  const name = prompt("Save current settings as preset name:");
  if (!name) return;
  try {
    await api(`/api/presets/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify({config: readForm()}),
    });
    presetMsg.textContent = `Saved preset "${name}"`;
    await loadPresetList();
    presetSelect.value = name;
  } catch (err) {
    presetMsg.textContent = `Could not save preset: ${err}`;
  }
}

previewBtn.addEventListener("click", async () => {
  firewallDismissed = false;
  autoSolved = false;
  startStream();
  updateStatus(await api("/api/session/preview", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
});

startRunBtn.addEventListener("click", async () => {
  firewallDismissed = false;
  autoSolved = false;
  results.textContent = "";
  resultsPanel.hidden = true;
  startStream();
  updateStatus(await api("/api/session/run", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
});

pauseResumeBtn.addEventListener("click", async () => {
  const action = pauseResumeBtn.textContent === "Resume" ? "resume" : "pause";
  updateStatus(await api(`/api/session/${action}`, {method: "POST"}));
});

stopBtn.addEventListener("click", async () => {
  // Tear the video down first so a final stream "load" cannot flip the dot back
  // to green while the request is in flight.
  stopStream();
  clearInterval(pollTimer);
  updateStatus(await api("/api/session/stop", {method: "POST"}));
});

captureBtn.addEventListener("click", async () => {
  updateStatus(await api("/api/session/capture", {method: "POST"}));
});

async function runSolve() {
  statusLine.textContent = "solving...";
  try {
    await api("/api/session/solve", {method: "POST"});
  } catch (err) {
    statusLine.textContent = `solve failed: ${err}`;
    return;
  }
  await poll(); // status now carries results -> updateStatus renders the panel
}

solveBtn.addEventListener("click", runSolve);

nextCameraBtn.addEventListener("click", async () => {
  // Cleanly stop the current camera and go idle; the operator swaps the camera,
  // edits the camera name if needed, then clicks Open Preview for the new one.
  firewallDismissed = false;
  autoSolved = false;
  results.textContent = "";
  resultsPanel.hidden = true;
  // Tear the video down first (before awaiting) so the dot does not flip back to
  // green on the stream's final load while the camera is being stopped.
  stopStream();
  clearInterval(pollTimer);
  updateStatus(await api("/api/session/next-camera", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
});

presetLoad.addEventListener("click", () => applyPreset(presetSelect.value));
presetSave.addEventListener("click", savePreset);

form.elements["gopro.enabled"].addEventListener("change", syncGoproVisibility);

firewallCopy.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(lastFirewallCmd);
    firewallCopyResult.textContent = "Copied";
  } catch {
    const range = document.createRange();
    range.selectNodeContents(firewallCmd);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    firewallCopyResult.textContent = "Press Ctrl+C to copy";
  }
});

firewallDismiss.addEventListener("click", () => {
  firewallDismissed = true;
  firewallPanel.hidden = true;
});

async function init() {
  defaults = await api("/api/defaults");
  populateForm(defaults.config, defaults.aruco_dictionaries, defaults.gopro_options);
  await loadPresetList();
  statusLine.textContent = "Ready";
  drawScatter([]);
  await poll();
}

init().catch((err) => {
  statusLine.textContent = String(err);
});
