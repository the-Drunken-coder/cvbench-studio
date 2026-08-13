from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from itertools import islice
from pathlib import Path
from unittest.mock import patch

from cvbench_studio.core import (
    StudioError,
    create_project,
    export_project,
    extend_video_frame_count,
    import_video,
    list_projects,
    load_annotations,
    load_project,
    save_annotations,
    save_source_metadata,
    snapshot_video,
    validate_annotations,
    video_path,
)
from cvbench_studio.sampling import (
    iter_sample_frame_indices,
    probability,
    sample_frame_indices,
    stride_aligned_size,
)


class CoreTests(unittest.TestCase):
    def test_sample_frame_indices_are_deterministic_and_unique(self):
        self.assertEqual(sample_frame_indices(300, 30.0, 5.0), list(range(0, 300, 6)))
        self.assertEqual(sample_frame_indices(2, 30.0, 5.0), [0])
        self.assertEqual(list(islice(iter_sample_frame_indices(30.0, 5.0), 5)), [0, 6, 12, 18, 24])
        self.assertEqual(list(islice(iter_sample_frame_indices(30.0, 60.0), 5)), [0, 1, 2, 3, 4])

    def test_sampling_rejects_non_finite_rates_and_caps_oversampling(self):
        for invalid in (float("inf"), float("nan"), 0.0, -1.0):
            with self.subTest(invalid=invalid):
                with self.assertRaises(ValueError):
                    next(iter_sample_frame_indices(30.0, invalid))
                with self.assertRaises(ValueError):
                    sample_frame_indices(30, 30.0, invalid)
        self.assertEqual(list(islice(iter_sample_frame_indices(30.0, 1_000_000.0), 5)), [0, 1, 2, 3, 4])

    def test_model_input_size_requires_stride_alignment(self):
        self.assertEqual(stride_aligned_size("640"), 640)
        for invalid in ("0", "-32", "641"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                stride_aligned_size(invalid)
        for invalid_stride in (0, -32, False):
            with self.subTest(invalid_stride=invalid_stride), self.assertRaises(ValueError):
                stride_aligned_size("640", invalid_stride)

    def test_adapter_thresholds_require_finite_probabilities(self):
        self.assertEqual(probability("0"), 0.0)
        self.assertEqual(probability("1"), 1.0)
        for invalid in ("nan", "inf", "-0.1", "1.1"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                probability(invalid)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.project = create_project(self.data, "Game clip", ["person", "sports_ball"])
        self.source = self.data / "source.mp4"
        self.source.write_bytes(b"fake-video-one")
        self.project = import_video(
            self.data,
            self.project["id"],
            self.source,
            "match.mp4",
            width=640,
            height=360,
            duration=2,
            fps=30,
        )
        self.project = save_source_metadata(
            self.data,
            self.project["id"],
            {
                "title": "Synthetic match",
                "uri": "synthetic://studio/test",
                "license_spdx": "MIT",
                "license_name": "MIT License",
                "license_url": "https://opensource.org/license/mit",
                "license_text": "MIT test license",
            },
        )

    def tearDown(self):
        self.temporary.cleanup()

    def annotations(self):
        return {
            "schema_version": "cvbench.studio-annotations/v1",
            "tracks": [
                {"id": "ball-1", "class_id": "sports_ball", "name": "Ball", "color": "#a7f36b"}
            ],
            "boxes": [
                {"frame": 2, "track_id": "ball-1", "bbox_xyxy": [10, 20, 30, 40]},
                {"frame": 3, "track_id": "ball-1", "bbox_xyxy": [12, 20, 32, 40]},
            ],
        }

    def test_create_import_list_and_atomic_replace(self):
        self.assertEqual(list_projects(self.data)[0]["id"], self.project["id"])
        replacement = self.data / "replacement.mov"
        replacement.write_bytes(b"fake-video-two")
        updated = import_video(
            self.data,
            self.project["id"],
            replacement,
            "replacement.mov",
            width=1280,
            height=720,
            duration=3,
            fps=24,
        )
        self.assertEqual(video_path(self.data, self.project["id"]).name, "replacement.mov")
        self.assertEqual(video_path(self.data, self.project["id"]).read_bytes(), b"fake-video-two")
        self.assertEqual(updated["video"]["frame_count"], 72)
        self.assertEqual([path.name for path in video_path(self.data, self.project["id"]).parent.iterdir()], ["replacement.mov"])

    def test_failed_same_name_import_preserves_previous_video(self):
        replacement = self.data / "replacement.mp4"
        replacement.write_bytes(b"replacement")
        observed_during_commit = []

        def fail_metadata_write(*_args):
            observed_during_commit.append(video_path(self.data, self.project["id"]).read_bytes())
            raise OSError("disk full")

        with (
            patch("cvbench_studio.core._write_json", side_effect=fail_metadata_write),
            self.assertRaisesRegex(OSError, "disk full"),
        ):
            import_video(
                self.data,
                self.project["id"],
                replacement,
                "match.mp4",
                width=1280,
                height=720,
                duration=3,
                fps=24,
            )
        self.assertEqual(observed_during_commit, [b"replacement"])
        self.assertEqual(load_project(self.data, self.project["id"])["video"], self.project["video"])
        self.assertEqual(video_path(self.data, self.project["id"]).read_bytes(), b"fake-video-one")

    def test_model_snapshot_does_not_alias_project_video(self):
        _, snapshot = snapshot_video(self.data, self.project["id"], self.data / "snapshot")
        snapshot.chmod(0o644)
        snapshot.write_bytes(b"mutated")
        self.assertEqual(video_path(self.data, self.project["id"]).read_bytes(), b"fake-video-one")

    def test_frame_count_extension_requires_the_expected_source_video(self):
        with self.assertRaisesRegex(StudioError, "different source video"):
            extend_video_frame_count(
                self.data,
                self.project["id"],
                100,
                expected_video_sha256="0" * 64,
            )
        self.assertEqual(load_project(self.data, self.project["id"])["video"]["frame_count"], 60)

    def test_metadata_saves_preserve_extended_frame_count(self):
        digest = self.project["video"]["sha256"]
        extend_video_frame_count(
            self.data,
            self.project["id"],
            100,
            expected_video_sha256=digest,
        )
        save_source_metadata(
            self.data,
            self.project["id"],
            {
                "title": "Updated source",
                "uri": "synthetic://studio/updated",
                "license_spdx": "MIT",
                "license_name": "MIT License",
                "license_url": "https://opensource.org/license/mit",
                "license_text": "MIT",
            },
        )
        save_annotations(self.data, self.project["id"], self.annotations())
        self.assertEqual(load_project(self.data, self.project["id"])["video"]["frame_count"], 100)

    def test_validation_rejects_duplicate_and_out_of_bounds_boxes(self):
        annotations = self.annotations()
        annotations["boxes"].append(
            {"frame": 2, "track_id": "ball-1", "bbox_xyxy": [600, 20, 700, 40]}
        )
        result = validate_annotations(self.project, annotations)
        self.assertFalse(result["valid"])
        self.assertTrue(any("duplicate box" in error for error in result["errors"]))
        self.assertTrue(any("video bounds" in error for error in result["errors"]))
        with self.assertRaises(StudioError):
            save_annotations(self.data, self.project["id"], annotations)

    def test_validation_rejects_invalid_optional_confidence(self):
        for confidence in (True, False, None):
            with self.subTest(confidence=confidence):
                annotations = self.annotations()
                annotations["boxes"][0]["confidence"] = confidence
                result = validate_annotations(self.project, annotations)
                self.assertFalse(result["valid"])
                self.assertTrue(any("invalid confidence" in error for error in result["errors"]))

    def test_save_and_export_contract(self):
        annotations = self.annotations()
        result = save_annotations(self.data, self.project["id"], annotations)
        self.assertTrue(result["valid"])
        self.assertEqual(load_annotations(self.data, self.project["id"])["boxes"][0]["frame"], 2)
        output = self.data / "clip.cvbench.zip"
        export_project(self.data, self.project["id"], output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {
                    "contribution.json",
                    f"clips/{self.project['id']}/video.mp4",
                    f"clips/{self.project['id']}/tracks.jsonl",
                    f"clips/{self.project['id']}/source.json",
                    f"clips/{self.project['id']}/review.jsonl",
                    "licenses/MIT.txt",
                },
            )
            root = f"clips/{self.project['id']}"
            source = json.loads(archive.read(f"{root}/source.json"))
            self.assertEqual(source["media"]["fps_numerator"], 30)
            rows = [json.loads(line) for line in archive.read(f"{root}/tracks.jsonl").splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0]["schema_version"], "cvbench.track-annotation/v1")
            self.assertEqual(rows[0]["frame_index"], 2)
            self.assertEqual(rows[0]["label_origin"]["kind"], "human")
            self.assertEqual(archive.read(f"{root}/review.jsonl"), b"")
            contribution = json.loads(archive.read("contribution.json"))
            self.assertEqual(contribution["clip_path"], root)

    def test_project_id_cannot_escape_data_directory(self):
        with self.assertRaises(StudioError):
            load_project(self.data, "../escape")
