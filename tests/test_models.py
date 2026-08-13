from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from cvbench_studio.core import (
    StudioError,
    _protect_model_snapshot,
    create_project,
    export_project,
    import_video,
    load_project,
    save_annotations,
    save_source_metadata,
    video_path,
)
from cvbench_studio.models import WINDOWS_CREATE_SUSPENDED, ModelQueue, _start_adapter_process


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

    def test_startup_fails_interrupted_jobs_and_removes_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Interrupted adapter")
            jobs = data / "projects" / project["id"] / "jobs"
            jobs.mkdir()
            job_id = "a" * 32
            snapshot = jobs / f"{job_id}.input.mp4"
            snapshot.write_bytes(b"video snapshot")
            job = {
                "schema_version": "cvbench.model-job/v1",
                "id": job_id,
                "project_id": project["id"],
                "input_filename": snapshot.name,
                "status": "running",
                "created_at": "2026-01-01T00:00:00+00:00",
                "started_at": "2026-01-01T00:00:01+00:00",
                "finished_at": None,
                "error": None,
            }
            (jobs / f"{job_id}.json").write_text(json.dumps(job))
            queue = ModelQueue(data)
            recovered = queue.get(project["id"], job_id)
            self.assertEqual(recovered["status"], "failed")
            self.assertIn("restarted", recovered["error"])
            self.assertIsNotNone(recovered["finished_at"])
            self.assertFalse(snapshot.exists())
            queue.close()

    def test_startup_fails_legacy_interrupted_jobs_without_snapshot_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Legacy interrupted adapter")
            jobs = data / "projects" / project["id"] / "jobs"
            jobs.mkdir()
            job_id = "c" * 32
            job = {
                "schema_version": "cvbench.model-job/v1",
                "id": job_id,
                "project_id": project["id"],
                "status": "queued",
                "created_at": "2026-01-01T00:00:00+00:00",
                "finished_at": None,
                "error": None,
            }
            (jobs / f"{job_id}.json").write_text(json.dumps(job))
            queue = ModelQueue(data)
            recovered = queue.get(project["id"], job_id)
            self.assertEqual(recovered["status"], "failed")
            self.assertIn("restarted", recovered["error"])
            queue.close()

    def test_second_live_queue_cannot_recover_owned_jobs(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            queue = ModelQueue(data)
            try:
                with self.assertRaisesRegex(StudioError, "another model queue"):
                    ModelQueue(data)
            finally:
                queue.close()

    def test_startup_skips_non_object_job_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Malformed job")
            jobs = data / "projects" / project["id"] / "jobs"
            jobs.mkdir()
            (jobs / f"{'b' * 32}.json").write_text("[]\n")
            queue = ModelQueue(data)
            queue.close()

    def test_model_identity_provenance_requires_non_empty_strings(self):
        model = {
            "name": "fixture",
            "version": "1",
            "weights_uri": "synthetic://weights",
            "weights_sha256": "0" * 64,
            "code_revision": "1234567",
            "config_sha256": "1" * 64,
            "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
        }
        for key, invalid in (("name", ""), ("version", "   "), ("weights_uri", None)):
            with self.subTest(key=key, invalid=invalid):
                candidate = {**model, key: invalid}
                with self.assertRaisesRegex(StudioError, "invalid model identity provenance"):
                    ModelQueue._validate_model(candidate, 1)

    def test_model_hash_provenance_requires_strings(self):
        model = {
            "name": "fixture",
            "version": "1",
            "weights_uri": "synthetic://weights",
            "weights_sha256": "0" * 64,
            "code_revision": "1234567",
            "config_sha256": "1" * 64,
            "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
        }
        for key in ("weights_sha256", "config_sha256"):
            with self.subTest(key=key):
                candidate = {**model, key: int(model[key])}
                with self.assertRaisesRegex(StudioError, "invalid provenance hash"):
                    ModelQueue._validate_model(candidate, 1)

    def test_model_revision_and_license_provenance_require_strings(self):
        model = {
            "name": "fixture",
            "version": "1",
            "weights_uri": "synthetic://weights",
            "weights_sha256": "0" * 64,
            "code_revision": "1234567",
            "config_sha256": "1" * 64,
            "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
        }
        with self.assertRaisesRegex(StudioError, "invalid code revision"):
            ModelQueue._validate_model({**model, "code_revision": 1234567}, 1)
        for license_value in (
            {"spdx": None, "url": "https://example.invalid"},
            {"spdx": "MIT", "url": None},
        ):
            with (
                self.subTest(license=license_value),
                self.assertRaisesRegex(StudioError, "invalid model license provenance"),
            ):
                ModelQueue._validate_model({**model, "license": license_value}, 1)

    def test_windows_model_snapshot_remains_writable_for_cleanup(self):
        snapshot = Path("snapshot.mp4")
        with (
            patch("cvbench_studio.core.os.name", "nt"),
            patch.object(Path, "chmod") as chmod,
        ):
            _protect_model_snapshot(snapshot)
        chmod.assert_not_called()

    def test_windows_stop_closes_the_process_tree_job(self):
        queue = object.__new__(ModelQueue)
        queue._process_lock = threading.Lock()
        queue._active_process = Mock()
        queue._active_windows_job = 123
        with (
            patch("cvbench_studio.models.os.name", "nt"),
            patch("cvbench_studio.models._close_windows_job") as close_job,
        ):
            queue._stop_active_process(kill=False)
        close_job.assert_called_once_with(123)
        self.assertIsNone(queue._active_windows_job)
        queue._active_process.terminate.assert_not_called()

    def test_windows_adapter_is_supervised_before_it_resumes(self):
        process = Mock(pid=123, stdout=None, stderr=None)
        events = []
        with (
            patch("cvbench_studio.models.os.name", "nt"),
            patch("cvbench_studio.models.subprocess.Popen", return_value=process) as popen,
            patch(
                "cvbench_studio.models._assign_windows_job",
                side_effect=lambda candidate: events.append(("assign", candidate)) or 456,
            ),
            patch(
                "cvbench_studio.models._resume_windows_process",
                side_effect=lambda candidate: events.append(("resume", candidate)),
            ),
        ):
            started, windows_job = _start_adapter_process(
                ["adapter"],
                cwd=Path("project"),
                environment={"CVD": "1"},
            )
        self.assertIs(started, process)
        self.assertEqual(windows_job, 456)
        self.assertEqual(events, [("assign", process), ("resume", process)])
        self.assertEqual(popen.call_args.kwargs["creationflags"], WINDOWS_CREATE_SUSPENDED)

    def test_close_terminates_running_adapter_and_persists_interruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Shutdown adapter")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=1, fps=1)
            marker = data / "adapter-started"
            script = (
                "import pathlib,subprocess,sys,time; "
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                "pathlib.Path(sys.argv[1]).write_text('started'); "
                "time.sleep(60)"
            )
            queue = ModelQueue(data)
            submitted = queue.submit(project["id"], [sys.executable, "-c", script, str(marker)])
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            started = time.monotonic()
            queue.close()
            self.assertLess(time.monotonic() - started, 4)
            job = queue.get(project["id"], submitted["id"])
            self.assertEqual(job["status"], "failed")
            self.assertIn("closed", job["error"])
            with self.assertRaisesRegex(StudioError, "queue is closed"):
                queue.submit(project["id"], [sys.executable, "-c", "pass"])
            replacement = ModelQueue(data)
            replacement.close()

    def test_proposals_reject_output_changed_after_adapter_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Tampered output")
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
            payload = json.dumps({"schema_version": "cvbench.model-output/v1", "model": model})
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"],
                [sys.executable, "-c", script, "{output}", payload],
            )
            job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "completed", job)
            queue.output_path(project["id"], job["id"]).write_text(payload + "\n\n")
            with self.assertRaisesRegex(StudioError, "changed after adapter execution"):
                queue.proposals(project["id"], job["id"])

    def test_completion_metadata_and_digest_use_the_same_output_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Bound output")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=1, fps=1)
            original_model = {
                "name": "original",
                "version": "1",
                "weights_uri": "synthetic://original",
                "weights_sha256": "0" * 64,
                "code_revision": "1234567",
                "config_sha256": "1" * 64,
                "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
            }
            replacement_model = {**original_model, "name": "replacement"}
            original_body = (
                json.dumps(
                    {"schema_version": "cvbench.model-output/v1", "model": original_model},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            replacement_body = (
                json.dumps(
                    {"schema_version": "cvbench.model-output/v1", "model": replacement_model},
                    separators=(",", ":"),
                )
                + "\n"
            ).encode()
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(bytes.fromhex(sys.argv[2]))"
            queue = ModelQueue(data)
            real_read_bytes = Path.read_bytes
            replaced = False

            def read_then_replace(path: Path) -> bytes:
                nonlocal replaced
                body = real_read_bytes(path)
                if path.suffix == ".jsonl" and not replaced:
                    path.write_bytes(replacement_body)
                    replaced = True
                return body

            with patch.object(Path, "read_bytes", read_then_replace):
                submitted = queue.submit(
                    project["id"],
                    [sys.executable, "-c", script, "{output}", original_body.hex()],
                )
                job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["model"], original_model)
            self.assertEqual(job["raw_output_sha256"], hashlib.sha256(original_body).hexdigest())
            with self.assertRaisesRegex(StudioError, "changed after adapter execution"):
                queue.proposals(project["id"], job["id"])

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
                "confidence": 0.123456789,
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
            self.assertEqual(queue.input_path(project["id"], job["id"]).suffix, ".mp4")
            self.assertEqual(json.loads(queue.output_path(project["id"], job["id"]).read_text())["frame"], 0)
            imported = queue.proposals(project["id"], job["id"])
            self.assertEqual(imported["summary"]["boxes"], 1)
            self.assertEqual(imported["boxes"][0]["confidence"], 0.123456789)
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
                self.assertEqual(row["confidence"], 0.123456789)

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

    def test_completed_job_cannot_import_into_replaced_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Replacement")
            video = data / "first.mp4"
            video.write_bytes(b"first")
            import_video(data, project["id"], video, "first.mp4", width=10, height=10, duration=1, fps=1)
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
            replacement = data / "second.mp4"
            replacement.write_bytes(b"second")
            import_video(
                data, project["id"], replacement, "second.mp4", width=10, height=10, duration=2, fps=1
            )
            with self.assertRaisesRegex(StudioError, "different source video"):
                queue.proposals(project["id"], job["id"])

    def test_unchanged_frame_count_rechecks_source_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Same-length replacement")
            first = data / "first.mp4"
            first.write_bytes(b"first")
            import_video(data, project["id"], first, "first.mp4", width=10, height=10, duration=1, fps=1)
            model = {
                "name": "fixture",
                "version": "1",
                "weights_uri": "synthetic://weights",
                "weights_sha256": "0" * 64,
                "code_revision": "1234567",
                "config_sha256": "1" * 64,
                "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
            }
            payload = json.dumps({"schema_version": "cvbench.model-output/v1", "model": model})
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"], [sys.executable, "-c", script, "{output}", payload]
            )
            job = self._wait(queue, project["id"], submitted["id"])
            second = data / "second.mp4"
            second.write_bytes(b"second")

            load_original = load_project
            load_calls = 0

            def replace_after_initial_check(data_dir, project_id):
                nonlocal load_calls
                loaded = load_original(data_dir, project_id)
                load_calls += 1
                if load_calls == 1:
                    import_video(
                        data,
                        project["id"],
                        second,
                        "second.mp4",
                        width=10,
                        height=10,
                        duration=1,
                        fps=1,
                    )
                return loaded

            with (
                patch("cvbench_studio.models.load_project", side_effect=replace_after_initial_check),
                self.assertRaisesRegex(StudioError, "different source video"),
            ):
                queue.proposals(project["id"], job["id"])

    def test_queued_job_runs_against_submitted_video_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Queued source")
            first = data / "first.mp4"
            first.write_bytes(b"first")
            import_video(data, project["id"], first, "first.webm", width=10, height=10, duration=1, fps=1)
            model = {
                "name": "fixture",
                "version": "1",
                "weights_uri": "synthetic://weights",
                "weights_sha256": "0" * 64,
                "code_revision": "1234567",
                "config_sha256": "1" * 64,
                "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
            }
            metadata = json.dumps(
                {"schema_version": "cvbench.model-output/v1", "model": model},
                separators=(",", ":"),
            )
            queue = ModelQueue(data)
            blocker_script = (
                "import pathlib,sys,time; time.sleep(.2); "
                "pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            )
            blocker = queue.submit(
                project["id"], [sys.executable, "-c", blocker_script, "{output}", metadata]
            )
            snapshot_script = (
                "import pathlib,sys; video=pathlib.Path(sys.argv[2]).read_bytes(); "
                "assert video == b'first'; pathlib.Path(sys.argv[1]).write_text(sys.argv[3] + '\\n')"
            )
            submitted = queue.submit(
                project["id"],
                [sys.executable, "-c", snapshot_script, "{output}", "{video}", metadata],
            )
            second = data / "second.mp4"
            second.write_bytes(b"second")
            import_video(data, project["id"], second, "second.mp4", width=10, height=10, duration=1, fps=1)
            self.assertEqual(self._wait(queue, project["id"], blocker["id"])["status"], "completed")
            job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["input_filename"].split(".")[-1], "webm")
            self.assertFalse(queue.input_path(project["id"], submitted["id"]).exists())

    def test_job_fails_if_adapter_mutates_its_input_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Mutating adapter")
            video = data / "clip.mp4"
            video.write_bytes(b"original")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=1, fps=1)
            script = (
                "import pathlib,sys; video=pathlib.Path(sys.argv[1]); video.chmod(0o644); "
                "video.write_bytes(b'mutated'); pathlib.Path(sys.argv[2]).write_text('{}\\n')"
            )
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"], [sys.executable, "-c", script, "{video}", "{output}"]
            )
            job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "failed")
            self.assertIn("changed during adapter execution", job["error"])
            self.assertFalse(queue.input_path(project["id"], submitted["id"]).exists())
            self.assertEqual(video_path(data, project["id"]).read_bytes(), b"original")

    def test_tail_proposal_extends_underreported_project_frame_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Underreported")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=1, fps=1)
            proposal = {
                "schema_version": "cvbench.model-proposal/v1",
                "frame": 1,
                "track_id": "person-1",
                "class_id": "person",
                "bbox_xyxy": [1, 1, 5, 8],
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
            metadata = {
                "schema_version": "cvbench.model-output/v1",
                "model": proposal["model"],
                "decoded_frame_count": 2,
            }
            payload = "\n".join(
                [
                    json.dumps({"schema_version": "cvbench.model-output/v1", "model": proposal["model"]}),
                    json.dumps(proposal, separators=(",", ":")),
                    json.dumps(metadata, separators=(",", ":")),
                ]
            )
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"],
                [sys.executable, "-c", script, "{output}", payload],
            )
            job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "completed", job)
            self.assertEqual(job["decoded_frame_count"], 2)
            imported = queue.proposals(project["id"], job["id"])
            self.assertEqual(imported["frame_count"], 2)
            self.assertEqual(imported["boxes"][0]["frame"], 1)
            self.assertEqual(load_project(data, project["id"])["video"]["frame_count"], 2)

    def test_decoder_frame_count_bounds_proposals_when_browser_overreports(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Overreported")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=10, fps=1)
            model = {
                "name": "fixture",
                "version": "1",
                "weights_uri": "synthetic://weights",
                "weights_sha256": "0" * 64,
                "code_revision": "1234567",
                "config_sha256": "1" * 64,
                "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
            }
            rows = [
                {
                    "schema_version": "cvbench.model-proposal/v1",
                    "frame": 5,
                    "track_id": "person-1",
                    "class_id": "person",
                    "bbox_xyxy": [1, 1, 5, 8],
                    "model": model,
                },
                {
                    "schema_version": "cvbench.model-output/v1",
                    "model": model,
                    "decoded_frame_count": 2,
                },
            ]
            payload = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"], [sys.executable, "-c", script, "{output}", payload]
            )
            job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "completed", job)
            with self.assertRaisesRegex(StudioError, "invalid frame"):
                queue.proposals(project["id"], job["id"])

    def test_decoder_frame_count_corrects_browser_overreport(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Correct overreport")
            video = data / "clip.mp4"
            video.write_bytes(b"video")
            import_video(data, project["id"], video, "clip.mp4", width=10, height=10, duration=10, fps=1)
            model = {
                "name": "fixture",
                "version": "1",
                "weights_uri": "synthetic://weights",
                "weights_sha256": "0" * 64,
                "code_revision": "1234567",
                "config_sha256": "1" * 64,
                "license": {"spdx": "MIT", "url": "https://opensource.org/license/mit"},
            }
            rows = [
                {
                    "schema_version": "cvbench.model-proposal/v1",
                    "frame": 1,
                    "track_id": "person-1",
                    "class_id": "person",
                    "bbox_xyxy": [1, 1, 5, 8],
                    "model": model,
                },
                {
                    "schema_version": "cvbench.model-output/v1",
                    "model": model,
                    "decoded_frame_count": 2,
                },
            ]
            payload = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows)
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(
                project["id"], [sys.executable, "-c", script, "{output}", payload]
            )
            job = self._wait(queue, project["id"], submitted["id"])
            imported = queue.proposals(project["id"], job["id"])
            self.assertEqual(imported["frame_count"], 2)
            self.assertEqual(load_project(data, project["id"])["video"]["frame_count"], 2)

    def test_rejected_tail_proposals_do_not_change_project_frame_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            data = Path(temporary)
            project = create_project(data, "Rejected tail")
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
                "frame": 1,
                "track_id": "person-1",
                "class_id": "person",
                "bbox_xyxy": [1, 1, 5, 8],
                "model": model,
            }
            metadata = {
                "schema_version": "cvbench.model-output/v1",
                "model": model,
                "decoded_frame_count": 2,
            }
            payload = "\n".join(
                [
                    *(json.dumps(proposal, separators=(",", ":")) for _ in range(2)),
                    json.dumps(metadata, separators=(",", ":")),
                ]
            )
            script = "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text(sys.argv[2] + '\\n')"
            queue = ModelQueue(data)
            submitted = queue.submit(project["id"], [sys.executable, "-c", script, "{output}", payload])
            job = self._wait(queue, project["id"], submitted["id"])
            self.assertEqual(job["status"], "completed", job)
            with self.assertRaises(StudioError):
                queue.proposals(project["id"], job["id"])
            self.assertEqual(load_project(data, project["id"])["video"]["frame_count"], 1)

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
