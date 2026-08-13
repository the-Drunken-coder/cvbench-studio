from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SCHEMA = "cvbench.studio-annotations/v1"
DEFAULT_CLASSES = ["person", "vehicle", "dog", "sports_ball"]
PROJECT_WRITE_LOCK = threading.RLock()


class StudioError(ValueError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        temporary = Path(stream.name)
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug[:48] or "project").strip("-")


def project_dir(data_dir: Path, project_id: str) -> Path:
    if not PROJECT_ID.fullmatch(project_id):
        raise StudioError("invalid project id")
    path = data_dir.resolve() / "projects" / project_id
    if path.parent != (data_dir.resolve() / "projects"):
        raise StudioError("project escapes data directory")
    return path


def create_project(data_dir: Path, name: str, classes: list[str] | None = None) -> dict[str, Any]:
    name = name.strip()
    if not name:
        raise StudioError("project name is required")
    normalized_classes = [item.strip() for item in (classes or DEFAULT_CLASSES) if item.strip()]
    if not normalized_classes or len(set(normalized_classes)) != len(normalized_classes):
        raise StudioError("classes must be a non-empty unique list")
    project_id = f"{_slug(name)}-{uuid.uuid4().hex[:8]}"
    path = project_dir(data_dir, project_id)
    path.mkdir(parents=True)
    project = {
        "schema_version": "cvbench.studio-project/v1",
        "id": project_id,
        "name": name,
        "classes": normalized_classes,
        "created_at": _now(),
        "updated_at": _now(),
        "video": None,
        "source": None,
    }
    annotations = {"schema_version": SCHEMA, "tracks": [], "boxes": []}
    _write_json(path / "project.json", project)
    _write_json(path / "annotations.json", annotations)
    return project


def list_projects(data_dir: Path) -> list[dict[str, Any]]:
    root = data_dir / "projects"
    if not root.is_dir():
        return []
    projects = []
    for path in sorted(root.iterdir()):
        metadata = path / "project.json"
        if path.is_dir() and metadata.is_file():
            projects.append(_read_json(metadata))
    return sorted(projects, key=lambda item: item["updated_at"], reverse=True)


def load_project(data_dir: Path, project_id: str) -> dict[str, Any]:
    path = project_dir(data_dir, project_id) / "project.json"
    if not path.is_file():
        raise FileNotFoundError(project_id)
    return _read_json(path)


def load_annotations(data_dir: Path, project_id: str) -> dict[str, Any]:
    path = project_dir(data_dir, project_id) / "annotations.json"
    if not path.is_file():
        raise FileNotFoundError(project_id)
    return _read_json(path)


def save_source_metadata(data_dir: Path, project_id: str, source: dict[str, Any]) -> dict[str, Any]:
    required = ["title", "uri", "license_spdx", "license_name", "license_url", "license_text"]
    normalized = {key: str(source.get(key, "")).strip() for key in required}
    missing = [key for key, value in normalized.items() if not value]
    if missing:
        raise StudioError(f"source metadata is missing: {', '.join(missing)}")
    if not re.fullmatch(r"[A-Za-z0-9.+-]{2,80}", normalized["license_spdx"]):
        raise StudioError("license SPDX id is invalid")
    with PROJECT_WRITE_LOCK:
        project = load_project(data_dir, project_id)
        project["source"] = normalized
        project["updated_at"] = _now()
        _write_json(project_dir(data_dir, project_id) / "project.json", project)
        return project


def import_video(
    data_dir: Path,
    project_id: str,
    source: Path,
    filename: str,
    *,
    width: int,
    height: int,
    duration: float,
    fps: float,
) -> dict[str, Any]:
    if width <= 0 or height <= 0 or duration <= 0 or not math.isfinite(fps) or fps <= 0:
        raise StudioError("valid width, height, duration, and fps are required")
    safe_name = Path(filename).name
    if safe_name in {"", ".", ".."}:
        raise StudioError("invalid video filename")
    destination_dir = project_dir(data_dir, project_id) / "video"
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / safe_name
    with tempfile.NamedTemporaryFile(dir=destination_dir, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        shutil.copyfile(source, temporary)
        digest = _sha256_file(temporary)
        with PROJECT_WRITE_LOCK:
            project = load_project(data_dir, project_id)
            previous_video = project.get("video")
            backup = None
            if destination.exists():
                backup = destination_dir / f".{safe_name}.{uuid.uuid4().hex}.backup"
                try:
                    os.link(destination, backup)
                except OSError:
                    shutil.copyfile(destination, backup)
            try:
                temporary.replace(destination)
                project["video"] = {
                    "filename": safe_name,
                    "width": width,
                    "height": height,
                    "duration_seconds": duration,
                    "fps": fps,
                    "frame_count": max(1, round(duration * fps)),
                    "sha256": digest,
                    "size_bytes": destination.stat().st_size,
                }
                project["updated_at"] = _now()
                _write_json(project_dir(data_dir, project_id) / "project.json", project)
            except Exception:
                if backup is not None:
                    backup.replace(destination)
                else:
                    destination.unlink(missing_ok=True)
                raise
            stale_paths = [backup] if backup is not None else []
            if previous_video and previous_video["filename"] != safe_name:
                stale_paths.append(destination_dir / previous_video["filename"])
            for old in stale_paths:
                try:
                    old.unlink(missing_ok=True)
                except OSError:
                    # Metadata already points to the new video; a stale file is safer than false failure.
                    pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return project


def extend_video_frame_count(
    data_dir: Path,
    project_id: str,
    frame_count: int,
    *,
    expected_video_sha256: str,
) -> dict[str, Any]:
    """Persist the decoder count without invalidating existing annotations."""
    if isinstance(frame_count, bool) or not isinstance(frame_count, int) or frame_count <= 0:
        raise StudioError("frame count must be a positive integer")
    with PROJECT_WRITE_LOCK:
        project = load_project(data_dir, project_id)
        video = project.get("video")
        if not video:
            raise StudioError("project has no video")
        if video["sha256"] != expected_video_sha256:
            raise StudioError("model job belongs to a different source video")
        annotations = load_annotations(data_dir, project_id)
        annotated_frame_count = 1 + max(
            (box["frame"] for box in annotations["boxes"]),
            default=-1,
        )
        safe_frame_count = max(frame_count, annotated_frame_count)
        if safe_frame_count != video["frame_count"]:
            video["frame_count"] = safe_frame_count
            project["updated_at"] = _now()
            _write_json(project_dir(data_dir, project_id) / "project.json", project)
        return project


def snapshot_video(
    data_dir: Path,
    project_id: str,
    destination_stem: Path,
) -> tuple[dict[str, Any], Path]:
    """Copy the exact imported video into an isolated model-job input."""
    with PROJECT_WRITE_LOCK:
        project = load_project(data_dir, project_id)
        video = project.get("video")
        if not video:
            raise StudioError("project has no video")
        source = video_path(data_dir, project_id)
        suffix = Path(video["filename"]).suffix
        if not re.fullmatch(r"\.[A-Za-z0-9]{1,16}", suffix):
            suffix = ".video"
        destination = destination_stem.parent / f"{destination_stem.name}{suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(source, destination)
            if _sha256_file(destination) != video["sha256"]:
                raise StudioError("project video changed while queuing model job")
            if os.name != "nt":
                destination.chmod(0o444)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return video, destination


def video_path(data_dir: Path, project_id: str) -> Path:
    project = load_project(data_dir, project_id)
    if not project["video"]:
        raise FileNotFoundError("project has no video")
    path = project_dir(data_dir, project_id) / "video" / project["video"]["filename"]
    if not path.is_file():
        raise FileNotFoundError("video is missing")
    return path


def validate_annotations(project: dict[str, Any], annotations: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if annotations.get("schema_version") != SCHEMA:
        errors.append(f"schema_version must be {SCHEMA}")
    tracks = annotations.get("tracks")
    boxes = annotations.get("boxes")
    if not isinstance(tracks, list) or not isinstance(boxes, list):
        return {"valid": False, "errors": ["tracks and boxes must be arrays"], "warnings": []}

    track_map: dict[str, dict[str, Any]] = {}
    for index, track in enumerate(tracks):
        if not isinstance(track, dict):
            errors.append(f"track {index} must be an object")
            continue
        track_id = track.get("id")
        if not isinstance(track_id, str) or not PROJECT_ID.fullmatch(track_id):
            errors.append(f"track {index} has an invalid id")
        elif track_id in track_map:
            errors.append(f"duplicate track id: {track_id}")
        else:
            track_map[track_id] = track
        if track.get("class_id") not in project["classes"]:
            errors.append(f"track {track_id or index} has an unsupported class")

    video = project.get("video")
    seen: set[tuple[str, int]] = set()
    frame_counts: dict[str, int] = {track_id: 0 for track_id in track_map}
    for index, box in enumerate(boxes):
        if not isinstance(box, dict):
            errors.append(f"box {index} must be an object")
            continue
        track_id, frame, coordinates = box.get("track_id"), box.get("frame"), box.get("bbox_xyxy")
        if track_id not in track_map:
            errors.append(f"box {index} references unknown track {track_id}")
        if not isinstance(frame, int) or frame < 0:
            errors.append(f"box {index} has an invalid frame")
            continue
        key = (track_id, frame)
        if key in seen:
            errors.append(f"duplicate box for track {track_id} at frame {frame}")
        seen.add(key)
        if not isinstance(coordinates, list) or len(coordinates) != 4:
            errors.append(f"box {index} must contain four coordinates")
            continue
        if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in coordinates):
            errors.append(f"box {index} has a non-finite coordinate")
            continue
        confidence = box.get("confidence")
        if "confidence" in box and (
            confidence is None
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            errors.append(f"box {index} has an invalid confidence")
        x1, y1, x2, y2 = coordinates
        if x1 < 0 or y1 < 0 or x2 <= x1 or y2 <= y1:
            errors.append(f"box {index} has invalid geometry")
        if video:
            if x2 > video["width"] or y2 > video["height"]:
                errors.append(f"box {index} exceeds video bounds")
            if frame >= video["frame_count"]:
                errors.append(f"box {index} exceeds the video frame count")
        frame_counts[track_id] = frame_counts.get(track_id, 0) + 1
    for track_id, count in frame_counts.items():
        if count == 0:
            warnings.append(f"track {track_id} has no boxes")
    if not video:
        warnings.append("project has no video")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {"tracks": len(tracks), "boxes": len(boxes)},
    }


def save_annotations(data_dir: Path, project_id: str, annotations: dict[str, Any]) -> dict[str, Any]:
    with PROJECT_WRITE_LOCK:
        project = load_project(data_dir, project_id)
        result = validate_annotations(project, annotations)
        if not result["valid"]:
            raise StudioError("; ".join(result["errors"]))
        normalized = {
            "schema_version": SCHEMA,
            "tracks": sorted(annotations["tracks"], key=lambda item: item["id"]),
            "boxes": sorted(annotations["boxes"], key=lambda item: (item["frame"], item["track_id"])),
        }
        _write_json(project_dir(data_dir, project_id) / "annotations.json", normalized)
        project["updated_at"] = _now()
        _write_json(project_dir(data_dir, project_id) / "project.json", project)
        return result


def canonical_rows(project: dict[str, Any], annotations: dict[str, Any]) -> list[dict[str, Any]]:
    if not project.get("video"):
        raise StudioError("a video is required before export")
    result = validate_annotations(project, annotations)
    if not result["valid"]:
        raise StudioError("; ".join(result["errors"]))
    tracks = {track["id"]: track for track in annotations["tracks"]}
    rows = []
    fps = project["video"]["fps"]
    width, height = project["video"]["width"], project["video"]["height"]
    for box in sorted(annotations["boxes"], key=lambda item: (item["frame"], item["track_id"])):
        track_id, frame = box["track_id"], box["frame"]
        coordinates = [round(float(value), 3) for value in box["bbox_xyxy"]]
        origin = tracks[track_id].get("label_origin", {"kind": "human", "model_run_ids": []})
        row = {
            "schema_version": "cvbench.track-annotation/v1",
            "clip_id": project["id"],
            "frame_index": frame,
            "source_timestamp_ns": round(frame / fps * 1_000_000_000),
            "track_id": track_id,
            "class_id": tracks[track_id]["class_id"],
            "bbox_xyxy": coordinates,
            "occlusion": tracks[track_id].get("occlusion", "unknown"),
            "truncated": coordinates[0] <= 0
            or coordinates[1] <= 0
            or coordinates[2] >= width
            or coordinates[3] >= height,
            "label_origin": origin,
        }
        if "confidence" in box:
            row["confidence"] = float(box["confidence"])
        rows.append(row)
    return rows


def export_project(data_dir: Path, project_id: str, output: Path) -> Path:
    project = load_project(data_dir, project_id)
    annotations = load_annotations(data_dir, project_id)
    rows = canonical_rows(project, annotations)
    if not project.get("source"):
        raise StudioError("source and license metadata are required before export")
    source = video_path(data_dir, project_id)
    if source.suffix.lower() != ".mp4":
        raise StudioError("canonical dataset export currently requires an MP4 source video")
    video_name = "video.mp4"
    clip_root = f"clips/{project_id}"
    fps = Fraction(str(project["video"]["fps"])).limit_denominator(1_000_000)
    license_name = re.sub(r"[^A-Za-z0-9._-]+", "-", project["source"]["license_spdx"]).strip("-")
    license_path = f"licenses/{license_name or 'LICENSE'}.txt"
    model_runs = []
    jobs_root = project_dir(data_dir, project_id) / "jobs"
    if jobs_root.is_dir():
        for path in sorted(jobs_root.glob("*.json")):
            job = _read_json(path)
            if job.get("status") != "completed" or not job.get("model"):
                continue
            model = job["model"]
            model_runs.append(
                {
                    "run_id": job["id"],
                    "model_name": model["name"],
                    "model_version": model["version"],
                    "weights_uri": model["weights_uri"],
                    "weights_sha256": model["weights_sha256"],
                    "code_revision": model["code_revision"],
                    "config_sha256": model["config_sha256"],
                    "raw_output_sha256": job["raw_output_sha256"],
                    "command": job["command"],
                    "license": model["license"],
                }
            )
    source_record = {
        "schema_version": "cvbench.source/v1",
        "clip_id": project_id,
        "source": {
            "title": project["source"]["title"],
            "uri": project["source"]["uri"],
            "sha256": project["video"]["sha256"],
            "license": {
                "spdx": project["source"]["license_spdx"],
                "name": project["source"]["license_name"],
                "url": project["source"]["license_url"],
                "file": license_path,
            },
        },
        "media": {
            "width": project["video"]["width"],
            "height": project["video"]["height"],
            "frame_count": project["video"]["frame_count"],
            "fps_numerator": fps.numerator,
            "fps_denominator": fps.denominator,
        },
        "transformations": [],
        "model_runs": model_runs,
    }
    contribution = {
        "schema_version": "cvbench.studio-contribution/v1",
        "clip_id": project_id,
        "clip_path": clip_root,
        "license_path": license_path,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("contribution.json", json.dumps(contribution, indent=2, sort_keys=True) + "\n")
        archive.writestr(f"{clip_root}/source.json", json.dumps(source_record, indent=2, sort_keys=True) + "\n")
        archive.writestr(
            f"{clip_root}/tracks.jsonl",
            "".join(json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n" for row in rows),
        )
        archive.writestr(f"{clip_root}/review.jsonl", "")
        archive.writestr(license_path, project["source"]["license_text"].rstrip() + "\n")
        archive.write(source, f"{clip_root}/{video_name}")
    return output
