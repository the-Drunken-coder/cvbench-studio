"""Protocol smoke-test adapter. It intentionally emits no detections."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--video", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.video.is_file():
        raise SystemExit(f"missing video: {args.video}")
    args.output.write_text(
        json.dumps(
            {
                "schema_version": "cvbench.model-proposal-summary/v1",
                "adapter": "mock",
                "detections": 0,
            },
            separators=(",", ":"),
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
