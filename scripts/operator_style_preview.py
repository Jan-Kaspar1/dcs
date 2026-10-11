#!/usr/bin/env python3
"""Read-only design gallery over an existing simulated DCS monitor.

No plant composition, controller command, or production presentation is changed.
Only explicit monitor reads are proxied, to one configured origin. All mutation
methods and controller activation paths are refused before contacting it.
"""

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler


ROOT = Path(__file__).resolve().parents[1] / "docs/design/operator-styles"
STYLES = ("precision", "civic", "drafting", "editorial", "instrument")
READS = frozenset(("/signals", "/role", "/snapshot", "/schema", "/resources", "/history", "/journal"))
PRODUCTION_ASSETS = frozenset(("/plant-view.css", "/plant-view.js", "/lossless-json.js", "/wire-json.js"))
ASSETS = {
    "/review/gallery.css": ("gallery.css", "text/css; charset=utf-8"),
    "/review/gallery.js": ("gallery.js", "text/javascript; charset=utf-8"),
    "/review/themes.css": ("themes.css", "text/css; charset=utf-8"),
    "/review/readonly.js": ("readonly.js", "text/javascript; charset=utf-8"),
}
for name in (*STYLES, "overview"):
    ASSETS[f"/review/{name}.svg"] = (f"assets/{name}.svg", "image/svg+xml")
ASSETS["/review/overview.svg"] = ("assets/contact-sheet.svg", "image/svg+xml")
MAX_BYTES = 8 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A monitor read never needs to leave its configured origin.
        return None


def review_page(source, style):
    head = ('<link rel="stylesheet" href="/review/themes.css">'
            '<script src="/review/readonly.js"></script>')
    if "</head>" not in source or '<body class="workspace-plant">' not in source:
        raise ValueError("The upstream page does not have the supported operator shell")
    return source.replace("</head>", head + "</head>", 1).replace(
        '<body class="workspace-plant">',
        f'<body class="workspace-plant" data-review-style="{style}">', 1)


class Handler(BaseHTTPRequestHandler):
    server_version = "DCS-Design-Review/1"

    def answer(self, status, data, content_type="text/plain; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-src 'self'; frame-ancestors 'self'; form-action 'none'; base-uri 'none'; object-src 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def deny_write(self):
        self.close_connection = True
        self.answer(403, b"Design review is read-only; no request was sent to the controller.\n")

    do_POST = deny_write
    do_PUT = deny_write
    do_PATCH = deny_write
    do_DELETE = deny_write
    do_OPTIONS = deny_write
    do_HEAD = deny_write
    do_CONNECT = deny_write
    do_TRACE = deny_write

    def upstream_read(self, path):
        request = Request(self.server.upstream + path, headers={"Accept-Encoding": "identity"}, method="GET")
        try:
            response = self.server.opener.open(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                raise ValueError("Upstream read exceeds the review response bound")
            return response.status, data, response.headers.get("Content-Type", "application/octet-stream")

    def do_GET(self):
        parts = urlsplit(self.path)
        if parts.scheme or parts.netloc or len(parts.query) > 1024:
            self.answer(400, b"Unsupported review request\n")
            return
        path = parts.path
        if path == "/":
            self.answer(200, (ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif path in ASSETS:
            filename, kind = ASSETS[path]
            source = ROOT / filename
            if not source.is_file():
                self.answer(404, b"Design asset not found\n")
                return
            self.answer(200, source.read_bytes(), kind)
        elif path == "/operator" or path in READS or path in PRODUCTION_ASSETS:
            try:
                upstream_path = "/" if path == "/operator" else self.path
                status, data, kind = self.upstream_read(upstream_path)
                if path == "/operator" and status == 200:
                    # Never forward operator query parameters such as peer/pair.
                    style = parse_qs(parts.query).get("style", [STYLES[0]])[0]
                    if style not in STYLES:
                        self.answer(400, b"Unknown design style\n")
                        return
                    data = review_page(data.decode("utf-8"), style).encode("utf-8")
                self.answer(status, data, kind)
            except (URLError, TimeoutError, OSError, ValueError) as error:
                self.answer(502, f"Simulated preview unavailable: {error}\n".encode())
        else:
            self.answer(404, b"This route is not a design-review read\n")


def make_server(host, port, upstream):
    parts = urlsplit(upstream)
    if parts.scheme != "http" or not parts.hostname or parts.username or parts.password or parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError("upstream must be one plain HTTP simulated-monitor origin")
    server = ThreadingHTTPServer((host, port), Handler)
    server.upstream = upstream.rstrip("/")
    server.opener = build_opener(NoRedirect())
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1:9083")
    parser.add_argument("--upstream", default="http://127.0.0.1:9081")
    options = parser.parse_args()
    host, port = options.listen.rsplit(":", 1)
    server = make_server(host, int(port), options.upstream)
    print(f"DCS design review: http://{options.listen}/ (read-only)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
