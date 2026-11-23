#!/usr/bin/env python3
"""Receive workspace registrations; validate a live candidate before switching nginx.

The service is passive: start.sh / watchdog POST /register with X-Reg-Token.
It listens on loopback; nginx exposes /ops/register. See docs/DEPLOY.md.
"""
import hmac
import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

CONF = os.environ.get('UPSTREAM_CONF', '/etc/nginx/conf.d/cnb-ai-upstream.conf')
ENV_FILE = os.environ.get('REGISTER_ENV_FILE', '/root/.cnb/register.env')
LISTEN_PORT = int(os.environ.get('REGISTER_PORT', '9003'))
UPSTREAM_PORT = os.environ.get('UPSTREAM_PORT', '9001')
HEALTH_TIMEOUT = float(os.environ.get('REGISTER_HEALTH_TIMEOUT', '5'))
COMMAND_TIMEOUT = float(os.environ.get('REGISTER_COMMAND_TIMEOUT', '15'))
READ_TIMEOUT = float(os.environ.get('REGISTER_READ_TIMEOUT', '10'))
URI_RE = re.compile(r'^https://([a-z0-9]+)-(?:\{\{port\}\}|[0-9]+)\.cnb\.run$')
TOKEN = ''
# A file on disk does not prove nginx has loaded it. Re-apply once after this
# process starts; only a successful reload establishes this in-process cache.
APPLIED = None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_health_opener = urllib.request.build_opener(
    urllib.request.ProxyHandler({}), _NoRedirect)


def load_token():
    try:
        with open(ENV_FILE) as f:
            for line in f:
                if line.startswith('REG_TOKEN='):
                    return line.split('=', 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ''


def candidate_healthy(sub):
    url = 'https://%s-%s.cnb.run/health' % (sub, UPSTREAM_PORT)
    try:
        with _health_opener.open(url, timeout=HEALTH_TIMEOUT) as response:
            if response.status != 200:
                return False
            body = response.read(65537)
            if len(body) > 65536:
                return False
            doc = json.loads(body)
            return isinstance(doc, dict) and doc.get('status') == 'ok'
    except (OSError, ValueError):
        return False


def atomic_write(path, content):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.cnb-register-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def nginx_command(args):
    result = subprocess.run(args, capture_output=True, text=True,
                            timeout=COMMAND_TIMEOUT)
    if result.returncode:
        raise RuntimeError('%s failed: %s' % (args[0], result.stderr[-300:]))


def switch(sub):
    global APPLIED
    if not candidate_healthy(sub):
        return {'ok': False, 'error': 'candidate health check failed', 'sub': sub}
    new = 'map $request_uri $cnb_ai_upstream { default %s-%s.cnb.run:443; }\n' % (sub, UPSTREAM_PORT)
    try:
        with open(CONF) as f:
            cur = f.read()
    except FileNotFoundError:
        cur = None
    except OSError as e:
        return {'ok': False, 'error': 'cannot read current config', 'detail': str(e)[:200]}
    if cur == new and APPLIED == new:
        return {'ok': True, 'switched': False, 'sub': sub}

    published = False
    reload_attempted = False
    try:
        atomic_write(CONF, new)
        published = True
        nginx_command(['nginx', '-t'])
        reload_attempted = True
        nginx_command(['systemctl', 'reload', 'nginx'])
        APPLIED = new
        return {'ok': True, 'switched': True, 'sub': sub}
    except (OSError, subprocess.TimeoutExpired, RuntimeError) as e:
        APPLIED = None
        error = {'ok': False, 'error': 'upstream switch failed', 'detail': str(e)[:300]}
        if published:
            try:
                if cur is None:
                    os.unlink(CONF)
                else:
                    atomic_write(CONF, cur)
                # A failed/timed-out reload may already have signalled nginx.
                # Restore both disk and runtime rather than only the file.
                if reload_attempted:
                    nginx_command(['nginx', '-t'])
                    nginx_command(['systemctl', 'reload', 'nginx'])
                    APPLIED = cur
                error['rolled_back'] = True
            except (OSError, subprocess.TimeoutExpired, RuntimeError) as rollback_error:
                error['rolled_back'] = False
                error['rollback_detail'] = str(rollback_error)[:300]
        return error


class H(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(READ_TIMEOUT)

    def authorized(self):
        token_hdr = self.headers.get('X-Reg-Token', '')
        return bool(TOKEN and token_hdr and hmac.compare_digest(token_hdr, TOKEN))

    def reply(self, status, result):
        payload = json.dumps(result).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass

    def do_GET(self):
        if not self.authorized():
            return self.reply(403, {'ok': False, 'error': 'bad token'})
        self.reply(200, {'service': 'cnb-register', 'ok': True})

    def do_POST(self):
        if self.path != '/register':
            return self.reply(404, {'ok': False, 'error': 'not found'})
        if not self.authorized():
            return self.reply(403, {'ok': False, 'error': 'bad token'})
        try:
            length = int(self.headers.get('Content-Length', '0') or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 4096:
            return self.reply(400, {'ok': False, 'error': 'bad length'})
        try:
            doc = json.loads(self.rfile.read(length))
            uri = doc.get('uri', '') if isinstance(doc, dict) else ''
        except socket.timeout:
            return self.reply(408, {'ok': False, 'error': 'request body timeout'})
        except (OSError, ValueError):
            return self.reply(400, {'ok': False, 'error': 'bad json'})
        m = URI_RE.fullmatch(uri) if isinstance(uri, str) else None
        if not m:
            return self.reply(400, {'ok': False, 'error': 'uri not match PROXY_URI pattern'})
        result = switch(m.group(1))
        print('register:', json.dumps(result, ensure_ascii=False), flush=True)
        self.reply(200 if result.get('ok') else 503, result)

    def log_message(self, fmt, *args):
        pass


if __name__ == '__main__':
    TOKEN = load_token()
    HTTPServer(('127.0.0.1', LISTEN_PORT), H).serve_forever()
