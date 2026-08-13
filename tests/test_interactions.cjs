const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "../src/cvbench_studio/static/app.js"), "utf8");
const start = app.indexOf("function finishInteraction() {");
const end = app.indexOf("function cancelInteraction() {");
const finishInteraction = app.slice(start, end);

function finish(state) {
  const context = {
    state,
    markDirty(value = true) { state.dirty = value; },
    markBoxModelAssisted(box) {
      const track = state.annotations.tracks.find(item => item.id === state.selectedTrack);
      if (track?.label_origin?.kind === "model_generated") track.label_origin.kind = "model_assisted";
      delete box.confidence;
    },
    renderTracks() {},
    updateFrame() {},
  };
  vm.runInNewContext(`${finishInteraction}\nfinishInteraction();`, context);
}

test("undersized resize restores the complete pre-gesture state", () => {
  const box = {
    frame: 0,
    track_id: "model-track",
    bbox_xyxy: [10, 10, 12, 12],
    confidence: 0.9,
  };
  const state = {
    selectedTrack: "model-track",
    dirty: false,
    annotations: {
      tracks: [{id: "model-track", label_origin: {kind: "model_generated"}}],
      boxes: [box],
    },
    interaction: {
      kind: "resize",
      box,
      original: [10, 10, 50, 50],
      previousSelectedTrack: "other-track",
      wasDirty: false,
    },
  };

  finish(state);

  assert.deepEqual(Array.from(box.bbox_xyxy), [10, 10, 50, 50]);
  assert.equal(box.confidence, 0.9);
  assert.equal(state.annotations.tracks[0].label_origin.kind, "model_generated");
  assert.equal(state.selectedTrack, "other-track");
  assert.equal(state.dirty, false);
  assert.equal(state.annotations.boxes[0], box);
});

test("valid resize commits model-assisted state", () => {
  const box = {
    frame: 0,
    track_id: "model-track",
    bbox_xyxy: [10, 10, 60, 60],
    confidence: 0.9,
  };
  const state = {
    selectedTrack: "model-track",
    dirty: false,
    annotations: {
      tracks: [{id: "model-track", label_origin: {kind: "model_generated"}}],
      boxes: [box],
    },
    interaction: {
      kind: "resize",
      box,
      original: [10, 10, 50, 50],
      previousSelectedTrack: "other-track",
      wasDirty: false,
    },
  };

  finish(state);

  assert.deepEqual(Array.from(box.bbox_xyxy), [10, 10, 60, 60]);
  assert.equal(box.confidence, undefined);
  assert.equal(state.annotations.tracks[0].label_origin.kind, "model_assisted");
  assert.equal(state.selectedTrack, "model-track");
  assert.equal(state.dirty, true);
});
