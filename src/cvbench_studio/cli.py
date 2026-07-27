from __future__ import annotations

import argparse
import json
from pathlib import Path

from .core import export_project, load_annotations, load_project, validate_annotations
from .models import ModelQueue
from .server import make_server


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cvbench-studio")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(".cvbench-studio"),
        help="local project store (default: .cvbench-studio)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="start the local browser app")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    validate = commands.add_parser("validate", help="validate a project's annotations")
    validate.add_argument("project_id")
    export = commands.add_parser("export", help="export a canonical clip package")
    export.add_argument("project_id")
    export.add_argument("--output", type=Path)
    model = commands.add_parser("run-model", help="queue an external model adapter")
    model.add_argument("project_id")
    model.add_argument("adapter", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    data_dir = args.data_dir.resolve()
    if args.command == "serve":
        server = make_server(args.host, args.port, data_dir)
        print(f"CVBench Studio: http://{args.host}:{server.server_port}")
        print(f"Data directory: {data_dir}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0
    if args.command == "validate":
        result = validate_annotations(
            load_project(data_dir, args.project_id),
            load_annotations(data_dir, args.project_id),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["valid"] else 1
    if args.command == "export":
        output = args.output or Path(f"{args.project_id}.cvbench.zip")
        print(export_project(data_dir, args.project_id, output))
        return 0
    if args.command == "run-model":
        if not args.adapter:
            raise SystemExit("run-model requires an external adapter command after --")
        queue = ModelQueue(data_dir)
        job = queue.submit(args.project_id, args.adapter)
        job = queue.wait(args.project_id, job["id"])
        print(json.dumps(job, indent=2, sort_keys=True))
        return 0 if job["status"] == "completed" else 1
    raise AssertionError(args.command)
