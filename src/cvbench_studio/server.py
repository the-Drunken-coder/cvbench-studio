from __future__ import annotations

import json
import mimetypes
import tempfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import unquote, urlparse

from .core import (
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
from .models import ModelQueue


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], data_dir: Path):
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.model_queue = ModelQueue(self.data_dir)
        super().__init__(address, StudioHandler)


class StudioHandler(BaseHTTPRequestHandler):
    server: StudioServer

    def log_message(self, format: str, *args) -> None:
        print(f"[cvbench-studio] {self.address_string()} {format % args}")

    def do_GET(self) -> None:
        self._dispatch(self._get)

    def do_POST(self) -> None:
        self._dispatch(self._post)

    def do_PUT(self) -> None:
        self._dispatch(self._put)

    def _dispatch(self, operation) -> None:
        try:
            operation()
        except FileNotFoundError as exc:
            self._json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
        except (StudioError, json.JSONDecodeError, KeyError, TypeError) as exc:
            self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001 - request failures must not stop the local server
            self._json({"error": f"internal error: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    @property
    def segments(self) -> list[str]:
        return [unquote(item) for item in urlparse(self.path).path.split("/") if item]

    def _get(self) -> None:
        segments = self.segments
        if not segments:
            return self._static("index.html")
        if segments[0] != "api":
            return self._static("/".join(segments))
        if segments == ["api", "projects"]:
            return self._json({"projects": list_projects(self.server.data_dir)})
        if len(segments) >= 3 and segments[:2] == ["api", "projects"]:
            project_id = segments[2]
            if len(segments) == 3:
                return self._json(load_project(self.server.data_dir, project_id))
            resource = segments[3]
            if resource == "annotations" and len(segments) == 4:
                return self._json(load_annotations(self.server.data_dir, project_id))
            if resource == "validate" and len(segments) == 4:
                project = load_project(self.server.data_dir, project_id)
                annotations = load_annotations(self.server.data_dir, project_id)
                return self._json(validate_annotations(project, annotations))
            if resource == "video" and len(segments) == 4:
                return self._file(video_path(self.server.data_dir, project_id), ranged=True)
            if resource == "jobs" and len(segments) == 4:
                return self._json({"jobs": self.server.model_queue.list(project_id)})
            if resource == "jobs" and len(segments) == 5:
                return self._json(self.server.model_queue.get(project_id, segments[4]))
            if resource == "jobs" and len(segments) == 6 and segments[5] == "output":
                return self._file(self.server.model_queue.output_path(project_id, segments[4]))
            if resource == "jobs" and len(segments) == 6 and segments[5] == "proposals":
                return self._json(self.server.model_queue.proposals(project_id, segments[4]))
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _post(self) -> None:
        segments = self.segments
        if segments == ["api", "projects"]:
            body = self._read_json()
            project = create_project(self.server.data_dir, body.get("name", ""), body.get("classes"))
            return self._json(project, HTTPStatus.CREATED)
        if len(segments) == 4 and segments[:2] == ["api", "projects"]:
            project_id, resource = segments[2], segments[3]
            if resource == "export":
                body = self._read_json(optional=True)
                export_root = self.server.data_dir / "exports"
                output = export_root / f"{project_id}.cvbench.zip"
                export_project(self.server.data_dir, project_id, output)
                return self._file(output, download_name=output.name)
            if resource == "jobs":
                body = self._read_json()
                return self._json(
                    self.server.model_queue.submit(project_id, body.get("command", "")),
                    HTTPStatus.ACCEPTED,
                )
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _put(self) -> None:
        segments = self.segments
        if len(segments) != 4 or segments[:2] != ["api", "projects"]:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        project_id, resource = segments[2], segments[3]
        if resource == "annotations":
            return self._json(save_annotations(self.server.data_dir, project_id, self._read_json()))
        if resource == "source":
            return self._json(save_source_metadata(self.server.data_dir, project_id, self._read_json()))
        if resource == "video":
            length = self._content_length()
            filename = self.headers.get("X-Video-Filename", "clip.mp4")
            metadata = {
                "width": int(self.headers["X-Video-Width"]),
                "height": int(self.headers["X-Video-Height"]),
                "duration": float(self.headers["X-Video-Duration"]),
                "fps": float(self.headers["X-Video-Fps"]),
            }
            with tempfile.NamedTemporaryFile(dir=self.server.data_dir, delete=False) as stream:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise StudioError("video upload ended early")
                    stream.write(chunk)
                    remaining -= len(chunk)
                temporary = Path(stream.name)
            try:
                project = import_video(
                    self.server.data_dir,
                    project_id,
                    temporary,
                    filename,
                    **metadata,
                )
            finally:
                temporary.unlink(missing_ok=True)
            return self._json(project)
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _read_json(self, optional: bool = False) -> dict:
        length = self._content_length()
        if length == 0 and optional:
            return {}
        if length > 10 * 1024 * 1024:
            raise StudioError("JSON request is too large")
        body = self.rfile.read(length)
        value = json.loads(body or b"{}")
        if not isinstance(value, dict):
            raise StudioError("request body must be a JSON object")
        return value

    def _content_length(self) -> int:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise StudioError("invalid Content-Length") from exc
        if length < 0:
            raise StudioError("invalid Content-Length")
        return length

    def _json(self, value, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        if name not in {"index.html", "app.js", "style.css"}:
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        resource = files("cvbench_studio").joinpath("static", name)
        body = resource.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, *, ranged: bool = False, download_name: str | None = None) -> None:
        if not path.is_file():
            raise FileNotFoundError(path.name)
        size = path.stat().st_size
        start, end = 0, size - 1
        status = HTTPStatus.OK
        if ranged and (header := self.headers.get("Range")):
            if not header.startswith("bytes=") or "," in header:
                raise StudioError("unsupported byte range")
            start_text, end_text = header[6:].split("-", 1)
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else end
            else:
                suffix = int(end_text)
                start = max(0, size - suffix)
            if start < 0 or end < start or start >= size:
                self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = HTTPStatus.PARTIAL_CONTENT
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", mimetypes.guess_type(path.name)[0] or "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download_name:
            self.send_header("Content-Disposition", f'attachment; filename="{download_name}"')
        self.end_headers()
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def make_server(host: str, port: int, data_dir: Path) -> StudioServer:
    return StudioServer((host, port), data_dir)
