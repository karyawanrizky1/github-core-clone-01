"""Entry lifecycle for the forge (Python stdlib only).

Mirrors kit starter/main.ts exactly:
- MAKEFUN_ENV_URL set: bind ephemeral port, POST {port} to /register,
  poll GET /done until {"done": true}, close server, exit 0.
  Register 409 = another register already won: keep waiting.
  400/405 on register = contract error: exit 1.
  Other failures: retry with exponential backoff (250ms -> 2000ms, 30s timeout).
- MAKEFUN_ENV_URL unset: standalone mode for local practice (sdk/run.ts
  Lapis A): serve on --port / $PORT (default 8123) forever, exit 1 is NOT
  used — this is a deliberate deviation from the starter, documented in
  work/spec-runner.md, because the local runner needs a server without env.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from forge.store import Store
from forge.app import create_app_server

REQUEST_TIMEOUT = 30
RETRY_INITIAL_MS = 250
RETRY_MAX_MS = 2000


def _retry_delay(attempt):
    return min(RETRY_MAX_MS, RETRY_INITIAL_MS * (2 ** min(attempt, 8))) / 1000.0


def _http_json(url, method='GET', body=None, timeout=REQUEST_TIMEOUT):
    data = json.dumps(body).encode('utf-8') if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header('Content-Type', 'application/json')
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode('utf-8')
        return resp.status, json.loads(raw) if raw.strip() else {}


def register(env_url, port):
    attempt = 0
    while True:
        try:
            status, _body = _http_json(env_url + '/register', 'POST', {'port': port})
            if status in (200, 409):
                return
            if status in (400, 405):
                raise RuntimeError('register answered HTTP %d' % status)
        except RuntimeError:
            raise
        except Exception:
            pass
        time.sleep(_retry_delay(attempt))
        attempt += 1


def wait_for_done(env_url):
    attempt = 0
    while True:
        try:
            status, body = _http_json(env_url + '/done', 'GET')
            if status == 200 and body.get('done') is True:
                return
        except Exception:
            pass
        time.sleep(_retry_delay(attempt))
        attempt += 1


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=None)
    parser.add_argument('--host', default='127.0.0.1')
    args = parser.parse_args()

    store = Store()
    env_url = (os.environ.get('MAKEFUN_ENV_URL') or '').rstrip('/')
    if env_url:
        server = create_app_server(store, args.host, 0)
        port = server.server_address[1]
        print('[entry] listening on port %d' % port, flush=True)
        try:
            register(env_url, port)
        except RuntimeError as exc:
            print('[entry] %s' % exc, file=sys.stderr, flush=True)
            server.server_close()
            return 1
        print('[entry] registered; waiting for the env to say done', flush=True)
        import threading
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        wait_for_done(env_url)
        server.shutdown()
        server.server_close()
        print('[entry] done', flush=True)
        return 0
    # Standalone mode (local practice, no env).
    port = args.port or int(os.environ.get('PORT', '8123'))
    server = create_app_server(store, args.host, port)
    print('[entry] standalone on %s:%d (no MAKEFUN_ENV_URL)' % (args.host, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
