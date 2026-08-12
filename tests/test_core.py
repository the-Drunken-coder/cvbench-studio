from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from cvbench_studio.core import (
    StudioError,
    create_project,
    export_project,
    import_video,
    list_projects,
    load_annotations,
    load_project,
    save_annotations,
    save_source_metadata,
    validate_annotations,
    video_path,
)
from cvbench_studio.sampling import sample_frame_indices


class CoreTests(unittest.TestCase):
    def test_sample_frame_indices_are_deterministic_and_unique(self):
        self.assertEqual(sample_frame_indices(300, 30.0, 5.0), list(range(0, 300, 6)))
        self.assertEqual(sample_frame_indices(2, 30.0, 5.0), [0])

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
