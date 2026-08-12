from __future__ import annotations

import math
from collections.abc import Iterator


def iter_sample_frame_indices(source_fps: float, sample_fps: float) -> Iterator[int]:
    """Yield deterministic sample indices without trusting container frame counts."""
    if source_fps <= 0 or sample_fps <= 0:
        raise ValueError("frame rates must be positive")
    sample_index = 0
    previous = -1
    while True:
        frame_index = round(sample_index * source_fps / sample_fps)
        sample_index += 1
        if frame_index > previous:
            previous = frame_index
            yield frame_index


def sample_frame_indices(frame_count: int, source_fps: float, sample_fps: float) -> list[int]:
    """Return deterministic nearest-frame samples without duplicating source frames."""
    if frame_count <= 0 or source_fps <= 0 or sample_fps <= 0:
        raise ValueError("frame_count and frame rates must be positive")
    count = max(1, math.ceil(frame_count * sample_fps / source_fps))
    return sorted({min(frame_count - 1, round(index * source_fps / sample_fps)) for index in range(count)})
