# External model adapter protocol

Studio provides orchestration, provenance, and storage. It does not bundle a
detector or tracker.

An adapter command receives:

- `{video}`: the imported source video path.
- `{output}`: the required JSONL proposal path.
- `{project}`: the project directory.
- Matching `CVBENCH_STUDIO_VIDEO`, `CVBENCH_STUDIO_OUTPUT`, and
  `CVBENCH_STUDIO_PROJECT` environment variables.

Each non-empty output line must be valid JSON. The recommended proposal shape is:

```json
{
  "schema_version": "cvbench.model-proposal/v1",
  "frame": 42,
  "track_id": "proposal-7",
  "class_id": "sports_ball",
  "bbox_xyxy": [114.2, 85.1, 132.4, 104.9],
  "confidence": 0.83,
  "model": {
    "name": "your-model",
    "version": "2026-07",
    "weights_uri": "https://example.invalid/model.safetensors",
    "weights_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "code_revision": "abcdef123456",
    "config_sha256": "abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789",
    "license": {
      "spdx": "Apache-2.0",
      "url": "https://www.apache.org/licenses/LICENSE-2.0"
    }
  }
}
```

Every proposal row from one job must carry identical, complete model provenance.
An adapter should also emit a `cvbench.model-output/v1` metadata row with that
same `model` object before proposals. If it discovers the exact media length at
EOF, it should emit a final metadata row containing the same model plus a
positive integer `decoded_frame_count`. Studio uses that value to correct
underreported browser media metadata, including when decoded tail frames contain
no detections.
The queue validates JSON syntax, frame/class/geometry bounds, and provenance,
then records the exact command, timestamps, raw output hash, return code,
stdout, and stderr. A completed job can be explicitly imported into the
editable draft. It is never silently accepted as reviewed truth.

For a native Apple Silicon adapter, select MPS in the adapter itself:

```python
device = "mps" if torch.backends.mps.is_available() else "cpu"
```

The adapter owns its Python environment and dependencies. This keeps Studio
lightweight and prevents model framework conflicts.
