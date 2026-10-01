"""ASGI entry point: runs the stdlib `backend/server.py` handler under uvicorn/FastAPI for hosts that expect `app.main:app`.

Each request is rendered to raw HTTP/1.0 bytes, fed to `server.Handler` through an in-memory socket, and the response is
parsed back. Streaming routes are buffered. Run locally with: uvicorn app.main:app --port 8000
"""
import io
import os
import sys
from http.client import HTTPResponse
from pathlib import Path

from fastapi import FastAPI
from fastapi import Request, Response
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))


def load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

import server  # noqa: E402

app = FastAPI()
app.router.routes = [r for r in app.router.routes if getattr(r, "path", "") not in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect")]


class MemorySocket:
    def __init__(self, data):
        self.rfile, self.wfile = io.BytesIO(data), io.BytesIO()

    def makefile(self, mode, *_, **__):
        return self.rfile if "r" in mode else self.wfile

    def sendall(self, data):
        self.wfile.write(data)

    def close(self):
        pass


class FakeServer:
    server_name, server_port = "unprompted", 0


class ParsedSocket:
    def __init__(self, data):
        self._f = io.BytesIO(data)

    def makefile(self, *_, **__):
        return self._f


def serve(raw, client):
    sock = MemorySocket(raw)
    server.Handler(sock, client, FakeServer())
    out = sock.wfile.getvalue()
    res = HTTPResponse(ParsedSocket(out))
    res.begin()
    body = res.read()
    headers = [(k, v) for k, v in res.getheaders() if k.lower() not in ("content-length", "connection", "date", "server")]
    return res.status, headers, body


@app.on_event("startup")
def startup():
    server.init_db()


@app.api_route("/{path:path}", methods=["GET", "POST", "HEAD"])
async def proxy(path: str, request: Request):
    body = await request.body()
    host = request.headers.get("host", "")
    if host and "APP_URL" not in os.environ:  # share links need the real origin; the first request's Host decides
        scheme = request.headers.get("x-forwarded-proto") or request.url.scheme
        os.environ["APP_URL"] = "%s://%s" % (scheme, host)
    raw_path = request.scope.get("raw_path") or request.url.path.encode()  # keep %-encoding: the handler unquotes itself
    query = request.scope.get("query_string") or b""
    target = raw_path.decode("latin-1") + ("?" + query.decode("latin-1") if query else "")
    lines = ["%s %s HTTP/1.0" % (request.method, target)]
    for k, v in request.headers.items():
        if k.lower() not in ("transfer-encoding", "connection"):
            lines.append("%s: %s" % (k, v))
    if body and "content-length" not in request.headers:
        lines.append("Content-Length: %d" % len(body))
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body
    client = (request.client.host if request.client else "0.0.0.0", request.client.port if request.client else 0)
    status, headers, out = await run_in_threadpool(serve, raw, client)
    return Response(content=out, status_code=status, headers=dict(headers))
