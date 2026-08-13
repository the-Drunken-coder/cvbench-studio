from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any

from .core import StudioError, extend_video_frame_count, load_project, project_dir, snapshot_video

TRACK_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
WINDOWS_CREATE_SUSPENDED = 0x00000004
OUTPUT_READ_BYTES = 64 * 1024
MAX_OUTPUT_LINE_BYTES = 1024 * 1024


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _assign_windows_job(process: subprocess.Popen[str]) -> int:
    """Attach a process tree to a kill-on-close Windows Job Object."""
    import ctypes
    from ctypes import wintypes

    class BasicLimitInformation(ctypes.Structure):
        _fields_ = [
            ("per_process_user_time_limit", ctypes.c_longlong),
            ("per_job_user_time_limit", ctypes.c_longlong),
            ("limit_flags", wintypes.DWORD),
            ("minimum_working_set_size", ctypes.c_size_t),
            ("maximum_working_set_size", ctypes.c_size_t),
            ("active_process_limit", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority_class", wintypes.DWORD),
            ("scheduling_class", wintypes.DWORD),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            ("read_operation_count", ctypes.c_ulonglong),
            ("write_operation_count", ctypes.c_ulonglong),
            ("other_operation_count", ctypes.c_ulonglong),
            ("read_transfer_count", ctypes.c_ulonglong),
            ("write_transfer_count", ctypes.c_ulonglong),
            ("other_transfer_count", ctypes.c_ulonglong),
        ]

    class ExtendedLimitInformation(ctypes.Structure):
        _fields_ = [
            ("basic_limit_information", BasicLimitInformation),
            ("io_info", IoCounters),
            ("process_memory_limit", ctypes.c_size_t),
            ("job_memory_limit", ctypes.c_size_t),
            ("peak_process_memory_used", ctypes.c_size_t),
            ("peak_job_memory_used", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    information = ExtendedLimitInformation()
    information.basic_limit_information.limit_flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        9,  # JobObjectExtendedLimitInformation
        ctypes.byref(information),
        ctypes.sizeof(information),
    ) or not kernel32.AssignProcessToJobObject(
        handle,
        wintypes.HANDLE(process._handle),
    ):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        raise ctypes.WinError(error)
    return int(handle)


def _close_windows_job(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    if not kernel32.CloseHandle(wintypes.HANDLE(handle)):
        raise ctypes.WinError(ctypes.get_last_error())


def _resume_windows_process(process: subprocess.Popen[str]) -> None:
    """Resume the primary thread of a process created with CREATE_SUSPENDED."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry32(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage_count", wintypes.DWORD),
            ("thread_id", wintypes.DWORD),
            ("owner_process_id", wintypes.DWORD),
            ("base_priority", wintypes.LONG),
            ("priority_delta", wintypes.LONG),
            ("flags", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry32))
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.POINTER(ThreadEntry32))
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = (wintypes.HANDLE,)
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000004, 0)  # TH32CS_SNAPTHREAD
    if snapshot == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = ThreadEntry32()
    entry.size = ctypes.sizeof(entry)
    try:
        has_entry = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.owner_process_id == process.pid:
                thread = kernel32.OpenThread(0x0002, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel32.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                finally:
                    kernel32.CloseHandle(thread)
                return
            has_entry = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    raise OSError("could not find the suspended adapter thread")


def _start_adapter_process(
    expanded: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> tuple[subprocess.Popen[str], int | None]:
    """Start an adapter under process-tree supervision before it can execute."""
    process: subprocess.Popen[str] = subprocess.Popen(
        expanded,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
        creationflags=WINDOWS_CREATE_SUSPENDED if os.name == "nt" else 0,
    )
    if os.name != "nt":
        return process, None
    windows_job = None
    try:
        windows_job = _assign_windows_job(process)
        _resume_windows_process(process)
        return process, windows_job
    except OSError as exc:
        if windows_job is not None:
            try:
                _close_windows_job(windows_job)
            except OSError:
                pass
        else:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if process.poll() is None:
            process.kill()
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        raise StudioError("could not establish Windows process-tree supervision") from exc


class ModelQueue:
    """Runs explicit external commands without a shell.

    Adapters receive a video path and must write JSONL proposals to the output
    placeholder. Proposals are never promoted to annotations automatically.
    """

    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._ownership = (self.data_dir / ".model-queue.lock").open("a+b")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(self._ownership, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                import msvcrt

                self._ownership.seek(0)
                self._ownership.write(b"\0")
                self._ownership.flush()
                self._ownership.seek(0)
                msvcrt.locking(self._ownership.fileno(), msvcrt.LK_NBLCK, 1)
        except (OSError, BlockingIOError) as exc:
            self._ownership.close()
            raise StudioError("another model queue is already using this data directory") from exc
        self._queue: Queue[tuple[str, str, list[str]] | None] = Queue()
        self._lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._closing = threading.Event()
        self._process_lock = threading.Lock()
        self._active_process: subprocess.Popen[str] | None = None
        self._active_windows_job: int | None = None
        self._fail_interrupted_jobs()
        self._worker = threading.Thread(target=self._run, daemon=True, name="cvbench-model-queue")
        self._worker.start()

    def _fail_interrupted_jobs(self) -> None:
        projects_root = self.data_dir / "projects"
        if not projects_root.is_dir():
            return
        for project_root in projects_root.iterdir():
            jobs_root = project_root / "jobs"
            if not jobs_root.is_dir():
                continue
            for job_path in jobs_root.glob("*.json"):
                try:
                    job = json.loads(job_path.read_text())
                    if not isinstance(job, dict):
                        continue
                    job_id = job["id"]
                    project_id = job["project_id"]
                    if (
                        job["status"] not in {"queued", "running"}
                        or job_path.stem != job_id
                        or project_root.name != project_id
                    ):
                        continue
                except (KeyError, OSError, ValueError, json.JSONDecodeError):
                    continue
                try:
                    input_path = self.input_path(project_id, job_id)
                except (OSError, StudioError):
                    input_path = None
                job["status"] = "failed"
                job["finished_at"] = _now()
                job["error"] = "Studio restarted before the adapter completed"
                self._write(project_id, job)
                if input_path is not None:
                    input_path.unlink(missing_ok=True)

    def close(self) -> None:
        """Cancel queued work and release exclusive queue ownership promptly."""
        if self._ownership.closed:
            return
        pending_error = None
        with self._lifecycle_lock:
            if not self._closing.is_set():
                self._closing.set()
                pending_error = self._cancel_pending_jobs()
                self._queue.put(None)
        self._stop_active_process(kill=False)
        self._worker.join(timeout=2)
        if self._worker.is_alive():
            self._stop_active_process(kill=True)
            self._worker.join(timeout=2)
        if self._worker.is_alive():
            raise StudioError("model queue worker did not stop")
        if not self._ownership.closed:
            self._ownership.close()
        if pending_error is not None:
            raise StudioError("could not persist every cancelled model job") from pending_error

    def _cancel_pending_jobs(self) -> Exception | None:
        first_error = None
        while True:
            try:
                queued = self._queue.get_nowait()
            except Empty:
                return first_error
            try:
                if queued is not None:
                    project_id, job_id, _ = queued
                    try:
                        self._cancel_pending_job(project_id, job_id)
                    except Exception as exc:  # noqa: BLE001 - shutdown must continue draining
                        first_error = first_error or exc
            finally:
                self._queue.task_done()

    def _cancel_pending_job(self, project_id: str, job_id: str) -> None:
        job = self.get(project_id, job_id)
        job["status"] = "failed"
        job["finished_at"] = _now()
        job["error"] = "Studio closed before the adapter started"
        try:
            self.input_path(project_id, job_id).unlink(missing_ok=True)
        except OSError as exc:
            job["error"] = f"could not remove model input snapshot: {exc}"
        self._write(project_id, job)

    def _stop_active_process(self, *, kill: bool) -> None:
        with self._process_lock:
            process = self._active_process
            if process is None:
                return
            windows_job = self._active_windows_job
            if windows_job is not None:
                self._active_windows_job = None
            try:
                if windows_job is not None:
                    _close_windows_job(windows_job)
                elif os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL if kill else signal.SIGTERM)
                elif process.poll() is None:
                    process.kill() if kill else process.terminate()
            except OSError:
                pass

    def submit(self, project_id: str, command: str | list[str]) -> dict[str, Any]:
        argv = shlex.split(command) if isinstance(command, str) else list(command)
        if not argv or not all(isinstance(part, str) and part for part in argv):
            raise StudioError("model command must contain an executable")
        if self._closing.is_set():
            raise StudioError("model queue is closed")
        job_id = uuid.uuid4().hex
        input_stem = self._input_stem(project_id, job_id)
        video, input_path = snapshot_video(self.data_dir, project_id, input_stem)
        job = {
            "schema_version": "cvbench.model-job/v1",
            "id": job_id,
            "project_id": project_id,
            "input_filename": input_path.name,
            "video_sha256": video["sha256"],
            "command": argv,
            "status": "queued",
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "error": None,
        }
        try:
            with self._lifecycle_lock:
                if self._closing.is_set():
                    raise StudioError("model queue is closed")
                self._write(project_id, job)
                self._queue.put((project_id, job_id, argv))
        except BaseException:
            input_path.unlink(missing_ok=True)
            raise
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

    def input_path(self, project_id: str, job_id: str) -> Path:
        job = self.get(project_id, job_id)
        filename = job.get("input_filename")
        if (
            not isinstance(filename, str)
            or Path(filename).name != filename
            or not filename.startswith(f"{job_id}.input.")
        ):
            raise StudioError("model job has an invalid input filename")
        return project_dir(self.data_dir, project_id) / "jobs" / filename

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
        if job.get("video_sha256") != video["sha256"]:
            raise StudioError("model job belongs to a different source video")
        decoded_frame_count = job.get("decoded_frame_count")
        frame_count = decoded_frame_count or video["frame_count"]
        output_body = self.output_path(project_id, job_id).read_bytes()
        if hashlib.sha256(output_body).hexdigest() != job.get("raw_output_sha256"):
            raise StudioError("model proposal output changed after adapter execution")
        try:
            output_text = output_body.decode()
        except UnicodeDecodeError as exc:
            raise StudioError("model proposal output is not valid UTF-8") from exc
        rows = []
        for line_number, line in enumerate(output_text.splitlines(), 1):
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
            if (
                isinstance(frame, bool)
                or not isinstance(frame, int)
                or frame < 0
                or frame >= frame_count
            ):
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
            if "confidence" in row and (
                confidence is None
                or isinstance(confidence, bool)
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
                imported_box["confidence"] = float(row["confidence"])
            boxes.append(imported_box)
        with self._lock:
            project = extend_video_frame_count(
                self.data_dir,
                project_id,
                decoded_frame_count or video["frame_count"],
                expected_video_sha256=job["video_sha256"],
            )
        video = project["video"]
        return {
            "job_id": job_id,
            "frame_count": video["frame_count"],
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

    def _input_stem(self, project_id: str, job_id: str) -> Path:
        self._job_path(project_id, job_id)
        return project_dir(self.data_dir, project_id) / "jobs" / f"{job_id}.input"

    def _write(self, project_id: str, job: dict[str, Any]) -> None:
        path = self._job_path(project_id, job["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(job, indent=2, sort_keys=True) + "\n")
        temporary.replace(path)

    def _snapshot_sha256(self, input_path: Path) -> str:
        digest = hashlib.sha256()
        with input_path.open("rb") as stream:
            while True:
                if self._closing.is_set():
                    raise StudioError("Studio closed before the adapter completed")
                chunk = stream.read(1024 * 1024)
                if self._closing.is_set():
                    raise StudioError("Studio closed before the adapter completed")
                if not chunk:
                    return digest.hexdigest()
                digest.update(chunk)

    def _inspect_adapter_output(self, output: Path) -> tuple[str, Any, int | None]:
        """Hash and validate bounded JSONL records with cooperative shutdown."""
        digest = hashlib.sha256()
        pending = b""
        line_number = 0
        model = None
        decoded_frame_count = None

        def inspect_line(raw_line: bytes) -> None:
            nonlocal model, decoded_frame_count
            line = raw_line[:-1] if raw_line.endswith(b"\r") else raw_line
            if not line:
                return
            if len(line) > MAX_OUTPUT_LINE_BYTES:
                raise StudioError(f"adapter output line {line_number} is too large")
            if self._closing.is_set():
                raise StudioError("Studio closed before the adapter completed")
            try:
                row = json.loads(line)
            except UnicodeDecodeError as exc:
                raise StudioError("adapter output is not valid UTF-8") from exc
            except json.JSONDecodeError as exc:
                raise StudioError(f"invalid proposal JSONL on line {line_number}") from exc
            if self._closing.is_set():
                raise StudioError("Studio closed before the adapter completed")
            if row.get("schema_version") in {
                "cvbench.model-output/v1",
                "cvbench.model-proposal/v1",
            }:
                candidate_model = row.get("model")
                self._validate_model(candidate_model, line_number)
                if model is not None and candidate_model != model:
                    raise StudioError("proposal rows contain inconsistent model provenance")
                model = candidate_model
            if row.get("schema_version") == "cvbench.model-output/v1" and "decoded_frame_count" in row:
                candidate_count = row["decoded_frame_count"]
                if (
                    isinstance(candidate_count, bool)
                    or not isinstance(candidate_count, int)
                    or candidate_count <= 0
                ):
                    raise StudioError(
                        f"adapter output line {line_number} has an invalid decoded frame count"
                    )
                if decoded_frame_count is not None and candidate_count != decoded_frame_count:
                    raise StudioError("adapter output has inconsistent decoded frame counts")
                decoded_frame_count = candidate_count

        with output.open("rb") as stream:
            while True:
                if self._closing.is_set():
                    raise StudioError("Studio closed before the adapter completed")
                chunk = stream.read(OUTPUT_READ_BYTES)
                if self._closing.is_set():
                    raise StudioError("Studio closed before the adapter completed")
                if not chunk:
                    break
                digest.update(chunk)
                lines = (pending + chunk).split(b"\n")
                pending = lines.pop()
                for raw_line in lines:
                    line_number += 1
                    inspect_line(raw_line)
                if len(pending) > MAX_OUTPUT_LINE_BYTES:
                    raise StudioError(f"adapter output line {line_number + 1} is too large")
            if pending:
                line_number += 1
                inspect_line(pending)
        return digest.hexdigest(), model, decoded_frame_count

    def _run(self) -> None:
        while True:
            queued = self._queue.get()
            try:
                if queued is None:
                    return
                project_id, job_id, argv = queued
                self._execute(project_id, job_id, argv)
            finally:
                self._queue.task_done()

    def _execute(self, project_id: str, job_id: str, argv: list[str]) -> None:
        job = self.get(project_id, job_id)
        job["status"] = "running"
        job["started_at"] = _now()
        self._write(project_id, job)
        output = self.output_path(project_id, job_id)
        input_path = self.input_path(project_id, job_id)
        try:
            if self._closing.is_set():
                raise StudioError("Studio closed before the adapter completed")
            if not input_path.is_file():
                raise StudioError("queued model input is missing")
            if self._snapshot_sha256(input_path) != job["video_sha256"]:
                raise StudioError("queued model input no longer matches its source video")
            replacements = {
                "{video}": str(input_path),
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
            process, windows_job = _start_adapter_process(
                expanded,
                cwd=project_dir(self.data_dir, project_id),
                environment=environment,
            )
            with self._process_lock:
                self._active_process = process
                self._active_windows_job = windows_job
                closing = self._closing.is_set()
            if closing:
                self._stop_active_process(kill=False)
            try:
                stdout, stderr = process.communicate(timeout=24 * 60 * 60)
            except subprocess.TimeoutExpired as exc:
                self._stop_active_process(kill=True)
                process.communicate()
                raise StudioError("adapter timed out after 24 hours") from exc
            finally:
                remaining_windows_job = None
                with self._process_lock:
                    if self._active_process is process:
                        self._active_process = None
                        remaining_windows_job = self._active_windows_job
                        self._active_windows_job = None
                if remaining_windows_job is not None:
                    try:
                        _close_windows_job(remaining_windows_job)
                    except OSError:
                        pass
            if self._closing.is_set():
                raise StudioError("Studio closed before the adapter completed")
            if self._snapshot_sha256(input_path) != job["video_sha256"]:
                raise StudioError("queued model input changed during adapter execution")
            job["returncode"] = process.returncode
            job["stdout"] = stdout[-20_000:]
            job["stderr"] = stderr[-20_000:]
            if process.returncode:
                job["status"] = "failed"
                job["error"] = f"adapter exited with status {process.returncode}"
            elif not output.is_file():
                job["status"] = "failed"
                job["error"] = "adapter did not create {output}"
            else:
                output_sha256, model, decoded_frame_count = self._inspect_adapter_output(output)
                job["raw_output_sha256"] = output_sha256
                job["model"] = model
                if decoded_frame_count is not None:
                    job["decoded_frame_count"] = decoded_frame_count
                job["status"] = "completed"
        except Exception as exc:  # noqa: BLE001 - adapter failures become durable job state
            job["status"] = "failed"
            job["error"] = str(exc)
        job["finished_at"] = _now()
        try:
            input_path.unlink(missing_ok=True)
        except OSError as exc:
            job["status"] = "failed"
            job["error"] = f"could not remove model input snapshot: {exc}"
        self._write(project_id, job)

    @staticmethod
    def _validate_model(model: Any, line_number: int) -> None:
        required = {
            "name", "version", "weights_uri", "weights_sha256",
            "code_revision", "config_sha256", "license",
        }
        if not isinstance(model, dict) or not required.issubset(model):
            raise StudioError(f"adapter output line {line_number} lacks complete model provenance")
        if any(
            not isinstance(model[key], str) or not model[key].strip()
            for key in ("name", "version", "weights_uri")
        ):
            raise StudioError(f"adapter output line {line_number} has invalid model identity provenance")
        if any(
            not isinstance(model[key], str) or not SHA256.fullmatch(model[key])
            for key in ("weights_sha256", "config_sha256")
        ):
            raise StudioError(f"adapter output line {line_number} has an invalid provenance hash")
        code_revision = model["code_revision"]
        if not isinstance(code_revision, str) or len(code_revision.strip()) < 7:
            raise StudioError(f"adapter output line {line_number} has an invalid code revision")
        license_value = model["license"]
        spdx = license_value.get("spdx") if isinstance(license_value, dict) else None
        license_url = license_value.get("url") if isinstance(license_value, dict) else None
        if (
            not isinstance(license_value, dict)
            or not isinstance(spdx, str)
            or not re.fullmatch(r"[A-Za-z0-9.+-]{2,80}", spdx)
            or not isinstance(license_url, str)
            or not license_url.strip()
        ):
            raise StudioError(f"adapter output line {line_number} has invalid model license provenance")
