"""HTTP layer: a small JSON API plus the single HTML page.

WHY bind 127.0.0.1 ONLY: this dashboard has no authentication and one of its
buttons can publish real posts. On WSL, binding 0.0.0.0 would expose that
button to the whole LAN (and, with WSL's port forwarding, to the Windows
host's network). Loopback-only means "whoever is already at this keyboard" —
the same trust boundary the CLI itself has.

Routing is deliberately tiny: everything is JSON, every POST is idempotent
enough to retry, and errors come back as {"error": ...} with a real status
code so the page can show the reason instead of a generic failure.
"""
from __future__ import annotations

import base64
import binascii
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import content as content_mod
from . import state as state_mod
from .content import ContentError
from .page import render_page
from .runner import (
    MODE_SPECS,
    RingBuffer,
    RunBusy,
    RunManager,
    RunRejected,
    RunUnavailable,
    live_confirmation_error,
)


def _int_param(query: dict[str, list[str]], key: str, default: int = 0) -> int:
    try:
        return int((query.get(key) or [str(default)])[0])
    except (TypeError, ValueError):
        return default


class DashboardHandler(BaseHTTPRequestHandler):
    """One handler instance per request (ThreadingHTTPServer contract)."""

    server_version = "autoposter-gui"
    protocol_version = "HTTP/1.1"

    # The page polls /api/log once a second; logging every request would bury
    # the run output the operator actually wants to read.
    def log_message(self, fmt: str, *args: Any) -> None:  # type: ignore[override]
        return

    # -- plumbing ---------------------------------------------------------
    @property
    def dashboard(self) -> Any:
        return self.server.dashboard  # type: ignore[attr-defined]

    def _send_json(self, payload: dict[str, Any],
                   status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict[str, Any]:
        """Best-effort JSON body; a malformed/absent body reads as {} so the
        route can answer with its own validation error instead of a 500."""
        try:
            raw_len = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raw_len = 0
        if raw_len <= 0:
            return {}
        raw = self.rfile.read(raw_len)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _send_bytes(self, blob: bytes, ctype: str,
                    status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(blob)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(blob)

    # -- routes -----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/":
            self._send_html(render_page())
            return
        if path == "/api/state":
            self._send_json(self.dashboard.build_state())
            return
        if path == "/api/log":
            since = _int_param(parse_qs(parsed.query), "since", 0)
            self._send_json(self.dashboard.buffer.since(max(since, 0)))
            return
        if path == "/api/health":
            self._send_json({"ok": True})
            return
        if path == "/api/content":
            self._send_json(
                content_mod.read_content(self.dashboard.paths.capture_root))
            return
        if path == "/api/content/image":
            q = parse_qs(parsed.query)
            try:
                img = content_mod.resolve_image(
                    self.dashboard.paths.capture_root,
                    (q.get("car") or [""])[0], (q.get("name") or [""])[0])
            except ContentError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return
            self._send_bytes(img.read_bytes(),
                             content_mod.image_content_type(img))
            return
        self._send_json({"error": f"no such endpoint: {path}"},
                        HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path.rstrip("/")
        payload = self._read_json_body()
        if path == "/api/run":
            self._run(payload)
            return
        if path == "/api/kill":
            self._send_json({"killed": self.dashboard.kill()})
            return
        if path == "/api/refresh-groups":
            self._spawn("groups-refresh", live=False)
            return
        if path == "/api/refresh-listings":
            self._spawn("listings-refresh", live=False)
            return
        if path.startswith("/api/content"):
            self._content(path, payload)
            return
        self._send_json({"error": f"no such endpoint: {path}"},
                        HTTPStatus.NOT_FOUND)

    def _content(self, path: str, payload: dict[str, Any]) -> None:
        """Editing data/post.txt + data/car_photos — every handler is a thin
        wrapper over poster.gui.content, where the path guards live. Any
        ContentError is the user's bad input, never a server fault: 400 +
        the reason."""
        root = self.dashboard.paths.capture_root
        try:
            if path == "/api/content/post":
                out = content_mod.save_post(root, payload.get("text", ""))
            elif path == "/api/content/car":
                out = content_mod.add_car(root, str(payload.get("name") or ""))
            elif path == "/api/content/car/delete":
                out = content_mod.delete_car(
                    root, str(payload.get("name") or ""))
            elif path == "/api/content/photo":
                blob = base64.b64decode(str(payload.get("data_b64") or ""),
                                        validate=True)
                out = content_mod.save_photo(root,
                                             str(payload.get("car") or ""),
                                             str(payload.get("name") or ""),
                                             blob)
            elif path == "/api/content/photo/delete":
                out = content_mod.delete_photo(
                    root, str(payload.get("car") or ""),
                    str(payload.get("name") or ""))
            else:
                self._send_json({"error": f"no such endpoint: {path}"},
                                HTTPStatus.NOT_FOUND)
                return
        except ContentError as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except binascii.Error:
            self._send_json({"error": "photo body is not valid base64"},
                            HTTPStatus.BAD_REQUEST)
            return
        self._send_json(out)

    def _run(self, payload: dict[str, Any]) -> None:
        mode = str(payload.get("mode") or "")
        live = bool(payload.get("live"))
        spec = MODE_SPECS.get(mode)
        if live and spec is not None and not spec["live_allowed"]:
            # No live variant of this mode exists (e.g. 'share'). Reject with
            # 400 BEFORE the confirmation gate so the operator gets the real
            # reason instead of a prompt to type PUBLICAR for a run that can
            # never publish.
            self._send_json(
                {"error": f"mode {mode!r} has no live variant "
                          f"({spec['label']} is dry-run only)", "mode": mode},
                HTTPStatus.BAD_REQUEST)
            return
        reason = live_confirmation_error(payload)
        if reason is not None:
            # 403 rather than 400: the request was well-formed but is not
            # allowed to publish without the typed confirmation phrase.
            self._send_json({"error": reason}, HTTPStatus.FORBIDDEN)
            return
        self._spawn(mode, live=live)

    def _spawn(self, mode: str, live: bool) -> None:
        try:
            started = self.dashboard.start(mode, live)
        except RunBusy as e:
            self._send_json({"error": str(e), "busy": True},
                            HTTPStatus.CONFLICT)
        except (RunRejected, RunUnavailable) as e:
            status = (HTTPStatus.NOT_IMPLEMENTED if isinstance(e, RunUnavailable)
                      else HTTPStatus.BAD_REQUEST)
            self._send_json({"error": str(e), "mode": mode}, status)
        except OSError as e:
            self._send_json({"error": f"spawn failed: {type(e).__name__}: {e}"},
                            HTTPStatus.INTERNAL_SERVER_ERROR)
        else:
            self._send_json({"ok": True, **started}, HTTPStatus.ACCEPTED)


class Dashboard:
    """Bundles the read paths with the run manager for one server instance."""

    def __init__(self, paths: state_mod.GuiPaths, manager: RunManager,
                 identity: dict[str, Any] | None = None) -> None:
        self.paths = paths
        self.manager = manager
        self.identity = identity or {}
        self.buffer: RingBuffer = manager.buffer

    def build_state(self) -> dict[str, Any]:
        manager_status = self.manager.status()
        manager_status["available"] = self.manager.available()
        data = state_mod.build_state(self.paths, self.identity, manager_status)
        # the page needs the availability map + which mode maps to which
        # button, so it can grey out an absent CLI instead of erroring on click
        data["modes"] = manager_status["available"]
        return data

    def start(self, mode: str, live: bool) -> dict[str, Any]:
        return self.manager.start(mode, live)

    def kill(self) -> bool:
        return self.manager.kill()


def create_server(paths: state_mod.GuiPaths, manager: RunManager,
                  identity: dict[str, Any] | None = None,
                  host: str = "127.0.0.1", port: int = 8765,
                  container_bind: bool = False) -> ThreadingHTTPServer:
    """Build (but do not start) the loopback-only dashboard server.

    Returning the server lets callers read `server.server_address[1]` for the
    real port — that is how the smoke test binds port 0 (ephemeral) instead of
    racing a fixed port.

    `container_bind=True` is the ONE sanctioned way out of loopback, and it
    exists for Docker only: a server on 127.0.0.1 is unreachable through a
    published port (connections arrive on the container's eth0). Inside a
    container network namespace that is safe BY CONSTRUCTION — but only as
    long as the compose mapping keeps the HOST side at 127.0.0.1
    ("127.0.0.1:8765:8765"), so the unauthenticated dashboard never meets a
    network. The flag is deliberate: typing --bind-container on a bare host
    is an informed decision, not a default.
    """
    allowed = {"127.0.0.1", "localhost", "::1"}
    if container_bind:
        allowed |= {"0.0.0.0", "::"}
    if host not in allowed:
        raise ValueError(
            f"refusing to bind {host!r}: the dashboard can publish real posts "
            "and has no authentication — loopback only (in a container use "
            "--bind-container AND keep the host mapping on 127.0.0.1)")
    dashboard = Dashboard(paths, manager, identity)
    httpd = ThreadingHTTPServer((host, port), DashboardHandler)
    httpd.dashboard = dashboard  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd
