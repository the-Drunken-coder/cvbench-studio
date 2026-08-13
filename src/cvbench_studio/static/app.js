const $ = (selector) => document.querySelector(selector);
const state = {
  projects: [],
  project: null,
  annotations: null,
  selectedTrack: null,
  dirty: false,
  interaction: null,
};

const palette = ["#a7f36b", "#72d8ff", "#ffbe6b", "#d18cff", "#ff7f73", "#71e6c1"];

async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `${response.status} ${response.statusText}`;
    try { message = (await response.json()).error || message; } catch {}
    throw new Error(message);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("application/json") ? response.json() : response;
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("visible");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("visible"), 3200);
}

function escapeText(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}

function markDirty(value = true) {
  state.dirty = value;
  const status = $("#save-state");
  status.textContent = value ? "Unsaved changes" : "Saved locally";
  status.className = `status ${value ? "bad" : "good"}`;
}

async function refreshProjects() {
  state.projects = (await api("/api/projects")).projects;
  $("#project-list").innerHTML = state.projects.map(project => `
    <button class="project-card ${state.project?.id === project.id ? "selected" : ""}" data-id="${project.id}">
      ${escapeText(project.name)}
      <span>${project.video ? `${project.video.frame_count} frames` : "No video"}</span>
    </button>`).join("");
  document.querySelectorAll(".project-card").forEach(button => {
    button.addEventListener("click", () => openProject(button.dataset.id));
  });
}

async function openProject(projectId) {
  if (state.dirty && !confirm("Discard unsaved annotation changes?")) return;
  state.project = await api(`/api/projects/${projectId}`);
  state.annotations = await api(`/api/projects/${projectId}/annotations`);
  state.selectedTrack = state.annotations.tracks[0]?.id || null;
  state.dirty = false;
  $("#empty-state").hidden = true;
  $("#editor").hidden = false;
  $("#inspector").hidden = false;
  $("#project-id").textContent = state.project.id;
  $("#project-name").textContent = state.project.name;
  $("#fps").value = state.project.video?.fps || 30;
  $("#validate").disabled = false;
  $("#export").disabled = !state.project.video;
  $("#draw-box").disabled = !state.project.video || !state.selectedTrack;
  $("#copy-previous").disabled = !state.project.video || !state.selectedTrack;
  $("#delete-track").disabled = !state.selectedTrack;
  markDirty(false);
  renderTracks();
  await loadVideo();
  await refreshJobs();
  await refreshProjects();
}

async function loadVideo() {
  const hasVideo = Boolean(state.project.video);
  $("#video-empty").hidden = hasVideo;
  $("#stage").hidden = !hasVideo;
  if (!hasVideo) return;
  const video = $("#video");
  video.src = `/api/projects/${state.project.id}/video?v=${state.project.video.sha256}`;
  await new Promise(resolve => {
    if (video.readyState >= 1) resolve();
    else video.addEventListener("loadedmetadata", resolve, {once: true});
  });
  $("#seek").max = video.duration;
  $("#seek").value = video.currentTime;
  resizeCanvas();
  updateFrame();
}

function renderTracks() {
  const counts = Object.fromEntries(state.annotations.tracks.map(track => [track.id, 0]));
  state.annotations.boxes.forEach(box => counts[box.track_id] = (counts[box.track_id] || 0) + 1);
  $("#track-list").innerHTML = state.annotations.tracks.map(track => `
    <button class="track-card ${track.id === state.selectedTrack ? "selected" : ""}" data-id="${track.id}">
      <i class="track-color" style="background:${track.color}"></i>${escapeText(track.name || track.id)}
      <span>${escapeText(track.class_id)} · ${counts[track.id] || 0} boxes</span>
    </button>`).join("") || `<p class="small">Add a track, then draw its box on visible frames.</p>`;
  document.querySelectorAll(".track-card").forEach(button => {
    button.addEventListener("click", () => {
      state.selectedTrack = button.dataset.id;
      renderTracks();
      renderOverlay();
    });
  });
  $("#draw-box").disabled = !state.project.video || !state.selectedTrack;
  $("#copy-previous").disabled = !state.project.video || !state.selectedTrack;
  $("#delete-track").disabled = !state.selectedTrack;
}

function currentFrame() {
  if (!state.project?.video) return 0;
  return Math.max(0, Math.min(
    state.project.video.frame_count - 1,
    Math.round($("#video").currentTime * state.project.video.fps),
  ));
}

function currentBoxes() {
  const frame = currentFrame();
  return state.annotations.boxes.filter(box => box.frame === frame);
}

function selectedBox() {
  return currentBoxes().find(box => box.track_id === state.selectedTrack);
}

function markSelectedTrackModelAssisted() {
  const track = state.annotations.tracks.find(item => item.id === state.selectedTrack);
  if (track?.label_origin?.kind === "model_generated") {
    track.label_origin.kind = "model_assisted";
  }
}

function markBoxModelAssisted(box) {
  markSelectedTrackModelAssisted();
  if (box) delete box.confidence;
}

function updateFrame() {
  const video = $("#video");
  $("#frame").textContent = `Frame ${currentFrame()}`;
  $("#time").textContent = `${video.currentTime.toFixed(3)} s`;
  if (!$("#seek").matches(":active")) $("#seek").value = video.currentTime;
  $("#play-pause").textContent = video.paused ? "Play" : "Pause";
  $("#delete-box").disabled = !selectedBox();
  renderOverlay();
}

function resizeCanvas() {
  const canvas = $("#overlay");
  const video = $("#video");
  canvas.width = video.videoWidth || state.project.video.width;
  canvas.height = video.videoHeight || state.project.video.height;
  renderOverlay();
}

function renderOverlay() {
  const canvas = $("#overlay");
  const context = canvas.getContext("2d");
  context.clearRect(0, 0, canvas.width, canvas.height);
  if (!state.annotations) return;
  const tracks = Object.fromEntries(state.annotations.tracks.map(track => [track.id, track]));
  for (const box of currentBoxes()) {
    const track = tracks[box.track_id];
    if (!track) continue;
    const [x1, y1, x2, y2] = box.bbox_xyxy;
    const selected = box.track_id === state.selectedTrack;
    context.strokeStyle = track.color;
    context.lineWidth = selected ? 4 : 2;
    context.strokeRect(x1, y1, x2 - x1, y2 - y1);
    context.fillStyle = `${track.color}d9`;
    context.font = "bold 13px system-ui";
    const label = `${track.class_id} · ${track.name || track.id}`;
    const width = context.measureText(label).width + 12;
    context.fillRect(x1, Math.max(0, y1 - 22), width, 22);
    context.fillStyle = "#081008";
    context.fillText(label, x1 + 6, Math.max(15, y1 - 6));
    if (selected) {
      context.fillStyle = track.color;
      context.fillRect(x2 - 8, y2 - 8, 16, 16);
    }
  }
}

function canvasPoint(event) {
  const canvas = $("#overlay");
  const rect = canvas.getBoundingClientRect();
  return [
    (event.clientX - rect.left) * canvas.width / rect.width,
    (event.clientY - rect.top) * canvas.height / rect.height,
  ];
}

function hitBox(point) {
  const [x, y] = point;
  return [...currentBoxes()].reverse().find(box => {
    const [x1, y1, x2, y2] = box.bbox_xyxy;
    return x >= x1 && x <= x2 && y >= y1 && y <= y2;
  });
}

function clampBox(box) {
  const width = state.project.video.width;
  const height = state.project.video.height;
  box[0] = Math.max(0, Math.min(width, box[0]));
  box[1] = Math.max(0, Math.min(height, box[1]));
  box[2] = Math.max(0, Math.min(width, box[2]));
  box[3] = Math.max(0, Math.min(height, box[3]));
}

$("#overlay").addEventListener("pointerdown", event => {
  if (!state.selectedTrack) return;
  const point = canvasPoint(event);
  const hit = hitBox(point);
  if (hit) {
    const previousSelectedTrack = state.selectedTrack;
    state.selectedTrack = hit.track_id;
    const [x1, y1, x2, y2] = hit.bbox_xyxy;
    const resize = Math.abs(point[0] - x2) < 18 && Math.abs(point[1] - y2) < 18;
    state.interaction = {
      kind: resize ? "resize" : "move",
      point,
      box: hit,
      original: [...hit.bbox_xyxy],
      previousSelectedTrack,
      wasDirty: state.dirty,
    };
    renderTracks();
  } else {
    const existing = selectedBox();
    if (existing && !confirm("Replace this track's box on the current frame?")) return;
    const track = state.annotations.tracks.find(item => item.id === state.selectedTrack);
    const originalLabelOrigin = track?.label_origin ? structuredClone(track.label_origin) : null;
    const wasDirty = state.dirty;
    markSelectedTrackModelAssisted();
    const replacedIndex = existing ? state.annotations.boxes.indexOf(existing) : -1;
    if (existing) state.annotations.boxes = state.annotations.boxes.filter(box => box !== existing);
    const box = {frame: currentFrame(), track_id: state.selectedTrack, bbox_xyxy: [point[0], point[1], point[0], point[1]]};
    state.annotations.boxes.push(box);
    state.interaction = {kind: "draw", point, box, replaced: existing, replacedIndex, originalLabelOrigin, wasDirty};
    markDirty();
  }
  $("#overlay").setPointerCapture(event.pointerId);
});

$("#overlay").addEventListener("pointermove", event => {
  if (!state.interaction) return;
  const point = canvasPoint(event);
  const action = state.interaction;
  if (action.kind === "draw") {
    action.box.bbox_xyxy = [
      Math.min(action.point[0], point[0]), Math.min(action.point[1], point[1]),
      Math.max(action.point[0], point[0]), Math.max(action.point[1], point[1]),
    ];
  } else if (action.kind === "move") {
    const dx = point[0] - action.point[0], dy = point[1] - action.point[1];
    action.box.bbox_xyxy = action.original.map((value, index) => value + (index % 2 ? dy : dx));
    const [x1, y1, x2, y2] = action.box.bbox_xyxy;
    if (x1 < 0) { action.box.bbox_xyxy[0] -= x1; action.box.bbox_xyxy[2] -= x1; }
    if (y1 < 0) { action.box.bbox_xyxy[1] -= y1; action.box.bbox_xyxy[3] -= y1; }
    if (x2 > state.project.video.width) {
      const offset = x2 - state.project.video.width;
      action.box.bbox_xyxy[0] -= offset; action.box.bbox_xyxy[2] -= offset;
    }
    if (y2 > state.project.video.height) {
      const offset = y2 - state.project.video.height;
      action.box.bbox_xyxy[1] -= offset; action.box.bbox_xyxy[3] -= offset;
    }
  } else {
    action.box.bbox_xyxy[2] = point[0];
    action.box.bbox_xyxy[3] = point[1];
    clampBox(action.box.bbox_xyxy);
  }
  renderOverlay();
});

function finishInteraction() {
  if (!state.interaction) return;
  const action = state.interaction;
  const box = action.box;
  box.bbox_xyxy = box.bbox_xyxy.map(value => Math.round(value * 1000) / 1000);
  const [x1, y1, x2, y2] = box.bbox_xyxy;
  if (x2 - x1 < 3 || y2 - y1 < 3) {
    if (action.kind === "draw") {
      state.annotations.boxes = state.annotations.boxes.filter(item => item !== box);
      if (action.replaced) state.annotations.boxes.splice(action.replacedIndex, 0, action.replaced);
      const track = state.annotations.tracks.find(item => item.id === action.box.track_id);
      if (track) {
        if (action.originalLabelOrigin) track.label_origin = action.originalLabelOrigin;
        else delete track.label_origin;
      }
      markDirty(action.wasDirty);
    } else {
      box.bbox_xyxy = [...action.original];
      state.selectedTrack = action.previousSelectedTrack;
      markDirty(action.wasDirty);
    }
  } else if (
    action.kind !== "draw"
    && box.bbox_xyxy.some((value, index) => value !== action.original[index])
  ) {
    markBoxModelAssisted(box);
    markDirty();
  }
  state.interaction = null;
  renderTracks();
  updateFrame();
}

function cancelInteraction() {
  if (!state.interaction) return;
  const action = state.interaction;
  if (action.kind === "draw") {
    state.annotations.boxes = state.annotations.boxes.filter(item => item !== action.box);
    if (action.replaced) state.annotations.boxes.splice(action.replacedIndex, 0, action.replaced);
    const track = state.annotations.tracks.find(item => item.id === action.box.track_id);
    if (track) {
      if (action.originalLabelOrigin) track.label_origin = action.originalLabelOrigin;
      else delete track.label_origin;
    }
  } else {
    action.box.bbox_xyxy = [...action.original];
    state.selectedTrack = action.previousSelectedTrack;
  }
  state.interaction = null;
  markDirty(action.wasDirty);
  renderTracks();
  updateFrame();
}

$("#overlay").addEventListener("pointerup", finishInteraction);
$("#overlay").addEventListener("pointercancel", cancelInteraction);

$("#new-project").addEventListener("click", () => $("#project-dialog").showModal());
$("#empty-new-project").addEventListener("click", () => $("#project-dialog").showModal());
$("#project-form").addEventListener("submit", async event => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  try {
    const project = await api("/api/projects", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        name: $("#new-name").value,
        classes: $("#new-classes").value.split(",").map(item => item.trim()).filter(Boolean),
      }),
    });
    $("#project-dialog").close();
    $("#project-form").reset();
    $("#new-classes").value = "person, vehicle, dog, sports_ball";
    await openProject(project.id);
  } catch (error) { toast(error.message); }
});

$("#add-track").addEventListener("click", () => {
  const classId = prompt(`Class (${state.project.classes.join(", ")}):`, state.project.classes[0]);
  if (!classId) return;
  if (!state.project.classes.includes(classId)) return toast("Choose a class from the project ontology.");
  const name = prompt("Track name:", `${classId} ${state.annotations.tracks.length + 1}`);
  if (!name) return;
  const base = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "") || classId;
  let id = base.slice(0, 50);
  while (state.annotations.tracks.some(track => track.id === id)) id = `${base.slice(0, 41)}-${crypto.randomUUID().slice(0, 8)}`;
  state.annotations.tracks.push({id, class_id: classId, name, color: palette[state.annotations.tracks.length % palette.length]});
  state.selectedTrack = id;
  markDirty();
  renderTracks();
  renderOverlay();
});

$("#delete-track").addEventListener("click", () => {
  if (!state.selectedTrack || !confirm(`Delete track ${state.selectedTrack} and all of its boxes?`)) return;
  state.annotations.tracks = state.annotations.tracks.filter(track => track.id !== state.selectedTrack);
  state.annotations.boxes = state.annotations.boxes.filter(box => box.track_id !== state.selectedTrack);
  state.selectedTrack = state.annotations.tracks[0]?.id || null;
  markDirty();
  renderTracks();
  updateFrame();
});

$("#delete-box").addEventListener("click", () => {
  const box = selectedBox();
  if (!box) return;
  state.annotations.boxes = state.annotations.boxes.filter(item => item !== box);
  markDirty();
  renderTracks();
  updateFrame();
});

$("#draw-box").addEventListener("click", () => toast("Drag on the video to create or replace the selected track's box."));
$("#copy-previous").addEventListener("click", () => {
  const frame = currentFrame();
  const prior = state.annotations.boxes
    .filter(box => box.track_id === state.selectedTrack && box.frame < frame)
    .sort((a, b) => b.frame - a.frame)[0];
  if (!prior) return toast("No earlier box exists for this track.");
  const existing = selectedBox();
  if (existing) {
    if (existing.bbox_xyxy.every((value, index) => value === prior.bbox_xyxy[index])) {
      return toast("The current box already matches the previous box.");
    }
    markSelectedTrackModelAssisted();
    existing.bbox_xyxy = [...prior.bbox_xyxy];
    delete existing.confidence;
  }
  else {
    markSelectedTrackModelAssisted();
    state.annotations.boxes.push({frame, track_id: state.selectedTrack, bbox_xyxy: [...prior.bbox_xyxy]});
  }
  markDirty();
  renderTracks();
  updateFrame();
});

$("#video-file").addEventListener("change", async event => {
  const file = event.target.files[0];
  if (!file) return;
  const probe = document.createElement("video");
  const objectUrl = URL.createObjectURL(file);
  probe.src = objectUrl;
  try {
    await new Promise((resolve, reject) => {
      probe.addEventListener("loadedmetadata", resolve, {once: true});
      probe.addEventListener("error", () => reject(new Error("Browser could not read video metadata.")), {once: true});
    });
    $("#save-state").textContent = "Uploading video…";
    state.project = await api(`/api/projects/${state.project.id}/video`, {
      method: "PUT",
      headers: {
        "Content-Type": file.type || "application/octet-stream",
        "X-Video-Filename": file.name,
        "X-Video-Width": probe.videoWidth,
        "X-Video-Height": probe.videoHeight,
        "X-Video-Duration": probe.duration,
        "X-Video-Fps": Number($("#fps").value || 30),
      },
      body: file,
    });
    markDirty(false);
    $("#export").disabled = false;
    await loadVideo();
    await refreshProjects();
    toast("Video imported.");
  } catch (error) { toast(error.message); }
  finally { URL.revokeObjectURL(objectUrl); event.target.value = ""; }
});

async function save() {
  if (!state.project) return;
  await api(`/api/projects/${state.project.id}/annotations`, {
    method: "PUT",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(state.annotations),
  });
  markDirty(false);
}

$("#validate").addEventListener("click", async () => {
  try {
    if (state.dirty) await save();
    const result = await api(`/api/projects/${state.project.id}/validate`);
    toast(result.valid
      ? `Valid: ${result.counts.tracks} tracks, ${result.counts.boxes} boxes${result.warnings.length ? ` · ${result.warnings.length} warning(s)` : ""}`
      : result.errors.join(" · "));
  } catch (error) { toast(error.message); }
});

$("#export").addEventListener("click", () => {
  const source = state.project.source || {};
  $("#source-title").value = source.title || state.project.name;
  $("#source-uri").value = source.uri || "";
  $("#license-spdx").value = source.license_spdx || "";
  $("#license-name").value = source.license_name || "";
  $("#license-url").value = source.license_url || "";
  $("#license-text").value = source.license_text || "";
  $("#source-dialog").showModal();
});

$("#source-form").addEventListener("submit", async event => {
  if (event.submitter?.value === "cancel") return;
  event.preventDefault();
  try {
    if (state.dirty) await save();
    state.project = await api(`/api/projects/${state.project.id}/source`, {
      method: "PUT",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        title: $("#source-title").value,
        uri: $("#source-uri").value,
        license_spdx: $("#license-spdx").value,
        license_name: $("#license-name").value,
        license_url: $("#license-url").value,
        license_text: $("#license-text").value,
      }),
    });
    $("#source-dialog").close();
    const response = await api(`/api/projects/${state.project.id}/export`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: "{}",
    });
    const blob = await response.blob();
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${state.project.id}.cvbench.zip`;
    link.click();
    URL.revokeObjectURL(link.href);
    toast("Draft contribution package exported. Review ledger remains empty.");
  } catch (error) { toast(error.message); }
});

$("#run-model").addEventListener("click", async () => {
  const command = $("#model-command").value.trim();
  if (!command) return toast("Enter an external adapter command.");
  try {
    await api(`/api/projects/${state.project.id}/jobs`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({command}),
    });
    toast("Adapter queued. Proposals will not modify truth automatically.");
    await refreshJobs();
  } catch (error) { toast(error.message); }
});

async function refreshJobs() {
  if (!state.project) return;
  const jobs = (await api(`/api/projects/${state.project.id}/jobs`)).jobs;
  $("#job-list").innerHTML = jobs.slice(-5).reverse().map(job => `
    <div class="job-card">
      ${escapeText(job.status)}<span>${escapeText(job.id.slice(0, 8))}</span>
      ${job.status === "completed" && job.model
        ? `<button class="secondary full import-proposals" data-job="${job.id}">Review/import proposals</button>`
        : ""}
    </div>`).join("");
  document.querySelectorAll(".import-proposals").forEach(button => {
    button.addEventListener("click", () => importProposals(button.dataset.job));
  });
  if (jobs.some(job => ["queued", "running"].includes(job.status))) setTimeout(refreshJobs, 1200);
}

async function importProposals(jobId) {
  try {
    if (state.dirty) await save();
    const proposals = await api(`/api/projects/${state.project.id}/jobs/${jobId}/proposals`);
    state.project.video.frame_count = proposals.frame_count;
    const summary = proposals.summary;
    if (!summary.boxes) return toast("The completed adapter produced no proposal rows.");
    const message = [
      `Import ${summary.boxes} boxes across ${summary.tracks} tracks?`,
      `Classes: ${summary.classes.join(", ") || "none"}.`,
      "They will be editable draft annotations, not reviewed or certified truth.",
    ].join("\n\n");
    if (!confirm(message)) return;
    const ids = new Set(proposals.tracks.map(track => track.id));
    state.annotations.tracks = state.annotations.tracks.filter(track => !ids.has(track.id));
    state.annotations.boxes = state.annotations.boxes.filter(box => !ids.has(box.track_id));
    state.annotations.tracks.push(...proposals.tracks);
    state.annotations.boxes.push(...proposals.boxes);
    state.selectedTrack = proposals.tracks[0]?.id || state.selectedTrack;
    markDirty();
    renderTracks();
    if (proposals.boxes[0]) $("#video").currentTime = proposals.boxes[0].frame / state.project.video.fps;
    updateFrame();
    toast("Proposals imported to the editable draft. Scrub and correct them before review.");
  } catch (error) { toast(error.message); }
}

$("#previous-frame").addEventListener("click", () => {
  $("#video").currentTime = Math.max(0, (currentFrame() - 1) / state.project.video.fps);
});
$("#next-frame").addEventListener("click", () => {
  $("#video").currentTime = Math.min($("#video").duration, (currentFrame() + 1) / state.project.video.fps);
});
$("#play-pause").addEventListener("click", () => {
  const video = $("#video");
  if (video.paused) video.play();
  else video.pause();
});
$("#seek").addEventListener("input", event => {
  $("#video").currentTime = Number(event.target.value);
});
$("#video").addEventListener("timeupdate", updateFrame);
$("#video").addEventListener("seeked", updateFrame);
$("#video").addEventListener("play", updateFrame);
$("#video").addEventListener("pause", updateFrame);
$("#video").addEventListener("loadedmetadata", resizeCanvas);
window.addEventListener("resize", resizeCanvas);
window.addEventListener("keydown", async event => {
  if ((event.metaKey || event.ctrlKey) && event.key === "s") {
    event.preventDefault();
    try { await save(); toast("Saved."); } catch (error) { toast(error.message); }
  }
});
window.addEventListener("beforeunload", event => {
  if (state.dirty) event.preventDefault();
});

refreshProjects().catch(error => toast(error.message));
