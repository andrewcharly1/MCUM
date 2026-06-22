"""Stdlib-only local HTTP server for the MCUM dashboard."""

from __future__ import annotations

import argparse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .data_service import DashboardDataService, redact_secrets
from .pg_backend import build_backend


STATIC_DIR = Path(__file__).resolve().parent / "static"
API_ROUTES = {
    "/api/health": "get_health",
    "/api/summary": "get_summary",
    "/api/connectors": "get_connectors",
    "/api/agents": "get_agents",
    "/api/graph": "get_graph",
}
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class DashboardRequestHandler(BaseHTTPRequestHandler):
    """Serve exact read-only API and static routes."""

    server_version = "MCUMDashboard/1.0"
    data_service: DashboardDataService
    static_dir: Path

    def _headers(self, status: int, content_type: str, length: int, *, cache: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.end_headers()

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(redact_secrets(payload), ensure_ascii=True, default=str).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body), cache="no-store")
        self.wfile.write(body)

    def _send_static(self, route: str) -> None:
        filename, content_type = STATIC_ROUTES[route]
        path = self.static_dir / filename
        try:
            body = path.read_bytes()
        except OSError:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
            return
        self._headers(HTTPStatus.OK, content_type, len(body), cache="no-cache")
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        route = urlsplit(self.path).path
        if route in API_ROUTES:
            try:
                payload = getattr(self.data_service, API_ROUTES[route])()
            except Exception:  # A local dashboard must fail closed.
                self._send_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "data_unavailable"},
                )
                return
            self._send_json(HTTPStatus.OK, payload)
            return
        if route in STATIC_ROUTES:
            self._send_static(route)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802
        self._method_not_allowed()

    def _method_not_allowed(self) -> None:
        body = json.dumps({"error": "method_not_allowed"}).encode("utf-8")
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def create_server(
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    data_service: DashboardDataService | None = None,
    static_dir: str | Path | None = None,
    use_pg_backend: bool = True,
) -> ThreadingHTTPServer:
    class BoundDashboardRequestHandler(DashboardRequestHandler):
        pass

    if data_service is None and use_pg_backend:
        try:
            backend = build_backend()
            data_service = DashboardDataService(backend=backend)
        except Exception as exc:  # fallback to empty data if PG unavailable
            data_service = DashboardDataService(backend={})
    if data_service is None:
        data_service = DashboardDataService()
    BoundDashboardRequestHandler.data_service = data_service
    BoundDashboardRequestHandler.static_dir = Path(static_dir) if static_dir else STATIC_DIR
    server = ThreadingHTTPServer((host, int(port)), BoundDashboardRequestHandler)
    server.daemon_threads = True
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the local read-only MCUM dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = create_server(host=args.host, port=args.port)
    print(f"MCUM dashboard: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
