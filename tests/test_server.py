from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from cvbench_studio.core import StudioError, import_video
from cvbench_studio.server import StudioServer, make_server


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.data = Path(self.temporary.name)
        self.server = make_server("127.0.0.1", 0, self.data)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def test_static_api_and_video_range(self):
        with urllib.request.urlopen(self.base) as response:
            self.assertIn(b"CVBench Studio", response.read())
        body = json.dumps({"name": "HTTP project", "classes": ["person"]}).encode()
        request = urllib.request.Request(
            f"{self.base}/api/projects",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request) as response:
            project = json.load(response)
            self.assertEqual(response.status, 201)
        clip = self.data / "clip.mp4"
        clip.write_bytes(b"0123456789")
        import_video(
            self.data, project["id"], clip, "clip.mp4",
            width=10, height=10, duration=1, fps=10,
        )
        request = urllib.request.Request(
            f"{self.base}/api/projects/{project['id']}/video",
            headers={"Range": "bytes=2-5"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(response.status, 206)
            self.assertEqual(response.read(), b"2345")
            self.assertEqual(response.headers["Content-Range"], "bytes 2-5/10")

    def test_hidden_panels_cannot_be_redisplayed_by_component_styles(self):
        with urllib.request.urlopen(f"{self.base}/style.css") as response:
            stylesheet = response.read().decode()

        self.assertIn("[hidden] { display: none !important; }", stylesheet)

    def test_server_socket_closes_when_model_queue_shutdown_fails(self):
        server = object.__new__(StudioServer)
        server.model_queue = Mock()
        server.model_queue.close.side_effect = StudioError("queue shutdown failed")
        with (
            patch("http.server.HTTPServer.server_close") as close_socket,
            self.assertRaisesRegex(StudioError, "queue shutdown failed"),
        ):
            server.server_close()
        close_socket.assert_called_once_with()
