"""scripts/publish_release.py sends its token only to the repository's API host: a redirect to another host (or to
plain http) is refused, never followed with the Authorization header."""
import http.server
import importlib.util
from pathlib import Path
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]


def publish_module():
    spec = importlib.util.spec_from_file_location("publish_release_for_test", ROOT / "scripts" / "publish_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def server(handler):
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_a_redirect_to_another_host_never_carries_the_token():
    seen = []

    class Other(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args):
            pass

    other = server(Other)
    target = f"http://localhost:{other.server_port}/elsewhere"

    class Api(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def log_message(self, *args):
            pass

    api = server(Api)
    try:
        client = publish_module().Client("secret-token-value")
        with pytest.raises(SystemExit) as exc:
            client.request("GET", f"http://127.0.0.1:{api.server_port}/api/v1/repos/o/r/releases/tags/v1")
        assert "redirect" in str(exc.value)
        assert "secret-token-value" not in str(exc.value)
        assert seen == []  # the other host was never asked, with or without the token
    finally:
        api.shutdown()
        other.shutdown()


def test_a_redirect_on_the_same_host_is_followed_with_the_token():
    seen = []

    class Api(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path.startswith("/moved"):
                seen.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"id": 7}')
                return
            self.send_response(301)
            self.send_header("Location", "/moved")
            self.end_headers()

        def log_message(self, *args):
            pass

    api = server(Api)
    try:
        client = publish_module().Client("secret-token-value")
        assert client.request("GET", f"http://127.0.0.1:{api.server_port}/api") == {"id": 7}
        assert seen == ["token secret-token-value"]
    finally:
        api.shutdown()
