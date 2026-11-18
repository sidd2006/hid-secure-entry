"""Kiosk dev server: serves /web and forwards API calls to the backend (same origin, no CORS needed).

    python web/serve.py                         # http://localhost:8765, API at http://127.0.0.1:8000
    python web/serve.py --port 8765 --api http://127.0.0.1:8000

Standard library only. Proxies POST /attempt/..., POST /verify and GET /status; everything else is a static file.
Request bodies are streamed through and never written to disk.
"""
import argparse
import http.server
import os
import urllib.error
import urllib.request

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY_PREFIXES = ("/attempt/", "/verify", "/enrollment/")
HOP_HEADERS = {"connection", "keep-alive", "transfer-encoding", "content-length", "host"}


def make_handler(api_base: str):
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=WEB_DIR, **kw)

        def end_headers(self):
            self.send_header("Cache-Control", "no-store")
            super().end_headers()

        def do_GET(self):
            if self.path == "/status" or self.path.startswith("/enrollment/"):
                self._proxy("GET", None)
            else:
                super().do_GET()

        def do_POST(self):
            if not self.path.startswith(PROXY_PREFIXES):
                self.send_error(404)
                return
            length = int(self.headers.get("Content-Length") or 0)
            self._proxy("POST", self.rfile.read(length) if length else b"")

        def _proxy(self, method, body):
            req = urllib.request.Request(api_base + self.path, data=body, method=method)
            for k, v in self.headers.items():
                if k.lower() not in HOP_HEADERS:
                    req.add_header(k, v)
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    self._relay(resp.status, resp.headers, resp.read())
            except urllib.error.HTTPError as e:
                self._relay(e.code, e.headers, e.read())
            except Exception as e:  # backend down, refused, timeout
                msg = f'{{"detail": "proxy: {type(e).__name__}"}}'.encode()
                self._relay(502, {"Content-Type": "application/json"}, msg)
            finally:
                body = b""  # nothing kept

        def _relay(self, status, headers, data):
            self.send_response(status)
            for k, v in headers.items():
                if k.lower() not in HOP_HEADERS:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            print("[serve]", self.address_string(), fmt % args)

    return Handler


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.api.rstrip("/")))
    print(f"[serve] kiosk on http://localhost:{args.port}  ->  API {args.api}")
    server.serve_forever()


if __name__ == "__main__":
    main()
