const form = document.getElementById("configForm");
const previewBtn = document.getElementById("previewBtn");
const startRunBtn = document.getElementById("startRunBtn");
const stopBtn = document.getElementById("stopBtn");
const pauseResumeBtn = document.getElementById("pauseResumeBtn");
const nextCameraBtn = document.getElementById("nextCameraBtn");
const captureBtn = document.getElementById("captureBtn");
const solveBtn = document.getElementById("solveBtn");
const statusMain = document.getElementById("statusMain");
const statusDetail = document.getElementById("statusDetail");
const preview = document.getElementById("preview");
const previewEmpty = document.getElementById("previewEmpty");
const results = document.getElementById("results");
const resultsPanel = document.getElementById("resultsPanel");
const verdictBox = document.getElementById("verdict");
const verdictBadge = document.getElementById("verdictBadge");
const verdictText = document.getElementById("verdictText");
const verdictDetail = document.getElementById("verdictDetail");
const verdictReasons = document.getElementById("verdictReasons");
const recFigures = document.getElementById("recFigures");
const resultGrid = document.getElementById("resultGrid");
const filesGrid = document.getElementById("filesGrid");
const filesTitle = document.getElementById("filesTitle");
const resultsRaw = document.getElementById("resultsRaw");
const scatter = document.getElementById("scatter");
const guideOverlay = document.getElementById("guideOverlay");
const guidePrompt = document.getElementById("guidePrompt");
const guideLegend = document.getElementById("guideLegend");
const solveDesc = document.getElementById("solveDesc");
const streamStatus = document.getElementById("streamStatus");
const streamStatusText = document.getElementById("streamStatusText");
const cameraReadout = document.getElementById("cameraReadout");
const goproControls = document.getElementById("goproControls");
const deviceDetails = document.getElementById("deviceDetails");
const boardSummary = document.getElementById("boardSummary");
const steps = {
  setup: document.getElementById("stepSetup"),
  connect: document.getElementById("stepConnect"),
  capture: document.getElementById("stepCapture"),
  solve: document.getElementById("stepSolve"),
};

const presetSelect = document.getElementById("presetSelect");
const presetCustom = document.getElementById("presetCustom");
const presetSave = document.getElementById("presetSave");
const presetReset = document.getElementById("presetReset");
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

// Canvas colours, mirroring the CSS tokens in style.css.
const COLOR = {
  field: "#0b0c0e",
  grid: "rgba(155, 154, 149, 0.14)",
  frame: "#454a54",
  signal: "#e9a23b",
  signalSoft: "rgba(233, 162, 59, 0.45)",
  ink: "rgba(232, 230, 225, 0.8)",
  inkFill: "rgba(232, 230, 225, 0.1)",
  muted: "rgba(155, 154, 149, 0.8)",
  pass: "#7fbf8e",
  fail: "#e06a5a",
  target: "#6f9fd8",
};

// The status line: one plain sentence per session state. The server's own
// message follows it as a quieter detail.
function stateSentence(state, captures) {
  const views = `${captures} view${captures === 1 ? "" : "s"} saved`;
  switch (state) {
    case "idle": return "Not connected.";
    case "preview": return "Connected. Start a new run when the board is ready.";
    case "capturing": return `Capturing: ${views}.`;
    case "paused": return `Paused: ${views}.`;
    case "complete": return `Capture finished: ${views}.`;
    case "solving": return "Solving… this can take a minute.";
    case "solved": return "Calibration done.";
    default: return `${state}.`;
  }
}

// Server messages that only repeat the sentence in front of them.
const QUIET_MESSAGES = new Set(["no active preview", "preview closed"]);

function setStatusLine(main, detail = "") {
  if (statusMain.textContent === main && statusDetail.textContent === detail) return;
  statusMain.textContent = main;
  statusDetail.textContent = detail;
}

let defaults = null;
// The config the form edits: the server defaults, or the last loaded preset. Keys
// with no form field (for example gopro.webcam_digital_lens) pass through from it.
let baseConfig = null;
let pollTimer = null;
let streamActive = false;
let fallbackTimer = null;
let firewallDismissed = false;
let lastFirewallCmd = "";
let autoSolved = false;
let currentState = "idle";
let currentLive = false;
let lastStatus = null;
// Shipped and saved camera setups, name -> config, so the dropdown can show which
// one the form holds. presetChoice is the name shown ("" = custom settings).
let presetConfigs = {};
let presetChoice = "";
// The empty-preview copy, restored once an error message has replaced it.
const previewEmptyHTML = previewEmpty.innerHTML;

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

// The backend solver.models is a list; the form exposes it as one dropdown.
const MODEL_SETS = {
  pinhole: ["plumb_bob", "rational_polynomial"],
  fisheye: ["fisheye"],
  fisheye_ds: ["double_sphere", "kannala_brandt", "fisheye"],
  double_sphere: ["double_sphere"],
  both: ["plumb_bob", "rational_polynomial", "fisheye"],
};

// The smallest set that holds every requested model, so an older preset
// (for example [fisheye, double_sphere]) lands on the set that grew from it.
function modelsToKey(models) {
  const want = [...(models || [])];
  if (!want.length) return "pinhole";
  let best = null;
  for (const [key, set] of Object.entries(MODEL_SETS)) {
    if (!want.every((model) => set.includes(model))) continue;
    if (best === null || set.length < MODEL_SETS[best].length) best = key;
  }
  return best || "pinhole";
}

// What each model is called on the page.
const MODEL_NAME = {
  double_sphere: "Double Sphere",
  kannala_brandt: "Kannala–Brandt for UMI",
  fisheye: "OpenCV fisheye",
  plumb_bob: "Pinhole (plumb_bob)",
  rational_polynomial: "Pinhole (rational)",
};

function modelName(model) {
  return MODEL_NAME[model] || model || "model";
}

// Solved by the OpenICC backend: one aggregate error, no per-view errors.
const OPENICC_MODELS = new Set(["double_sphere", "kannala_brandt"]);

function setFormValue(name, value) {
  const input = form.elements[name];
  if (!input) return;
  if (name === "solver.models") {
    input.value = modelsToKey(value);
    return;
  }
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
  updateBoardSummary();
}

function readForm() {
  const config = structuredClone(baseConfig || defaults.config);
  for (const input of form.elements) {
    if (!input.name) continue;
    if (input.name === "solver.models") {
      setDeep(config, input.name, MODEL_SETS[input.value] || MODEL_SETS.pinhole);
      continue;
    }
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
  if (!enabled) return;
  if (goproControls) goproControls.hidden = !enabled.checked;
  for (const id of ["connectionDetails", "recordingDetails"]) {
    const el = document.getElementById(id);
    if (el) el.hidden = !enabled.checked; // GoPro-only settings
  }
  // Device and pixel format matter only for a plain camera: show them then.
  if (deviceDetails && !enabled.checked) deviceDetails.open = true;
}

function updateBoardSummary() {
  const value = (name) => form.elements[name]?.value;
  const dict = (value("board.aruco_dict") || "").replace(/^DICT_/, "");
  boardSummary.textContent =
    `${value("board.cols")}×${value("board.rows")} · ${dict} · `
    + `${Number(value("board.square_m"))}/${Number(value("board.marker_m"))} mm`;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...options,
  });
  if (!response.ok) {
    const text = await response.text();
    let message = text || response.statusText;
    try {
      const detail = JSON.parse(text).detail; // FastAPI's HTTPException body
      if (detail) message = typeof detail === "string" ? detail : JSON.stringify(detail);
    } catch {
      // not JSON: keep the raw text
    }
    throw new Error(message);
  }
  return response.json();
}

function percent(value) {
  return `${Math.round((value || 0) * 100)} %`;
}

function num(value, digits = 2) {
  return value == null || Number.isNaN(value) ? "n/a" : Number(value).toFixed(digits);
}

// ---- Stream ----

// Whether the camera is streaming. The state names describe the run, and a run
// can be solved after Stop, so the server reports this separately.
function isLive(status) {
  const state = status.state || "idle";
  if (state === "error") return false;
  return status.preview_open ?? state !== "idle";
}

function setStreamStatus(state, text) {
  // Both this and the readout are live regions: touch them only on a change,
  // or a screen reader re-announces them on every poll.
  if (streamStatus.dataset.state === state && streamStatusText.textContent === text) return;
  streamStatus.dataset.state = state;
  streamStatusText.textContent = text;
}

function startStream() {
  if (streamActive) return;
  streamActive = true;
  clearInterval(fallbackTimer);
  fallbackTimer = null;
  setStreamStatus("connecting", "connecting…");
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

function syncStream(live) {
  if (live) startStream();
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

// ---- Firewall ----

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
  // role="alert": rewriting it on every poll would make screen readers repeat it.
  const signature = JSON.stringify(bridge) + fwCmd;
  if (!firewallPanel.hidden && firewallPanel.dataset.signature === signature) return;
  firewallPanel.dataset.signature = signature;
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

// ---- Camera readout ----

const READOUT_KEYS = {
  webcam_digital_lens: "Lens",
  max_lens_mod: "Mod",
  hypersmooth: "HyperSmooth",
};

function chip(text, {key = "", kind = ""} = {}) {
  const el = document.createElement("span");
  el.className = `chip${kind ? ` chip-${kind}` : ""}`;
  if (key) {
    const k = document.createElement("span");
    k.className = "chip-key";
    k.textContent = key;
    el.append(k);
  }
  el.append(document.createTextNode(text));
  return el;
}

// What the camera itself reported after the webcam started (read back, never
// assumed), plus any mismatch with what was requested. Only a live session has
// a report worth showing; otherwise say when one will appear.
function renderReadout(gopro, live) {
  const chips = [];
  const autoSetup = form.elements["gopro.enabled"]?.checked
    && form.elements["gopro.apply_on_preview"]?.checked;
  const labels = gopro?.camera_state?.labels;
  if (gopro?.enabled && gopro.ok === false) {
    chips.push(chip("GoPro setup failed: details at the bottom of the page", {kind: "warn"}));
  } else if (live && labels && Object.keys(labels).length) {
    for (const [field, label] of Object.entries(labels)) {
      chips.push(chip(label, {key: READOUT_KEYS[field] || field}));
    }
  } else if (live && gopro?.enabled) {
    chips.push(chip("camera did not report its settings", {kind: "muted"}));
  } else if (!live && autoSetup) {
    chips.push(chip("camera settings appear after Open preview", {kind: "muted"}));
  }
  if (live) {
    for (const warning of gopro?.warnings || []) {
      chips.push(chip(warning, {key: "Check", kind: "warn"}));
    }
  }
  const signature = chips.map((el) => el.textContent).join("|");
  if (cameraReadout.dataset.signature === signature) return;
  cameraReadout.dataset.signature = signature;
  cameraReadout.replaceChildren(...chips);
}

// ---- Steps ----

function stepStates({state, live, captures, minFrames, hasResults, presetChosen}) {
  const captured = ["complete", "solving", "solved"].includes(state);
  // Stopped with enough views: Solve is the next step, not reconnecting.
  const solvable = !live && !captured && !hasResults && state !== "error" && captures >= minFrames;
  const started = live || captures > 0 || hasResults;
  // Custom settings are allowed, but a newcomer is pointed at step 1 first.
  const setup = presetChosen || started ? "done" : "current";
  let connect = live ? "done" : solvable ? "pending" : "current";
  if (setup === "current") connect = "pending";
  let capture = "pending";
  if (captured) capture = "done";
  else if (live) capture = "current";
  let solve = "pending";
  if (state === "solved" || (hasResults && !live)) solve = "done";
  else if (state === "solving" || state === "complete" || solvable) solve = "current";
  return {setup, connect, capture, solve};
}

// The one obvious next action (the amber button), or null while the operator
// is busy moving the board or waiting for a solve.
function primaryButton({state, live, captures, minFrames, hasResults}) {
  if (state === "capturing" || state === "solving") return null;
  if (!live) {
    const solvable = state !== "error" && !hasResults && captures >= minFrames;
    return solvable ? solveBtn : previewBtn;
  }
  if (state === "preview") return startRunBtn;
  if (state === "paused") return captures >= minFrames ? solveBtn : pauseResumeBtn;
  if (state === "complete") return solveBtn;
  if (state === "solved") return nextCameraBtn;
  return null;
}

// Enable or disable a button, with a title that says what it does or why it
// cannot be used right now.
function setButton(button, enabled, title, whyNot) {
  button.disabled = !enabled;
  const text = enabled ? title : whyNot;
  if (button.title !== text) button.title = text;
}

const STEP_NOTE = {current: ", current step", done: ", done", pending: ""};

function renderSteps(states) {
  for (const [name, value] of Object.entries(states)) {
    const step = steps[name];
    step.dataset.stepState = value;
    if (value === "current") step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
    step.querySelector("[data-step-note]").textContent = STEP_NOTE[value] || "";
  }
}

// ---- Canvases ----

function fitCanvas(canvas) {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const cssW = Math.max(1, rect.width);
  const cssH = Math.max(1, rect.height);
  const pixelW = Math.round(cssW * dpr);
  const pixelH = Math.round(cssH * dpr);
  if (canvas.width !== pixelW || canvas.height !== pixelH) {
    canvas.width = pixelW;
    canvas.height = pixelH;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  return {ctx, cssW, cssH};
}

function updateBars(coverage) {
  document.getElementById("barX").value = coverage?.x?.progress || 0;
  document.getElementById("barY").value = coverage?.y?.progress || 0;
  document.getElementById("barSize").value = coverage?.size?.progress || 0;
  document.getElementById("barSkew").value = coverage?.skew?.progress || 0;
}

// The coverage map is the image field itself (same aspect as the stream).
// Each captured view is drawn as its board footprint, so it is obvious whether
// the board reached the edges, which is what decides wide-lens model quality.
const DEFAULT_TARGETS = {x_min: 0.2, x_max: 0.8, y_min: 0.2, y_max: 0.8};

// The box the board centre must reach, from the run's coverage targets.
function targetBox(coverage) {
  const t = {...DEFAULT_TARGETS, ...(coverage?.targets || {})};
  return {x: t.x_min, y: t.y_min, w: t.x_max - t.x_min, h: t.y_max - t.y_min};
}

function drawCoverageMap(points, imageSize, target = targetBox(null)) {
  const {ctx, cssW, cssH} = fitCanvas(scatter);
  const [natW, natH] = imageSize?.length === 2 ? imageSize : [16, 9];
  const scale = Math.min(cssW / natW, cssH / natH);
  const w = natW * scale;
  const h = natH * scale;
  const ox = (cssW - w) / 2;
  const oy = (cssH - h) / 2;
  const px = (x) => ox + x * w;
  const py = (y) => oy + y * h;

  ctx.fillStyle = COLOR.field;
  ctx.fillRect(ox, oy, w, h);
  ctx.strokeStyle = COLOR.grid;
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let i = 1; i < 10; i++) {
    ctx.moveTo(px(i / 10), oy);
    ctx.lineTo(px(i / 10), oy + h);
    ctx.moveTo(ox, py(i / 10));
    ctx.lineTo(ox + w, py(i / 10));
  }
  ctx.stroke();
  ctx.strokeStyle = COLOR.frame;
  ctx.strokeRect(ox + 0.5, oy + 0.5, w - 1, h - 1);

  // The spread the coverage targets ask for.
  ctx.setLineDash([6, 5]);
  ctx.strokeStyle = COLOR.signalSoft;
  ctx.lineWidth = 1.5;
  ctx.strokeRect(px(target.x), py(target.y), target.w * w, target.h * h);
  ctx.setLineDash([]);

  function footprint(point) {
    const size = Math.max(point.size || 0, 0.03);
    return [px(point.x) - (size * w) / 2, py(point.y) - (size * h) / 2, size * w, size * h];
  }

  for (const point of points || []) {
    const box = footprint(point);
    ctx.fillStyle = COLOR.inkFill;
    ctx.fillRect(...box);
    ctx.strokeStyle = COLOR.ink;
    ctx.lineWidth = 1;
    ctx.strokeRect(...box);
  }
}

function drawGuideOverlay(status) {
  const {ctx, cssW, cssH} = fitCanvas(guideOverlay);
  const checkpoints = status.guide?.checkpoints || [];
  // Only over a live picture: on the empty preview it would cover the how-to.
  const draw = isLive(status) && checkpoints.length > 0;
  guideLegend.hidden = !draw || Boolean(status.guide?.complete);
  if (!draw) return;

  const naturalW = preview.naturalWidth || status.image_size?.[0] || cssW;
  const naturalH = preview.naturalHeight || status.image_size?.[1] || cssH;
  const scale = Math.min(cssW / naturalW, cssH / naturalH);
  const imageW = naturalW * scale;
  const imageH = naturalH * scale;
  const offsetX = (cssW - imageW) / 2;
  const offsetY = (cssH - imageH) / 2;
  const px = (x) => offsetX + x * imageW;
  const py = (y) => offsetY + y * imageH;

  function dot(point, color, radius) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(px(point.x), py(point.y), radius, 0, Math.PI * 2);
    ctx.fill();
  }

  ctx.save();
  ctx.strokeStyle = COLOR.signal;
  ctx.globalAlpha = 0.9;
  ctx.lineWidth = 3;
  const target = targetBox(status.coverage);
  const midX = target.x + target.w / 2;
  const midY = target.y + target.h / 2;
  ctx.setLineDash([14, 10]);
  ctx.strokeRect(px(target.x), py(target.y), imageW * target.w, imageH * target.h);
  ctx.setLineDash([]);
  ctx.globalAlpha = 0.35;
  ctx.lineWidth = 2;
  ctx.beginPath();
  ctx.moveTo(px(target.x), py(midY));
  ctx.lineTo(px(target.x + target.w), py(midY));
  ctx.moveTo(px(midX), py(target.y));
  ctx.lineTo(px(midX), py(target.y + target.h));
  ctx.stroke();
  ctx.restore();

  for (const point of checkpoints) {
    const radius = point.current ? 12 : 7;
    const color = point.complete ? COLOR.pass : point.current ? COLOR.signal : COLOR.target;
    dot(point, color, radius);
  }

  const current = status.guide?.current;
  if (current && !status.guide?.complete) {
    const boxW = Math.max(80, current.size * imageW);
    const boxH = Math.max(50, current.size * imageH);
    ctx.strokeStyle = current.live_match ? COLOR.pass : COLOR.signal;
    ctx.lineWidth = 4;
    ctx.strokeRect(px(current.x) - boxW / 2, py(current.y) - boxH / 2, boxW, boxH);
  }
  const pose = status.pose;
  if (pose) dot(pose, COLOR.fail, 9);
}

const IDLE_PROMPT = "Open preview to start.";

function guideText(status, live, minFrames) {
  const guide = status.guide || {};
  const state = status.state || "idle";
  const done = guide.complete_count || 0;
  const total = guide.total_count || 0;
  if (state === "solving") return "Solving…";
  if (state === "solved") {
    return live
      ? "Solved. Check the result below; Resume to add views where the coverage map has gaps, then Solve again."
      : "Solved. Check the result below. Open preview to calibrate again.";
  }
  if (state === "error") return "Fix the problem on the status line, then Open preview again.";
  if (!live) {
    const captures = status.captures || 0;
    if (!captures || !status.run_id) return IDLE_PROMPT;
    return captures >= minFrames
      ? `Preview stopped. Solve this run's ${captures} views, or Open preview to start again.`
      : `Preview stopped with ${captures} views; solving needs ${minFrames}. Open preview and start a new run.`;
  }
  if (state === "preview") return "Hold the board in front of the camera, then click Start new run.";
  if (state === "complete") {
    if (guide.complete) {
      return `Route complete (${done}/${total}). Check the result below; `
        + "Resume to add or repeat poses, then Solve again.";
    }
    return `Frame cap reached (${status.captures} views) with the route at ${done}/${total}. `
      + "Solve with these views, or raise Frame cap under Capture and solver and start a new run.";
  }
  const paused = state === "paused";
  if (guide.complete) {
    // Resumed after the route was done: free capture of extra views.
    return paused
      ? "Paused. Resume to add more views, then Solve again."
      : "Adding extra views: fill the gaps in the coverage map, then Solve again.";
  }
  const current = guide.current;
  if (!current) return paused ? "Paused. Resume to keep capturing." : "Move the board centre along the guide.";
  const base = `target ${done + 1}/${total}, ${current.label}`;
  if (paused) return `Paused at ${base}. Resume to keep capturing.`;
  const Base = `Target ${done + 1}/${total}: ${current.label}`;
  if (current.live_match) return `${Base}. Hold still.`;
  if ((current.skew || 0) > 0) {
    return `${Base}. Put the board centre on the orange dot, then tilt the board until the box turns green.`;
  }
  return `${Base}. Put the board centre on the orange dot and match the size of the orange box.`;
}

// ---- Results ----

function pickRecommended(results) {
  const solved = results.filter(
    (result) => result && result.ok !== false && (result.median_view_error_px != null || result.rms != null),
  );
  if (!solved.length) return null;
  // The solver marks the recommendation (double_sphere when it solved: the cv2
  // medians are biased by dropping edge views). Older summaries lack the flag:
  // lower median reprojection error wins, falling back to RMS.
  const marked = solved.find((result) => result.recommended === true);
  if (marked) return marked;
  return solved.slice().sort((a, b) => {
    const am = a.median_view_error_px ?? a.rms ?? 1e9;
    const bm = b.median_view_error_px ?? b.rms ?? 1e9;
    return am - bm;
  })[0];
}

function coverageReasons(cov) {
  const reasons = [];
  if (!cov) return reasons;
  if ((cov.overall ?? 0) < 0.8) {
    reasons.push(`Coverage is ${Math.round((cov.overall || 0) * 100)}%; aim for 80% or more.`);
  }
  if (cov.x && !(cov.x.low_hit && cov.x.high_hit)) reasons.push("Push the board to the left and right edges.");
  if (cov.y && !(cov.y.low_hit && cov.y.high_hit)) reasons.push("Push the board to the top and bottom edges.");
  if (cov.size && !(cov.size.low_hit && cov.size.high_hit)) {
    reasons.push("Add both near (large) and far (small) board views.");
  }
  if (cov.skew && !cov.skew.hit) reasons.push("Add more tilted views.");
  return reasons;
}

function modeSummary(mode) {
  if (!mode) return "";
  const parts = [];
  // What the camera reported beats what was requested.
  const lens = mode.reported_webcam_digital_lens || mode.lens_fov;
  const mod = mode.reported_max_lens_mod || mode.max_lens_mod;
  if (lens) parts.push(`lens ${lens}`);
  if (mod) parts.push(`mod ${mod}`);
  if (mode.webcam_resolution) parts.push(`res ${mode.webcam_resolution}`);
  if (mode.frame_size) parts.push(mode.frame_size);
  if (mode.fps) parts.push(`${mode.fps} fps`);
  if (mode.protocol) parts.push(`${mode.protocol} stream`);
  return parts.join(", ");
}

function figure(key, value, primary = false) {
  const el = document.createElement("div");
  el.className = `figure${primary ? " figure-primary" : ""}`;
  const k = document.createElement("span");
  k.className = "label";
  k.textContent = key;
  const v = document.createElement("span");
  v.className = "figure-value";
  v.textContent = value;
  el.append(k, v);
  return el;
}

// One row of a result list. `notes` are extra lines under the value (warnings,
// or what a file is for), each with its own class.
function gridRow(grid, key, value, {cls = "", notes = []} = {}) {
  const dt = document.createElement("dt");
  dt.textContent = key;
  const dd = document.createElement("dd");
  if (cls) dd.className = cls;
  const main = document.createElement("span");
  main.className = "row-value";
  main.textContent = value;
  dd.append(main);
  for (const note of notes) {
    const line = document.createElement("span");
    line.className = note.cls;
    line.textContent = note.text;
    dd.append(line);
  }
  grid.append(dt, dd);
}

// Plain-language warnings a model row carries: the solver's own, plus the UMI
// check on the Kannala–Brandt row.
function resultWarnings(result) {
  const all = [...(result.warnings || []), ...(result.umi_check?.warnings || [])];
  return [...new Set(all)];
}

function warningNotes(result) {
  return resultWarnings(result).map((text) => ({cls: "row-warning", text}));
}

function umiSummary(result) {
  const check = result.umi_check || {};
  const diff = check.max_diff_vs_double_sphere_px;
  const reach = check.board_reach_deg;
  const fit = `Reprojection RMS ${num(result.rms)} px (OpenICC).`;
  if (diff != null && reach != null) {
    return `Kannala–Brandt for UMI: matches Double Sphere within ${num(diff)} px `
      + `out to ${Math.round(reach)}°. ${fit}`;
  }
  return `Kannala–Brandt for UMI: solved. ${fit} No Double Sphere solve to compare it with.`;
}

// Every file a solved model wrote, with what it is for.
function fileRows(result) {
  const rows = [];
  const name = modelName(result.model);
  if (result.json && result.model === "kannala_brandt") {
    rows.push(["UMI intrinsics JSON", result.json, "Use this for: UMI. Load it with parse_fisheye_intrinsics."]);
  } else if (result.json && result.model === "double_sphere") {
    rows.push(["Double Sphere JSON", result.json, "Use this for: tools that take the Double Sphere model (the reference fit for this lens)."]);
  } else if (result.json) {
    rows.push([`${name} JSON`, result.json, "Use this for: tools that read OpenICC's JSON."]);
  }
  if (result.orbslam3_yaml) {
    rows.push([
      "ORB-SLAM3 camera YAML",
      result.orbslam3_yaml,
      "Use this for: the camera block of ORB-SLAM3 settings. Merge in the IMU block from your existing settings file.",
    ]);
  }
  if (result.yaml) {
    rows.push([`ROS camera_info YAML (${name})`, result.yaml, "Use this for: ROS, as the camera_info file."]);
  }
  return rows;
}

function renderResults(modelResults, coverage, mode, live) {
  if (!modelResults || !modelResults.length) {
    resultsPanel.hidden = true;
    return;
  }
  resultsPanel.hidden = false;
  resultGrid.replaceChildren();
  filesGrid.replaceChildren();
  recFigures.replaceChildren();
  verdictReasons.replaceChildren();
  resultsRaw.textContent = JSON.stringify(modelResults, null, 2);
  const solvedOk = (r) => r && r.ok !== false && (r.median_view_error_px != null || r.rms != null);
  const failed = modelResults.filter((r) => !solvedOk(r));
  const rec = pickRecommended(modelResults);
  const failedRows = () => {
    for (const result of failed) {
      gridRow(resultGrid, `${modelName(result.model)} failed`, result.error || result.error_type || "solver failed", {cls: "failed"});
    }
  };

  if (!rec) {
    verdictBox.className = "verdict retake";
    verdictBadge.textContent = "FAILED";
    verdictText.textContent = "No calibration model solved. The reasons are below.";
    verdictDetail.textContent = "Fix the first reason, then Solve again.";
    failedRows();
    filesTitle.hidden = true;
    return;
  }

  const isDS = rec.model === "double_sphere";
  const isOpenICC = OPENICC_MODELS.has(rec.model);
  const cm = rec.camera_matrix || [[null, null, null], [null, null, null]];
  const selected = rec.selected || rec;
  const allFrames = rec.all_frames || {};
  const used = selected.frame_count ?? allFrames.frame_count;
  const total = allFrames.frame_count ?? used;

  const reasons = coverageReasons(coverage);
  if (rec.worst_view_error_px != null && rec.worst_view_error_px > 2.5) {
    reasons.push(`The worst view is off by ${num(rec.worst_view_error_px)} px.`);
  }
  const alpha = isDS ? rec.distortion?.[1] : null;
  if (alpha != null && alpha >= 0.98) {
    reasons.push(`alpha ${num(alpha, 3)} is at its limit: the board missed the edge of the lens circle. Add edge views.`);
  }
  reasons.push(...resultWarnings(rec));
  const pass = reasons.length === 0;
  verdictBox.className = `verdict ${pass ? "pass" : "retake"}`;
  verdictBadge.textContent = pass ? "PASS" : "RETAKE";
  const next = live
    ? "Click Next camera to do another camera."
    : "To do another camera, plug it in and click Open preview.";
  verdictText.textContent = pass
    ? `Calibration passed. The files are listed below. ${next}`
    : "Retake: fix what is listed below, then Solve again.";
  // Say only what was checked: coverage, plus worst view (cv2) or alpha (Double Sphere).
  const checked = isDS
    ? " and alpha is clear of its limit"
    : isOpenICC ? "" : " and no kept view is off by more than 2.5 px";
  verdictDetail.textContent = pass
    ? `Recommended model: ${modelName(rec.model)}. Coverage targets met${checked}.`
    : `The recommended model, ${modelName(rec.model)}, solved and wrote its files, but:`;
  for (const reason of reasons) {
    const li = document.createElement("li");
    li.textContent = reason;
    verdictReasons.append(li);
  }

  recFigures.append(
    figure(isOpenICC ? "Reprojection RMS (OpenICC)" : "Median error", `${num(isOpenICC ? rec.rms : rec.median_view_error_px)} px`, true),
  );
  if (!isOpenICC) {
    recFigures.append(
      figure("Worst view", `${num(rec.worst_view_error_px)} px`),
      figure("RMS", `${num(rec.rms)} px`),
    );
  }
  if (isDS) {
    recFigures.append(figure("xi", num(rec.distortion?.[0], 4)), figure("alpha", num(alpha, 3)));
  }
  recFigures.append(
    figure("Focal", `${num(cm[0]?.[0], 1)}${isDS ? "" : ` / ${num(cm[1]?.[1], 1)}`}`),
    figure("Centre (cx, cy)", `${num(cm[0]?.[2], 1)}, ${num(cm[1]?.[2], 1)}`),
    figure("Frames used", `${used ?? "n/a"} / ${total ?? "n/a"}`),
  );

  const modeStr = modeSummary(mode);
  if (modeStr) gridRow(resultGrid, "Captured in", `${modeStr}. Record your data in exactly this mode.`);
  const solved = modelResults.filter(solvedOk);
  for (const result of solved) {
    // The recommended row's warnings are already in the verdict above.
    const notes = result === rec ? [] : warningNotes(result);
    if (result.model === "kannala_brandt") {
      gridRow(resultGrid, "For UMI", umiSummary(result), {notes});
    } else if (result !== rec) {
      const error = OPENICC_MODELS.has(result.model)
        ? `Reprojection RMS ${num(result.rms)} px (OpenICC)`
        : `median ${num(result.median_view_error_px)} px, worst ${num(result.worst_view_error_px)} px`;
      gridRow(resultGrid, `Also solved: ${modelName(result.model)}`, error, {notes});
    }
  }
  failedRows();
  if (isDS) gridRow(resultGrid, "Note", "Compare Double Sphere models by projecting rays, not by focal length: f, xi and alpha trade off.");

  // Files: the recommended model's first, then the others in solve order.
  const files = [rec, ...solved.filter((r) => r !== rec)].flatMap(fileRows);
  for (const [label, path, use] of files) {
    gridRow(filesGrid, label, path, {cls: "file", notes: [{cls: "row-use", text: use}]});
  }
  filesTitle.hidden = files.length === 0;
}

// ---- Status ----

function renderDiagnostics(status, fwCmd) {
  const gopro = status.gopro;
  const bridge = status.video_bridge;
  let text = "";
  if (gopro && gopro.enabled && gopro.ok === false) text = JSON.stringify(gopro, null, 2);
  else if (bridge && bridge.enabled && bridge.ok === false && !fwCmd) text = JSON.stringify(bridge, null, 2);
  results.textContent = text;
  results.hidden = !text;
}

function updateStatus(status) {
  lastStatus = status;
  const state = status.state || "idle";
  const captures = status.captures || 0;
  const message = status.message || "";
  if (state === "error") {
    setStatusLine(`Problem: ${message || "the camera could not be opened."}`);
  } else {
    setStatusLine(stateSentence(state, captures), QUIET_MESSAGES.has(message) ? "" : message);
  }
  const target = status.target_samples || 0;
  const minFrames = Math.max(3, Number(form.elements["solver.min_frames"].value || 25));
  document.getElementById("captureCount").textContent = captures;
  document.getElementById("captureAim").textContent = `· aim ${target}`;
  document.getElementById("markerCount").textContent = status.markers || 0;
  document.getElementById("coverageOverall").textContent = percent(status.coverage?.overall);
  document.getElementById("guideCount").textContent =
    `${status.guide?.complete_count || 0} / ${status.guide?.total_count || 0}`;
  const capturing = state === "capturing";
  const paused = state === "paused";
  const solving = state === "solving";
  const live = isLive(status);
  guidePrompt.textContent = guideText(status, live, minFrames);
  // Before anything has started, the how-to on the empty preview says it all.
  guidePrompt.hidden = guidePrompt.textContent === IDLE_PROMPT;
  updateBars(status.coverage || {});
  drawCoverageMap(status.coverage?.points || [], status.image_size, targetBox(status.coverage));
  drawGuideOverlay(status);
  renderReadout(status.gopro, live);
  const hasResults = Boolean(status.results && status.results.length);
  const facts = {state, live, captures, minFrames, hasResults, presetChosen: Boolean(presetChoice)};
  renderSteps(stepStates(facts));

  currentState = state;
  currentLive = live;
  const done = state === "complete" || state === "solved";
  // At the frame cap, Resume would stop again on the next frame.
  const atCap = status.max_samples != null && captures >= status.max_samples;
  const waitSolve = "Wait for the solve to finish";
  // Open only when nothing is live; stop only when something is.
  setButton(previewBtn, !live, "Start the GoPro webcam and show its picture", "The preview is already open");
  setButton(stopBtn, live, "Stop the webcam stream", "The preview is not open");
  // Start or restart a run whenever it is not capturing or solving.
  setButton(
    startRunBtn, !(capturing || solving), "Start saving views into a new run folder",
    solving ? waitSolve : "A run is capturing. Pause it first to start over",
  );
  // Switch only from a live session.
  setButton(
    nextCameraBtn, live && !solving,
    "End this run and stop the webcam, so you can plug in the next camera",
    solving ? waitSolve : "Only needed while the preview is open. Otherwise plug in the next camera and click Open preview",
  );
  // Resume re-enables capturing from paused or after the route completed/solved,
  // so the operator can add or repeat poses on the same run.
  const resumeLabel = paused || done;
  pauseResumeBtn.textContent = resumeLabel ? "Resume" : "Pause";
  let pauseWhy = "Start a new run first";
  if (!live) pauseWhy = "Open the preview first";
  else if (solving) pauseWhy = waitSolve;
  else if (resumeLabel && atCap) pauseWhy = `The run has reached its frame cap (${status.max_samples} views)`;
  setButton(
    pauseResumeBtn, live && (capturing || paused || done) && !(resumeLabel && atCap),
    resumeLabel ? "Carry on saving views into this run" : "Stop saving views for now; the preview keeps running",
    pauseWhy,
  );
  setButton(
    captureBtn, capturing, "Save the current view immediately (views also save automatically)",
    paused ? "Resume first" : "Start a new run first",
  );
  setButton(
    solveBtn, captures >= minFrames && !solving, "Compute the lens calibration from the saved views",
    solving ? "Solving now…" : `Needs at least ${minFrames} views (${captures} saved so far)`,
  );
  const primary = primaryButton(facts);
  for (const button of [previewBtn, stopBtn, startRunBtn, captureBtn, pauseResumeBtn, solveBtn, nextCameraBtn]) {
    button.classList.toggle("btn-primary", button === primary && !button.disabled);
  }
  solveDesc.textContent = !hasResults
    ? "Computes the lens calibration from the saved views."
    : live
      ? "Done. Next camera ends this run so you can plug in the next GoPro."
      : "Done. For another camera, plug it in and click Open preview.";

  // When the route first completes, run a checkpoint solve automatically so the
  // operator gets an early quality readout (and can then Resume to fix weak areas).
  if (state === "complete" && !autoSolved) {
    autoSolved = true;
    runSolve();
  }

  syncStream(live);
  previewEmpty.hidden = live;
  if (state === "error") {
    previewEmpty.replaceChildren(Object.assign(document.createElement("p"), {
      textContent: `${status.message || "The camera could not be opened."} Fix it, then Open preview again.`,
    }));
  } else if (previewEmpty.innerHTML !== previewEmptyHTML) {
    previewEmpty.innerHTML = previewEmptyHTML;
  }

  const bridge = status.video_bridge;
  const fwCmd = bridge ? bridge.ufw_command || extractUfwFromError(bridge.error) : "";
  renderFirewall(bridge, fwCmd);

  if (status.results && status.results.length) {
    renderResults(status.results, status.coverage, status.acquisition_mode, live);
    results.hidden = true;
  } else {
    resultsPanel.hidden = true;
    renderDiagnostics(status, fwCmd);
  }
}

async function poll() {
  try {
    updateStatus(await api("/api/session/status"));
  } catch {
    setStatusLine("Problem: the server is not reachable. Is it still running?");
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

// ---- Presets ----

async function loadPresetList() {
  try {
    const data = await api("/api/presets");
    const list = data.presets || [];
    presetSelect.replaceChildren(presetCustom);
    for (const preset of list) {
      presetSelect.append(new Option(preset.title || preset.name, preset.name));
    }
    const configs = await Promise.all(list.map((preset) =>
      api(`/api/presets/${encodeURIComponent(preset.name)}`)
        .then((data) => [preset.name, data.config])
        .catch(() => [preset.name, null])));
    presetConfigs = Object.fromEntries(configs);
  } catch {
    // Presets endpoint unavailable; the dropdown keeps only "Custom settings".
  }
}

// The camera setup whose config is exactly this one, or "" for custom settings.
function matchingPreset(config) {
  const want = JSON.stringify(config);
  for (const [name, cfg] of Object.entries(presetConfigs)) {
    if (cfg && JSON.stringify(cfg) === want) return name;
  }
  return "";
}

function showPresetChoice(name) {
  presetChoice = name;
  presetSelect.value = name;
  presetCustom.hidden = Boolean(name); // offered only while it is the truth
  if (lastStatus) updateStatus(lastStatus); // step 1 follows the choice
}

function appliedNote(what) {
  return currentLive
    ? `${what} They reach the camera when you click Start new run.`
    : `${what}`;
}

// A choice in step 1 fills in the settings at once (there is no Load button).
async function applyPreset(name) {
  try {
    const preset = await api(`/api/presets/${encodeURIComponent(name)}`);
    const cfg = preset.config || preset;
    baseConfig = structuredClone(cfg);
    presetConfigs[name] = cfg;
    populateForm(cfg, defaults.aruco_dictionaries, defaults.gopro_options);
    showPresetChoice(name);
    presetMsg.textContent = appliedNote(`Settings filled in for "${preset.title || name}".`);
  } catch (err) {
    showPresetChoice(presetChoice);
    presetMsg.textContent = `Could not load that camera setup: ${err.message || err}`;
  }
}

function resetSettings() {
  // Back to the settings the server started with.
  baseConfig = null;
  populateForm(defaults.config, defaults.aruco_dictionaries, defaults.gopro_options);
  showPresetChoice(matchingPreset(defaults.config));
  presetMsg.textContent = appliedNote("Put back the settings the server started with.");
}

async function savePreset() {
  const name = prompt("Save the current settings as a camera setup named:");
  if (!name) return;
  try {
    const saved = await api(`/api/presets/${encodeURIComponent(name)}`, {
      method: "POST",
      body: JSON.stringify({config: readForm()}),
    });
    await loadPresetList();
    // The server may tidy the name; the file it wrote is the one to select.
    const stem = (saved.path || "").split(/[\\/]/).pop().replace(/\.ya?ml$/, "") || name;
    showPresetChoice(stem in presetConfigs ? stem : "");
    presetMsg.textContent = `Saved "${name}". It is now listed under Camera setup.`;
  } catch (err) {
    presetMsg.textContent = `Could not save the camera setup: ${err.message || err}`;
  }
}

// ---- Actions ----

// Every button runs through here, so a failed request says what failed on the
// status line instead of dying silently in the console.
function act(label, fn) {
  return async () => {
    const name = typeof label === "function" ? label() : label;
    try {
      await fn();
    } catch (err) {
      setStatusLine(`Problem: ${name} failed: ${err.message || err}`);
      syncStream(currentLive);
    }
  };
}

previewBtn.addEventListener("click", act("Open preview", async () => {
  firewallDismissed = false;
  autoSolved = false;
  startStream();
  updateStatus(await api("/api/session/preview", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
}));

startRunBtn.addEventListener("click", act("Start new run", async () => {
  firewallDismissed = false;
  autoSolved = false;
  results.hidden = true;
  resultsPanel.hidden = true;
  startStream();
  updateStatus(await api("/api/session/run", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
  startPolling();
}));

pauseResumeBtn.addEventListener("click", act(() => pauseResumeBtn.textContent, async () => {
  const action = pauseResumeBtn.textContent === "Resume" ? "resume" : "pause";
  updateStatus(await api(`/api/session/${action}`, {method: "POST"}));
}));

stopBtn.addEventListener("click", act("Stop", async () => {
  // Tear the video down first so a final stream "load" cannot flip the dot back
  // to green while the request is in flight.
  stopStream();
  clearInterval(pollTimer);
  updateStatus(await api("/api/session/stop", {method: "POST"}));
}));

captureBtn.addEventListener("click", act("Capture now", async () => {
  updateStatus(await api("/api/session/capture", {method: "POST"}));
}));

async function runSolve() {
  // After Stop nothing polls, so no "solving" status would disable the button.
  solveBtn.disabled = true;
  setStatusLine(stateSentence("solving", 0));
  try {
    // Pause capture first so solving from an active run is a clean, explicit stop.
    if (currentState === "capturing") {
      await api("/api/session/pause", {method: "POST"});
    }
    await api("/api/session/solve", {method: "POST"});
  } catch (err) {
    await poll(); // the session parks itself paused on a failed solve
    setStatusLine(`Problem: Solve failed: ${err.message || err}`);
    return;
  }
  await poll(); // status now carries results -> updateStatus renders the panel
}

solveBtn.addEventListener("click", runSolve);

nextCameraBtn.addEventListener("click", act("Next camera", async () => {
  const captures = lastStatus?.captures || 0;
  const unsolved = captures > 0 && !(lastStatus?.results || []).length;
  if (unsolved && !confirm(
    `Next camera ends this run. Its ${captures} views stay on disk but can then be solved only `
    + "with the solve-frames command. Continue?",
  )) return;
  // Cleanly stop the current camera and go idle; the operator swaps the camera,
  // edits the camera name if needed, then clicks Open preview for the new one.
  firewallDismissed = false;
  autoSolved = false;
  results.hidden = true;
  resultsPanel.hidden = true;
  // Tear the video down first (before awaiting) so the dot does not flip back to
  // green on the stream's final load while the camera is being stopped.
  stopStream();
  clearInterval(pollTimer);
  updateStatus(await api("/api/session/next-camera", {
    method: "POST",
    body: JSON.stringify({config: readForm()}),
  }));
}));

presetSelect.addEventListener("change", () => applyPreset(presetSelect.value));
presetSave.addEventListener("click", savePreset);
presetReset.addEventListener("click", resetSettings);

for (const name of ["gopro.enabled", "gopro.apply_on_preview"]) {
  form.elements[name].addEventListener("change", () => {
    syncGoproVisibility();
    if (lastStatus) updateStatus(lastStatus); // the idle readout depends on both
  });
}
form.addEventListener("input", (event) => {
  if (event.target.name?.startsWith("board.")) updateBoardSummary();
});

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

// Canvases are sized to their CSS box; redraw them whenever that box changes
// (window resize, a scrollbar appearing, the results panel opening).
function redrawCanvases() {
  const status = lastStatus || {};
  drawCoverageMap(status.coverage?.points || [], status.image_size, targetBox(status.coverage));
  if (lastStatus) drawGuideOverlay(lastStatus);
}
new ResizeObserver(redrawCanvases).observe(scatter);
new ResizeObserver(redrawCanvases).observe(guideOverlay);

async function init() {
  defaults = await api("/api/defaults");
  populateForm(defaults.config, defaults.aruco_dictionaries, defaults.gopro_options);
  await loadPresetList();
  showPresetChoice(matchingPreset(defaults.config));
  setStatusLine("Ready.");
  drawCoverageMap([], null);
  await poll();
  if (lastStatus && isLive(lastStatus)) startPolling();
}

init().catch((err) => {
  setStatusLine(`Problem: could not load the app settings: ${err.message || err}. Is the server running?`);
});
