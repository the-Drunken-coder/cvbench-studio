from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from typing import Any

from .core import StudioError, load_project, project_dir, video_path

TRACK_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


class ModelQueue:
    """Runs explicit external commands without a shell.

    Adapters receive a video path and must write JSONL proposals to the output
    placeholder. Proposals are never promoted to annotations automatically.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._queue: Queue[tuple[str, str, list[str]]] = Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, daemon=True, name="cvbench-model-queue")
        self._worker.start()

    def submit(self, project_id: str, command: str | list[str]) -> dict[str, Any]:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise StudioError("model command must contain an executable")
        job_id = uuid.uuid4().hex
        job = {
            "schema_version": "cvbench.model-job/v1",
            "id": job_id,
            "project_id": project_id,
            "command": argv,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "error": None,
        }
        self._write(project_id, job)
        self._queue.put((project_id, job_id, argv))
        return job

    def list(self, project_id: str) -> list[dict[str, Any]]:
        root = project_dir(self.data_dir, project_id) / "jobs"
        if not root.is_dir():
            return []
        return sorted((json.loads(path.read_text()) for path in root.glob("*.json")), key=lambda x: x["created_at"])

    def wait(self, project_id: str, job_id: str) -> dict[str, Any]:
        self._queue.join()
        return self.get(project_id, job_id)

    def get(self, project_id: str, job_id: str) -> dict[str, Any]:
        path = self._job_path(project_id, job_id)
        if not path.is_file():
            raise FileNotFoundError(job_id)
        return json.loads(path.read_text())

    def output_path(self, project_id: str, job_id: str) -> Path:
        return project_dir(self.data_dir, project_id) / "jobs" / f"{job_id}.jsonl"

    def proposals(self, project_id: str, job_id: str) -> dict[str, Any]:
        job = self.get(project_id, job_id)
        if job["status"] != "completed":
            raise StudioError("only completed jobs can be imported")
        if not job.get("model"):
            raise StudioError("adapter output lacks required model provenance")
        project = load_project(self.data_dir, project_id)
        video = project.get("video")
        if not video:
            raise StudioError("project has no video")
        rows = []
        for line_number, line in enumerate(self.output_path(project_id, job_id).read_text().splitlines(), 1):
            if not line:
                continue
            row = json.loads(line)
            if row.get("schema_version") != "cvbench.model-proposal/v1":
                continue
            frame = row.get("frame")
            track_id = row.get("track_id")
            class_id = row.get("class_id")
            box = row.get("bbox_xyxy")
            confidence = row.get("confidence")
            if not isinstance(frame, int) or frame < 0 or frame >= video["frame_count"]:
                raise StudioError(f"proposal line {line_number} has an invalid frame")
            if not isinstance(track_id, str) or not TRACK_ID.fullmatch(track_id):
                raise StudioError(f"proposal line {line_number} has an invalid track_id")
            if class_id not in project["classes"]:
                raise StudioError(f"proposal line {line_number} has an unsupported class")
            if (
                not isinstance(box, list)
                or len(box) != 4
                or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in box)
            ):
                raise StudioError(f"proposal line {line_number} has an invalid bbox_xyxy")
            x1, y1, x2, y2 = box
            if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1 or x2 > video["width"] or y2 > video["height"]:
                raise StudioError(f"proposal line {line_number} has out-of-bounds geometry")
            if confidence is not None and (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise StudioError(f"proposal line {line_number} has an invalid confidence")
            rows.append(row)
        track_map: dict[str, dict[str, Any]] = {}
        boxes = []
        seen = set()
        colors = ["#a7f36b", "#72d8ff", "#ffbe6b", "#d18cff", "#ff7f73", "#71e6c1"]
        for row in rows:
            original = row["track_id"]
            safe = re.sub(r"[^A-Za-z0-9._-]+", "-", original)[:40]
            imported_id = f"proposal-{job_id[:8]}-{safe}"[:63]
            track_map.setdefault(
                imported_id,
                {
                    "id": imported_id,
                    "class_id": row["class_id"],
                    "name": f"Proposal {original}",
                    "color": colors[len(track_map) % len(colors)],
                    "label_origin": {"kind": "model_generated", "model_run_ids": [job_id]},
                },
            )
            if track_map[imported_id]["class_id"] != row["class_id"]:
                raise StudioError(f"proposal track {original} crosses class boundaries")
            key = (imported_id, row["frame"])
            if key in seen:
                raise StudioError(f"proposal track {original} has duplicate frame {row['frame']}")
            seen.add(key)
            imported_box = {
                "frame": row["frame"],
                "track_id": imported_id,
                "bbox_xyxy": [round(float(value), 3) for value in row["bbox_xyxy"]],
            }
            if row.get("confidence") is not None:
                imported_box["confidence"] = round(float(row["confidence"]), 6)
            boxes.append(imported_box)
        return {
            "job_id": job_id,
            "tracks": list(track_map.values()),
            "boxes": sorted(boxes, key=lambda item: (item["frame"], item["track_id"])),
            "summary": {
                "tracks": len(track_map),
                "boxes": len(boxes),
                "classes": sorted({row["class_id"] for row in rows}),
                "status": "draft_model_proposals",
            },
        }

    def _job_path(self, project_id: str, job_id: str) -> Path:
        if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
            raise StudioError("invalid job id")
        return project_dir(self.data_dir, project_id) / "jobs" / f"{job_id}.json"

    def _write(self, project_id: str, job: dict[str, Any]) -> None:
        path = self._job_path(project_id, job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)

    def _run(self) -> None:
        while True:
            project_id, job_id, argv = self._queue.get()
            try:
                self._execute(project_id, job_id, argv)
            finally:
                self._queue.task_done()

    def _execute(self, project_id: str, job_id: str, argv: list[str]) -> None:
        job = self.get(project_id, job_id)
        job["status"] = "running"
        job["started_at"] = _now()
        self._write(project_id, job)
        output = self.output_path(project_id, job_id)
        replacements = {
            "{video}": str(video_path(self.data_dir, project_id)),
            "{output}": str(output),
            "{project}": str(project_dir(self.data_dir, project_id)),
        }
        expanded = [replacements.get(part, part) for part in argv]
        environment = os.environ.copy()
        environment.update(
            CVBENCH_STUDIO_PROJECT=project_id,
            CVBENCH_STUDIO_VIDEO=replacements["{video}"],
            CVBENCH_STUDIO_OUTPUT=replacements["{output}"],
        )
        try:
            completed = subprocess.run(
                expanded,
                cwd=project_dir(self.data_dir, project_id),
                env=environment,
                capture_output=True,
                text=True,
                timeout=24 * 60 * 60,
                check=False,
            )
            job["returncode"] = completed.returncode
            job["stdout"] = completed.stdout[-20_000:]
            job["stderr"] = completed.stderr[-20_000:]
            if completed.returncode:
                job["status"] = "failed"
                job["error"] = f"adapter exited with status {completed.returncode}"
            elif not output.is_file():
                job["status"] = "failed"
                job["error"] = "adapter did not create {output}"
            else:
                model = None
                for line_number, line in enumerate(output.read_text().splitlines(), 1):
                    if line:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError as exc:
                            raise StudioError(f"invalid proposal JSONL on line {line_number}") from exc
                        if row.get("schema_version") in {
                            "cvbench.model-output/v1",
                            "cvbench.model-proposal/v1",
                        }:
                            candidate_model = row.get("model")
                            self._validate_model(candidate_model, line_number)
                            if model is not None and candidate_model != model:
                                raise StudioError("proposal rows contain inconsistent model provenance")
                            model = candidate_model
                digest = hashlib.sha256()
                with output.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                job["raw_output_sha256"] = digest.hexdigest()
                job["model"] = model
                job["status"] = "completed"
        except Exception as exc:  # noqa: BLE001 - adapter failures become durable job state
            job["status"] = "failed"
            job["error"] = str(exc)
        job["finished_at"] = _now()
        self._write(project_id, job)

    @staticmethod
    def _validate_model(model: Any, line_number: int) -> None:
        required = {
            "name", "version", "weights_uri", "weights_sha256",
            "code_revision", "config_sha256", "license",
        }
        if not isinstance(model, dict) or not required.issubset(model):
            raise StudioError(f"adapter output line {line_number} lacks complete model provenance")
        if not SHA256.fullmatch(str(model["weights_sha256"])) or not SHA256.fullmatch(str(model["config_sha256"])):
            raise StudioError(f"adapter output line {line_number} has an invalid provenance hash")
        if len(str(model["code_revision"])) < 7:
            raise StudioError(f"adapter output line {line_number} has an invalid code revision")
        license_value = model["license"]
        if (
            not isinstance(license_value, dict)
            or not re.fullmatch(r"[A-Za-z0-9.+-]{2,80}", str(license_value.get("spdx", "")))
            or not str(license_value.get("url", "")).strip()
        ):
            raise StudioError(f"adapter output line {line_number} has invalid model license provenance")
