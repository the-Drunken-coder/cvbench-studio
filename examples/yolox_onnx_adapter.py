"""Emit reviewable CVBench Studio proposals from a YOLOX COCO ONNX model."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from cvbench_studio.sampling import (
    iter_sample_frame_indices,
    non_empty_string,
    probability,
    stride_aligned_size,
)


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]
    class_id: str
    confidence: float


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: object) -> str:
    body = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def area(box: tuple[float, float, float, float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def iou(left: tuple[float, float, float, float], right: tuple[float, float, float, float]) -> float:
    intersection = max(0.0, min(left[2], right[2]) - max(left[0], right[0])) * max(
        0.0, min(left[3], right[3]) - max(left[1], right[1])
    )
    union = area(left) + area(right) - intersection
    return intersection / union if union > 0 else 0.0


def nms(detections: Iterable[Detection], threshold: float) -> list[Detection]:
    kept: list[Detection] = []
    for candidate in sorted(detections, key=lambda item: item.confidence, reverse=True):
        if all(
            candidate.class_id != selected.class_id or iou(candidate.box, selected.box) <= threshold
            for selected in kept
        ):
            kept.append(candidate)
    return sorted(kept, key=lambda item: (item.class_id, item.box))


def parse_class_map(values: list[str]) -> dict[int, str]:
    result: dict[int, str] = {}
    for value in values:
        coco_id, separator, class_id = value.partition("=")
        if not separator or not coco_id.isdigit() or not class_id:
            raise ValueError(f"class mapping must use <coco-id=class-id>: {value!r}")
        index = int(coco_id)
        if index > 79:
            raise ValueError(f"COCO class ID must be between 0 and 79: {index}")
        result[index] = class_id
    if not result:
        raise ValueError("at least one --class mapping is required")
    return result


class YoloX:
    def __init__(self, model: Path, class_map: dict[int, str], input_size: int) -> None:
        self.class_map = class_map
        self.input_size = input_size
        self.net = cv2.dnn.readNetFromONNX(str(model))
        self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        grids = []
        strides = []
        for stride in (8, 16, 32):
            size = input_size // stride
            y_grid, x_grid = np.meshgrid(np.arange(size), np.arange(size), indexing="ij")
            grids.append(np.stack((x_grid, y_grid), axis=2).reshape(-1, 2))
            strides.append(np.full((size * size, 1), stride))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.expanded_stride = np.concatenate(strides).astype(np.float32)

    def detect(self, image: np.ndarray, minimum_score: float, nms_threshold: float) -> list[Detection]:
        height, width = image.shape[:2]
        ratio = min(self.input_size / height, self.input_size / width)
        resized = cv2.resize(
            image,
            (round(width * ratio), round(height * ratio)),
            interpolation=cv2.INTER_LINEAR,
        )
        padded = np.full((self.input_size, self.input_size, 3), 114, dtype=np.uint8)
        padded[: resized.shape[0], : resized.shape[1]] = resized
        self.net.setInput(padded.transpose(2, 0, 1).astype(np.float32)[None])
        output = np.asarray(self.net.forward()).reshape(-1, 85)
        output[:, :2] = (output[:, :2] + self.grid) * self.expanded_stride
        output[:, 2:4] = np.exp(output[:, 2:4]) * self.expanded_stride

        boxes = output[:, :4].copy()
        boxes[:, 0] = output[:, 0] - output[:, 2] / 2
        boxes[:, 1] = output[:, 1] - output[:, 3] / 2
        boxes[:, 2] = output[:, 0] + output[:, 2] / 2
        boxes[:, 3] = output[:, 1] + output[:, 3] / 2
        boxes /= ratio
        class_scores = output[:, 4:5] * output[:, 5:]

        detections: list[Detection] = []
        for coco_id, class_id in self.class_map.items():
            scores = class_scores[:, coco_id]
            for index in np.flatnonzero(scores >= minimum_score):
                raw = boxes[index]
                if (
                    not np.isfinite(raw).all()
                    or raw[2] <= 0
                    or raw[3] <= 0
                    or raw[0] >= width
                    or raw[1] >= height
                ):
                    continue
                box = (
                    max(0.0, float(raw[0])),
                    max(0.0, float(raw[1])),
                    min(float(raw[2]), float(width)),
                    min(float(raw[3]), float(height)),
                )
                if area(box) >= 16:
                    detections.append(Detection(box, class_id, float(scores[index])))
        return nms(detections, nms_threshold)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--weights-uri", type=non_empty_string, required=True)
    parser.add_argument("--code-revision", type=non_empty_string, required=True)
    parser.add_argument(
        "--model-name", type=non_empty_string, default="Megvii YOLOX-X COCO 640 ONNX"
    )
    parser.add_argument("--model-version", type=non_empty_string, default="YOLOX-X")
    parser.add_argument("--class", dest="classes", action="append", default=[])
    parser.add_argument("--sample-fps", type=float, default=5.0)
    parser.add_argument("--input-size", type=stride_aligned_size, default=640)
    parser.add_argument("--confidence-threshold", type=probability, default=0.25)
    parser.add_argument("--nms-threshold", type=probability, default=0.45)
    parser.add_argument("--license-spdx", default="Apache-2.0")
    parser.add_argument("--license-url", default="https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_path = args.output.resolve()
    for label, input_path in (("--video", args.video), ("--model", args.model)):
        if output_path == input_path.resolve() or (
            args.output.exists() and input_path.exists() and args.output.samefile(input_path)
        ):
            raise ValueError(f"--output must not identify {label}")
    class_map = parse_class_map(args.classes or ["0=person", "16=dog"])
    config = {
        "class_map": class_map,
        "confidence_threshold": args.confidence_threshold,
        "input_size": args.input_size,
        "nms_threshold": args.nms_threshold,
        "sample_fps": args.sample_fps,
    }
    model = {
        "name": args.model_name,
        "version": args.model_version,
        "weights_uri": args.weights_uri,
        "weights_sha256": sha256_file(args.model),
        "code_revision": args.code_revision,
        "config_sha256": canonical_sha256(config),
        "license": {"spdx": args.license_spdx, "url": args.license_url},
    }
    detector = YoloX(args.model, class_map, args.input_size)
    capture = cv2.VideoCapture(str(args.video))
    if not capture.isOpened():
        raise ValueError(f"cannot decode source video: {args.video}")
    fps = float(capture.get(cv2.CAP_PROP_FPS))
    selected_indices = iter_sample_frame_indices(fps, args.sample_fps)
    selected_frame = next(selected_indices)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame_index = 0
    try:
        with args.output.open("w") as output:
            metadata = {"schema_version": "cvbench.model-output/v1", "model": model}
            output.write(json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n")
            while True:
                ok, image = capture.read()
                if not ok:
                    break
                if frame_index == selected_frame:
                    detections = detector.detect(image, args.confidence_threshold, args.nms_threshold)
                    for index, detection in enumerate(detections):
                        row = {
                            "schema_version": "cvbench.model-proposal/v1",
                            "frame": frame_index,
                            "track_id": f"detection-{frame_index:06d}-{index:03d}",
                            "class_id": detection.class_id,
                            "bbox_xyxy": [round(value, 3) for value in detection.box],
                            "confidence": detection.confidence,
                            "model": model,
                        }
                        output.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
                    selected_frame = next(selected_indices)
                frame_index += 1
            if frame_index:
                completed_metadata = {
                    "schema_version": "cvbench.model-output/v1",
                    "model": model,
                    "decoded_frame_count": frame_index,
                }
                output.write(
                    json.dumps(completed_metadata, sort_keys=True, separators=(",", ":")) + "\n"
                )
    finally:
        capture.release()
    if frame_index == 0:
        raise ValueError(f"cannot decode any frames from source video: {args.video}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
