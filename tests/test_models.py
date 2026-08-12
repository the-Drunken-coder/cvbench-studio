from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from cvbench_studio.core import (
    StudioError,
    create_project,
    export_project,
    import_video,
    save_annotations,
    save_source_metadata,
)
from cvbench_studio.models import ModelQueue


class ModelQueueTests(unittest.TestCase):
    @staticmethod
    def _wait(queue, project_id, job_id):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = queue.get(project_id, job_id)
            if job["status"] in {"completed", "failed"}:
                return job
            time.sleep(0.02)
        return queue.get(project_id, job_id)

    def test_external_adapter_writes_separate_proposals(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Adapter")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(
                data, project["id"], video, "clip.mp4",
                width=10, height=10, duration=1, fps=1,
            )
            queue = ModelQueue(data)
            proposal = {
                "schema_version": "cvbench.model-proposal/v1",
                "frame": 0,
                "track_id": "person-1",
                "class_id": "person",
                "bbox_xyxy": [1, 1, 5, 8],
                "confidence": 0.75,
                "model": {
                    "name": "fixture",
                    "version": "1",
                    "weights_uri": "synthetic://weights",
                    "weights_sha256": "0" * 64,
                    "code_revision": "1234567",
                    "config_sha256": "1" * 64,
                    "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
                },
            }
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            job = queue.submit(
                project["id"],
                [sys.executable, "-c", script, "{output}", json.dumps(proposal, separators=(",", ":"))],
            )
            job = self._wait(queue, project["id"], job["id"])
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(json.loads(queue.output_path(project["id"], job["id"]).read_text())["frame"], 0)
            imported = queue.proposals(project["id"], job["id"])
            self.assertEqual(imported["summary"]["boxes"], 1)
            self.assertEqual(imported["boxes"][0]["confidence"], 0.75)
            self.assertEqual(imported["tracks"][0]["label_origin"]["kind"], "model_generated")
            save_annotations(
                data,
                project["id"],
                {
                    "schema_version": "cvbench.studio-annotations/v1",
                    "tracks": imported["tracks"],
                    "boxes": imported["boxes"],
                },
            )
            save_source_metadata(
                data,
                project["id"],
                {
                    "title": "Model fixture",
                    "uri": "synthetic://model-fixture",
                    "license_spdx": "MIT",
                    "license_name": "MIT License",
                    "license_url": "https://opensource.org/license/mit",
                    "license_text": "MIT",
                },
            )
            output = data / "model-contribution.zip"
            export_project(data, project["id"], output)
            with zipfile.ZipFile(output) as archive:
                source_path = f"clips/{project['id']}/source.json"
                source = json.loads(archive.read(source_path))
                self.assertEqual(source["model_runs"][0]["run_id"], job["id"])
                tracks_path = f"clips/{project['id']}/tracks.jsonl"
                row = json.loads(archive.read(tracks_path))
                self.assertEqual(row["label_origin"]["model_run_ids"], [job["id"]])
                self.assertEqual(row["confidence"], 0.75)

    def test_empty_adapter_output_retains_model_provenance(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "No detections")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=1, fps=1)
            model = {
                "name": "fixture",
                "version": "1",
                "weights_uri": "synthetic://weights",
                "weights_sha256": "0" * 64,
                "code_revision": "1234567",
                "config_sha256": "1" * 64,
                "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
            }
            metadata = {"schema_version": "cvbench.model-output/v1", "model": model}
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"],
                [sys.executable, "-c", script, "{output}", json.dumps(metadata, separators=(",", ":"))],
            )
            job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["model"], model)
            self.assertEqual(queue.proposals(project["id"], job["id"])["summary"]["boxes"], 0)

    def test_proposals_accept_missing_confidence_and_reject_invalid_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Confidence")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=1, fps=1)
            model = {
                "name": "fixture",
                "version": "1",
                "weights_uri": "synthetic://weights",
                "weights_sha256": "0" * 64,
                "code_revision": "1234567",
                "config_sha256": "1" * 64,
                "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
            }
            proposal = {
                "schema_version": "cvbench.model-proposal/v1",
                "frame": 0,
                "track_id": "person-1",
                "class_id": "person",
                "bbox_xyxy": [1, 1, 5, 8],
                "model": model,
            }
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"],
                [sys.executable, "-c", script, "{output}", json.dumps(proposal, separators=(",", ":"))],
            )
            job = self._wait(queue, project["id"], submitted["id"])
            imported = queue.proposals(project["id"], job["id"])
            self.assertNotIn("confidence", imported["boxes"][0])

            for confidence in (True, False, None):
                with self.subTest(confidence=confidence):
                    proposal["confidence"] = confidence
                    submitted = queue.submit(
                        project["id"],
                        [sys.executable, "-c", script, "{output}", json.dumps(proposal, separators=(",", ":"))],
                    )
                    job = self._wait(queue, project["id"], submitted["id"])
                    self.assertEqual(job["status"], "completed", job)
                    with self.assertRaises(StudioError):
                        queue.proposals(project["id"], job["id"])
