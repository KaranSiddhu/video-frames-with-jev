"use strict";

const $ = (id) => document.getElementById(id);
const video = $("video");
const overlay = $("boxOverlay");
const context = overlay.getContext("2d");
let config = null, busy = false, loaded = false, hasCompletedResponse = false;
const number = (value) => value == null ? "—" : Number(value).toLocaleString(undefined, { maximumFractionDigits: 1 });
const probability = (value) => value == null ? "—" : Number(value).toFixed(3);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function renderJSON(id, value) {
  const text = JSON.stringify(value, null, 2);
  const fragment = document.createDocumentFragment();
  const tokens = /"(?:\\[\s\S]|[^"\\])*"|\b(?:true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|[{}\[\],:]/g;
  let offset = 0;
  for (const match of text.matchAll(tokens)) {
    fragment.append(document.createTextNode(text.slice(offset, match.index)));
    const token = match[0];
    let kind = "punctuation";
    if (token.startsWith('"')) kind = /^\s*:/.test(text.slice(match.index + token.length)) ? "key" : "string";
    else if (token === "null") kind = "null";
    else if (token === "true" || token === "false") kind = "boolean";
    else if (/^-?\d/.test(token)) kind = "number";
    const span = document.createElement("span");
    span.className = `json-${kind}`;
    // API strings stay text, never HTML. The underlying JSON is unchanged.
    span.textContent = token;
    fragment.append(span);
    offset = match.index + token.length;
  }
  fragment.append(document.createTextNode(text.slice(offset)));
  $(id).replaceChildren(fragment);
}

function notice(text, error = false) {
  $("notice").textContent = text;
  $("notice").title = text;
  $("notice").hidden = !text;
  document.querySelector(".shell").classList.toggle("has-notice", Boolean(text));
  $("notice").classList.toggle("error", error);
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : JSON.stringify(result.detail));
  return result;
}

function readyState() {
  $("startButton").disabled = busy || !loaded || !config?.live_ready;
  $("startButton").textContent = busy ? "Analyzing" : "Start";
  $("startButton").setAttribute("aria-busy", String(busy));
}

function status(text, kind = "") {
  $("connection").textContent = text;
  $("connection").className = `badge ${kind}`;
  $("connection").hidden = !text || text === "Ready";
}

function decisionLabel(value) {
  return { marked: "Person located", not_present: "No match", multiple_people: "Multiple people", unclear: "Unclear",
    low_presence: "Low confidence", low_box_confidence: "Low confidence", missing_box_confidence: "Confidence unavailable",
    api_error: "Analysis unavailable" }[value] || "No usable location";
}

function clearBorder() {
  context.clearRect(0, 0, overlay.width, overlay.height);
  overlay.dataset.marked = "false";
}

function drawBorder(record) {
  clearBorder();
  if (!record.decision.draw) return;
  const box = record.decision.video_box || record.decision.box;
  context.strokeStyle = "#ed424b";
  context.lineWidth = Math.max(3, overlay.width / 250);
  context.strokeRect(box.left * overlay.width, box.top * overlay.height,
    (box.right - box.left) * overlay.width, (box.bottom - box.top) * overlay.height);
  overlay.dataset.marked = "true";
}

function showSummary(summary) {
  $("metricFrames").textContent = `${summary.frames} / ${summary.marked}`;
  $("metricErrors").textContent = `${summary.successful_responses} responses · ${summary.errors} errors`;
  $("metricInput").textContent = summary.frames ? number(summary.known_input_tokens) : "—";
  $("metricOutput").textContent = summary.frames ? number(summary.known_output_tokens) : "—";
  $("metricUsage").textContent = summary.missing_input_usage ? `Partial total · ${summary.missing_input_usage} unreported` : "Reported input usage";
  $("metricCost").textContent = `Reported-input estimate: $${summary.estimated_known_input_cost_usd.toFixed(8)}`;
  $("metricSDK").textContent = summary.sdk_p50_ms == null ? "—" : `${number(summary.sdk_p50_ms)} / ${number(summary.sdk_p95_ms)} ms`;
  $("metricSamples").textContent = `${summary.sdk_timing_samples} timed requests`;
  $("metricClient").textContent = summary.client_p50_ms == null ? "—" : `${number(summary.client_p50_ms)} ms`;
  $("metricClientSamples").textContent = `${summary.client_timing_samples} browser measurements`;
  document.querySelectorAll(".metric small").forEach((el) => { el.title = el.textContent; });
}

async function seekVideo(timestamp) {
  clearBorder();
  video.pause();
  if (Math.abs(video.currentTime - timestamp) < .001 && video.readyState >= 2) return;
  await new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { cleanup(); reject(new Error("This video frame could not be loaded. Please try again.")); }, 15000);
    const cleanup = () => { clearTimeout(timeout); video.removeEventListener("seeked", done); video.removeEventListener("error", failed); };
    const done = () => { cleanup(); resolve(); };
    const failed = () => { cleanup(); reject(new Error("The video could not be loaded. Please try again.")); };
    video.addEventListener("seeked", done, { once: true });
    video.addEventListener("error", failed, { once: true });
    video.currentTime = timestamp;
  });
}

async function capture(timestamp) {
  await seekVideo(timestamp);
  const canvas = document.createElement("canvas");
  const scale = config.fixed_settings.max_edge / Math.max(video.videoWidth, video.videoHeight);
  canvas.width = Math.round(video.videoWidth * scale);
  canvas.height = Math.round(video.videoHeight * scale);
  if (Math.min(canvas.width, canvas.height) < 280) throw new Error("This video's aspect ratio cannot be analyzed at the current resolution.");
  canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
  return { image_base64: canvas.toDataURL("image/jpeg", .9).split(",")[1], timestamp_seconds: video.currentTime };
}

async function start() {
  if (busy || !loaded || !config?.live_ready) return;
  busy = true; readyState(); clearBorder();
  let runId = null, completed = 0;
  $("outputState").textContent = hasCompletedResponse ? "Updating" : "Waiting for Jev";
  try {
    status("Analyzing", "working");
    notice("Analyzing the footage. A location is highlighted only after Jev responds.");
    const run = await api("/api/fixed-runs", { duration_seconds: video.duration, width: video.videoWidth, height: video.videoHeight });
    runId = run.run_id;
    const settings = run.manifest.settings;
    showSummary({ frames: 0, marked: 0, successful_responses: 0, errors: 0, missing_input_usage: 0, estimated_known_input_cost_usd: 0, sdk_p50_ms: null, sdk_timing_samples: 0, client_p50_ms: null, client_timing_samples: 0 });
    for (let index = 0; index < settings.max_frames; index++) {
      const timestamp = index * settings.interval_seconds;
      if (timestamp >= video.duration) break;
      $("frameDecision").textContent = "Analyzing"; $("frameDecision").className = "badge";
      $("outputState").textContent = hasCompletedResponse ? "Updating" : "Waiting for Jev";
      if (!hasCompletedResponse) $("responseLabel").textContent = "Waiting for the first response.";
      notice(`Analyzing frame ${index + 1} / ${settings.max_frames} at ${timestamp.toFixed(2)}s…`);
      const begin = performance.now();
      const frame = await capture(timestamp);
      const captureMS = performance.now() - begin;
      $("frameTitle").textContent = `Frame #${index + 1} · ${frame.timestamp_seconds.toFixed(3)}s`;
      const httpStart = performance.now();
      const result = await api(`/api/runs/${runId}/frames`, { index, ...frame, capture_ms: captureMS });
      const finished = performance.now();
      const r = result.record;
      renderJSON("requestJSON", { headers: { "TypeSafe-Danger-Zone-Images": "true" }, ...r.request });
      renderJSON("responseJSON", r.error ? { error: r.error, response: r.response } : r.response);
      hasCompletedResponse = true;
      $("outputState").textContent = r.error ? "Error" : "Response received";
      $("frameDecision").textContent = decisionLabel(r.decision.status);
      $("frameDecision").className = `badge ${r.decision.draw ? "good" : r.error ? "bad" : ""}`;
      $("frameDetail").textContent = `Presence p=${probability(r.decision.presence_probability)} · people: ${r.decision.subjects || "—"} · Box confidence: ${probability(r.decision.box_confidence)}`;
      $("responseLabel").textContent = r.response.model || "No response received";
      drawBorder(r);
      showSummary(result.summary);
      completed++;
      const measured = await api(`/api/runs/${runId}/frames/${index}/timing`, { http_roundtrip_ms: finished - httpStart, end_to_end_ms: finished - begin });
      showSummary(measured.summary);
      if (r.error) throw new Error("This frame could not be analyzed. Please try again.");
      const moreFrames = index + 1 < settings.max_frames && (index + 1) * settings.interval_seconds < video.duration;
      if (moreFrames && (result.summary.known_input_tokens >= settings.input_token_limit || result.summary.missing_input_usage > 0)) {
        status("Paused");
        notice(result.summary.missing_input_usage ? "Analysis paused because usage information was not reported." : `Analysis paused at the ${settings.input_token_limit.toLocaleString()} input-token limit.`);
        break;
      }
      // Presentation hold is not part of the recorded SDK/browser timing.
      await sleep(650);
    }
    if ($("connection").textContent !== "Paused") {
      status("Complete", "good");
      notice(`Analysis complete · ${completed} frames analyzed.`);
    }
  } catch (error) {
    status("Analysis unavailable", "bad");
    notice("Analysis could not be completed. Please check the local setup and try again.", true);
  } finally {
    if (runId) {
      try { await api(`/api/runs/${runId}/finish`, {}); }
      catch (error) { status("Analysis unavailable", "bad"); notice("Analysis finished, but its record could not be saved completely. Please check the local setup.", true); }
    }
    busy = false; readyState();
  }
}

video.addEventListener("loadeddata", () => {
  loaded = Number.isFinite(video.duration) && video.duration > 0;
  overlay.width = video.videoWidth; overlay.height = video.videoHeight;
  clearBorder(); $("videoEmpty").hidden = true; readyState();
  if (config?.live_ready) {
    status("Ready", "good");
    notice("");
  }
});
video.addEventListener("seeking", clearBorder);
video.addEventListener("error", () => {
  loaded = false; readyState();
  $("videoEmpty").hidden = false;
  $("emptyTitle").textContent = "Video could not be loaded";
  $("emptyDetail").textContent = "Please check the source video and try again.";
  status("Video unavailable", "bad");
  notice("The video could not be loaded.", true);
});
$("startButton").onclick = start;

(async function init() {
  renderJSON("requestJSON", {});
  renderJSON("responseJSON", {});
  if (location.protocol === "file:") {
    status("Open the local app");
    notice("Open this experience at http://127.0.0.1:8880.", true);
    return;
  }
  try {
    config = await api("/api/config");
    if (!config.video.available) {
      status("Video unavailable", "bad");
      $("emptyTitle").textContent = "Footage unavailable";
      $("emptyDetail").textContent = "Please check the source video and try again.";
      notice("The video is currently unavailable.", true);
      return;
    }
    if (!config.live_ready) {
      status("Analysis unavailable");
      notice("Analysis is not configured yet. Please see the setup instructions.", true);
    }
    video.src = config.video.url;
    video.load();
  } catch (error) {
    status("Temporarily unavailable", "bad");
    notice("Unable to load the analysis. Please try again shortly.", true);
  }
})();
