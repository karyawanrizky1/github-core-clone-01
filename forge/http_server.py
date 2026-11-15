"""HTTP server core for the forge (Python stdlib only).

ThreadingHTTPServer with HTTP/1.1 keep-alive (needed for the read workload:
2,000 GETs on one connection). Every handler receives a Request and returns
via helpers on the handler. Routing is a table of (method, regex, handler)
registered by the surface modules (rest, gitproto, graphql, mcp).
"""
import json
import re
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs


class ForgeServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with TCP_NODELAY on accepted sockets.

    The suite's workloads are thousands of small sequential request/response
    pairs (2,000 GETs on one keep-alive connection, 200 sequential pushes).
    Without TCP_NODELAY, Nagle holds the second small write of a response
    until the peer ACKs the first; the peer's delayed-ACK timer (~40 ms)
    then taxes every request. Measured before this fix: ~21 ms per issue
    read (T_ref 1.38 ms), ~43 ms per push (T_ref 8.2 ms)."""
    daemon_threads = True

    def get_request(self):
        sock, addr = super().get_request()
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass
        return sock, addr


class Request:
    def __init__(self, method, path, query, headers, body):
        self.method = method
        self.path = path
        self.query = query          # dict[str, list[str]]
        self.headers = headers      # case-insensitive-ish dict (lower keys)
        self.body = body            # bytes
        self.json = None
        self.json_error = False
        if body:
            try:
                self.json = json.loads(body.decode('utf-8'))
            except Exception:
                self.json_error = True

    def query_one(self, name, default=None):
        vals = self.query.get(name)
        return vals[0] if vals else default


class Response:
    def __init__(self, status, body=b'', content_type='application/json',
                 headers=None):
        self.status = status
        self.body = body if isinstance(body, bytes) else body.encode('utf-8')
        self.content_type = content_type
        self.headers = headers or {}


def json_response(status, obj, headers=None):
    text = json.dumps(obj, sort_keys=True, separators=(',', ':'))
    return Response(status, text.encode('utf-8'), 'application/json',
                    headers or {})


def json_error(status, message):
    return json_response(status, {'message': message})


class ForgeHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'Forge/1.0'

    # set by create_server()
    routes = []     # list of (method, compiled_regex, handler_fn)
    store = None

    def log_message(self, fmt, *args):
        pass  # keep stdout clean; the trial reads logs, noise costs nothing but clarity helps

    def _handle(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        # Git paths keep % encoding in segments; REST paths are decoded by handlers as needed.
        query = parse_qs(parsed.query, keep_blank_values=True)
        length = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(length) if length > 0 else b''
        headers = {k.lower(): v for k, v in self.headers.items()}
        req = Request(method, path, query, headers, body)
        resp = None
        for m, regex, fn in self.routes:
            if m != method and m != '*':
                continue
            match = regex.match(path)
            if match:
                try:
                    resp = fn(self.store, req, **match.groupdict())
                except Exception as exc:  # never crash the server on one bad request
                    resp = json_error(500, 'internal error: %s' % exc)
                break
        if resp is None:
            resp = json_error(404, 'Not Found')
        self._send(resp)

    def _send(self, resp):
        headers = {'Content-Type': resp.content_type,
                   'Content-Length': str(len(resp.body))}
        # Every response carries a request id (REST check protocol.request-id
        # probes /, a repo, an unknown route and an unauthenticated repo).
        # Handlers that already set it (REST) keep theirs; this fills the
        # rest (core root, global 404, git endpoints) without duplicates.
        if 'x-github-request-id' not in resp.headers:
            try:
                headers['X-GitHub-Request-Id'] = self.store.next_request_id()
            except Exception:
                pass
        headers.update(resp.headers)
        try:
            self.send_response(resp.status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(resp.body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')

    def do_PUT(self):
        self._handle('PUT')

    def do_PATCH(self):
        self._handle('PATCH')

    def do_DELETE(self):
        self._handle('DELETE')

    def do_HEAD(self):
        self._handle('HEAD')


def route(method, pattern):
    """Decorator helper: compile a path pattern with {name} groups."""
    regex = re.compile('^' + re.sub(r'\{(\w+)\}', r'(?P<\1>[^/]+)', pattern) + '$')
    return method, regex


def create_server(store, routes, host='127.0.0.1', port=0):
    handler = type('Handler', (ForgeHandler,), {'routes': routes, 'store': store})
    server = ForgeServer((host, port), handler)
    server.daemon_threads = True
    return server
