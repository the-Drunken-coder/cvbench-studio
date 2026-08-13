from __future__ import annotations

import math
from collections.abc import Iterator


def stride_aligned_size(value: str, stride: int = 32) -> int:
    """Parse a positive model input size aligned to the model's largest stride."""
    size = int(value)
    if size <= 0 or size % stride:
        raise ValueError(f"input size must be a positive multiple of {stride}")
    return size


def probability(value: str) -> float:
    """Parse a finite probability in the closed unit interval."""
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError("probability must be finite and between 0 and 1")
    return result


def iter_sample_frame_indices(source_fps: float, sample_fps: float) -> Iterator[int]:
    """Yield deterministic sample indices without trusting container frame counts."""
    if not math.isfinite(source_fps) or not math.isfinite(sample_fps) or source_fps <= 0 or sample_fps <= 0:
        raise ValueError("frame rates must be positive and finite")
    effective_sample_fps = min(source_fps, sample_fps)
    sample_index = 0
    while True:
        frame_index = round(sample_index * source_fps / effective_sample_fps)
        sample_index += 1
        yield frame_index


def sample_frame_indices(frame_count: int, source_fps: float, sample_fps: float) -> list[int]:
    """Return deterministic nearest-frame samples without duplicating source frames."""
    if (
        frame_count <= 0
        or not math.isfinite(source_fps)
        or not math.isfinite(sample_fps)
        or source_fps <= 0
        or sample_fps <= 0
    ):
        raise ValueError("frame_count and frame rates must be positive and finite")
    effective_sample_fps = min(source_fps, sample_fps)
    count = max(1, math.ceil(frame_count * effective_sample_fps / source_fps))
    return sorted(
        {
            min(frame_count - 1, round(index * source_fps / effective_sample_fps))
            for index in range(count)
        }
    )
