# CVBench Studio

CVBench Studio is a local-first video annotation and truth-review application.
It runs as one native Python process and opens in any modern browser. The UI has
no build step and the Python package has no runtime dependencies.

It is deliberately an annotation tool, not an automatic truth generator.
External models can queue proposals, but their output never modifies human
annotations automatically.

## What the MVP does

- Create and list local projects.
- Import and byte-range serve browser-readable video.
- Scrub by time or step one frame at a time.
- Create, move, resize, copy, and delete boxes.
- Create and delete typed tracks.
- Validate IDs, classes, geometry, bounds, frames, and uniqueness.
- Save deterministic local annotation JSON.
- Export a dataset contribution ZIP containing `contribution.json`,
  `clips/<id>/{video.mp4,tracks.jsonl,source.json,review.jsonl}`, and the
  declared license file.
- Queue an explicit external model command and retain its JSONL proposals and
  logs separately from truth.

## Run natively

Python 3.11 or newer is required.

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
.venv/bin/cvbench-studio --data-dir .cvbench-studio serve
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Projects remain under the
chosen data directory.

The browser cannot reliably infer a compressed video's exact source frame rate.
Enter the known FPS before import. Width, height, and duration are read from the
browser's native decoder.

## Annotation workflow

1. Create a project and freeze its class list.
2. Import a video and confirm FPS.
3. Add a track, then drag on the video to create its box.
4. Move a box by dragging it. Resize from its lower-right handle.
5. Step to another frame and draw or copy the previous box.
6. Save with `Cmd-S`/`Ctrl-S`, validate, and export.

Tracks are sparse: only explicitly authored frames are exported. No interpolation
or detector-gap filling is performed.

Export requires an MP4 clip plus source title, original URI, SPDX license ID,
license name/URL, and license text. Studio exports an empty `review.jsonl` on
purpose: its contribution is a draft for dataset intake, not certified truth.

## External models

Run a model in your own environment:

```bash
cvbench-studio --data-dir .cvbench-studio run-model PROJECT_ID -- \
  python examples/mock_adapter.py --video '{video}' --output '{output}'
```

The command is executed directly, never through a shell. The exact tokens
`{video}`, `{output}`, and `{project}` are replaced with local paths. An adapter
must write one JSON object per line to `{output}`. See
[docs/model-adapters.md](docs/model-adapters.md).

The queue validates proposal geometry and complete model provenance, records
the exact command and raw output hash, and offers an explicit **Review/import
proposals** action. Imported output is marked `model_generated`; a manual box
edit changes its origin to `model_assisted`. Neither state is a certification
or review approval.

Adapters should emit one `cvbench.model-output/v1` metadata row containing the
model provenance before any `cvbench.model-proposal/v1` rows. This preserves
the model audit trail even when a successful run finds no objects.

On Apple Silicon, run Studio and a PyTorch adapter natively so the adapter can
select `mps`. Studio does not import PyTorch, select a model, download weights,
or claim that a model is bundled. Docker is an optional Linux deployment path
and does not provide macOS MPS acceleration:

```bash
docker build -t cvbench-studio .
docker run --rm -p 8765:8765 -v "$PWD/.cvbench-studio:/data" cvbench-studio
```

The reusable `examples/yolox_onnx_adapter.py` samples a video at a declared
rate and emits confidence-bearing COCO-class proposals in source pixels. It
hashes the exact ONNX weights and canonical configuration into every Studio
job. Install the optional adapter dependencies, then run it through the normal
explicit review flow:

```bash
.venv/bin/pip install -e '.[model-adapters]'
cvbench-studio --data-dir .cvbench-studio run-model PROJECT_ID -- \
  python /absolute/path/to/cvbench-studio/examples/yolox_onnx_adapter.py \
  --video '{video}' --output '{output}' \
  --model /path/to/yolox_x.onnx \
  --weights-uri docker://image@sha256:digest#models/yolox_x.onnx \
  --code-revision "$(git rev-parse HEAD)"
```

The adapter is a detector, not a source of reviewed truth. It intentionally
assigns frame-local proposal IDs; Studio users must correct classes and boxes
and decide whether cross-frame identities should be linked.

## Validate and export from the CLI

```bash
cvbench-studio --data-dir .cvbench-studio validate PROJECT_ID
cvbench-studio --data-dir .cvbench-studio export PROJECT_ID --output clip.cvbench.zip
```

## Test

The suite uses only the Python standard library:

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
node --check src/cvbench_studio/static/app.js
```

## Security boundary

Studio is intended for a trusted local machine. External adapter commands run
with the current user's permissions. Do not expose the server to an untrusted
network or run commands from an untrusted project.
