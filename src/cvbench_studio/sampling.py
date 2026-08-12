from __future__ import annotations

import math


def sample_frame_indices(frame_count: int, source_fps: float, sample_fps: float) -> list[int]:
    """Return deterministic nearest-frame samples without duplicating source frames."""
    if frame_count <= 0 or source_fps <= 0 or sample_fps <= 0:
        raise ValueError("frame_count and frame rates must be positive")
    count = max(1, math.ceil(frame_count * sample_fps / source_fps))
    return sorted({min(frame_count - 1, round(index * source_fps / sample_fps)) for index in range(count)})
