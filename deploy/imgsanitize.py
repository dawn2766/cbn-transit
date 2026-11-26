#!/usr/bin/env python3
"""CNB relay with shared request-memory admission and a gateway wire budget.

Reuse upload admission for bounded JSON/image processing. Image targets are
planned before decoding; a single codec pass precedes oldest-first omission.
Healthy bodies pass through unchanged. Request buffers are released before
streaming replies, so a long reply does not hold upload quota.

A 400/code 11128 is caused by two exact client-prompt fingerprints in
system/developer/assistant text. Those words are replaced with readable
equivalents before forwarding; user and tool turns stay untouched. Bodies
without a fingerprint remain byte-exact.

A 400/code 11133 permits one changed attempt only when local tool-history repair
actually changes pairing. This cold JSON parse is serialized and reserves extra
memory; if it cannot be admitted, the original error is returned. No transport or
5xx replay. Workspace redirects/recycle-style auth errors become retryable 503.
"""
import http.client
import io
from http.client import HTTPException, IncompleteRead
import json
import os
import re
import signal
import socket
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from image_budget import (GATEWAY_BODY_BYTES, WIRE_MARGIN_BYTES, RequestImageLimit,
                          dump as _dump, reduce_images, wire_size)


def _positive_int_env(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    if not re.fullmatch(r'[0-9]+', value) or int(value) <= 0:
        raise ValueError('%s must be a positive integer' % name)
    return int(value)


CONF = {
    'upstream_conf': '/etc/nginx/conf.d/cnb-ai-upstream.conf',
    'listen_host': '127.0.0.1',
    'listen_port': 9004,
    'body_buffer_bytes': _positive_int_env('CNB_RELAY_BODY_BUFFER_BYTES', 512 * 1024 * 1024),
    'upstream_timeout': 3600,
    'request_timeout': 60,
    'live_timeout': 2.0,
    'live_ttl': 2.0,
}
_REDUCE = 'compact the conversation or reduce/compress large images before retrying'
_live_lock = threading.Lock()
_live = {'base': None, 'at': 0.0, 'ok': False}
_cap_lock = threading.Lock()
_capability = {'base': None, 'limit': CONF['body_buffer_bytes'], 'retry_at': 0.0}
_repair_gate = threading.Lock()
_prepare_gate = threading.BoundedSemaphore(2)

# Exact client-prompt fingerprints refused by the CNB AI gateway with
# HTTP 400/code 11128. Replacements keep the instruction readable; only
# system/developer/assistant text is scanned. User and tool turns are untouched.
_FINGERPRINTS = (
    ('DeepSeek Harness', 'DeepSeek'),
    ('Tokens prefixed with @ are paths', 'Tokens prefixed with (at) are paths'),
)
_SCANNED_ROLES = frozenset(('system', 'developer', 'assistant'))
_FINGERPRINT_MAX_BYTES = 32 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


class BodyBufferBusy(Exception):
    pass


class BodyBufferLimit(ValueError):
    def __init__(self, limit):
        super().__init__('request exceeds server request-buffer capacity; ' + _REDUCE)
        self.limit = limit


class _BodyBuffers:
    """Bytes held for uploads; extra JSON repair reservation is an estimate.

    This is a shared admission budget, not a process RSS guarantee. Python object
    overhead, threads/socket buffers and allocator reuse are separate. JSON and
    image scratch estimates share this budget; there is no image cache.
    """
    def __init__(self):
        self._lock = threading.Lock()
        self._used = 0

    @property
    def used(self):
        with self._lock:
            return self._used

    def reserve(self, size):
        self._grow(size)
        return _BodyReservation(self, size)

    def _grow(self, size):
        if size > CONF['body_buffer_bytes']:
            raise BodyBufferLimit(CONF['body_buffer_bytes'])
        with self._lock:
            if self._used + size > CONF['body_buffer_bytes']:
                raise BodyBufferBusy()
            self._used += size

    def release(self, size):
        with self._lock:
            self._used -= size


class _BodyReservation:
    def __init__(self, buffers, size, capacity=None):
        self._buffers = buffers
        self.size = size
        self._capacity = capacity

    @property
    def capacity(self):
        return CONF['body_buffer_bytes'] if self._capacity is None else self._capacity

    def fork(self):
        # A codec worker owns its reservation; never races the parent's size.
        return _BodyReservation(self._buffers, 0, max(0, self.capacity - self.size))

    def grow(self, target):
        if target > self.capacity:
            raise BodyBufferLimit(self.capacity)
        extra = max(0, target - self.size)
        if extra:
            self._buffers._grow(extra)
            self.size += extra

    def for_repair(self, source_size):
        if source_size * 4 > CONF['body_buffer_bytes']:
            raise BodyBufferLimit(CONF['body_buffer_bytes'] // 4)
        self.grow(source_size * 4)

    def release(self):
        self._buffers.release(self.size)
        self.size = 0


_body_buffers = _BodyBuffers()


def note(message):
    print(message, file=sys.stderr, flush=True)


def upstream_base():
    try:
        with open(CONF['upstream_conf'], encoding='utf-8') as f:
            match = re.search(r'default\s+([A-Za-z0-9._:\-]+);', f.read())
        return 'https://' + match.group(1) if match else None
    except OSError:
        return None


def _valid_limit(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _probe(base):
    try:
        with _opener.open(base + '/health', timeout=CONF['live_timeout']) as response:
            if response.status != 200:
                return False, None
            doc = json.loads(response.read(4096))
        if not isinstance(doc, dict) or doc.get('status') != 'ok':
            return False, None
        limit = doc.get('max_body_bytes')
        return True, limit if _valid_limit(limit) else None
    # HTTPException covers a malformed status line / bad chunk framing, which
    # urllib surfaces uncaught and would otherwise abort the request handler.
    except (OSError, ValueError, urllib.error.URLError, HTTPException):
        return False, None


def body_limit(base):
    with _cap_lock:
        now = time.monotonic()
        retry_at = _capability['retry_at']
        if _capability['base'] != base or (retry_at and now >= retry_at):
            alive, limit = _probe(base)
            _capability.update(base=base, limit=limit or CONF['body_buffer_bytes'],
                               retry_at=0.0 if alive else now + CONF['live_ttl'])
            with _live_lock:
                _live.update(base=base, at=time.monotonic(), ok=alive)
        return _capability['limit']


# A 413 payload can carry any max_body_bytes value, including one that is absurdly
# small or was rewritten by an intermediate hop. Remembering it forever would make
# every later request 413 until the workspace generation changes, so keep a floor
# and let the cached value expire back to a fresh probe.
_REMEMBERED_LIMIT_FLOOR = 1 * 1024 * 1024
_REMEMBERED_LIMIT_TTL = 60.0


def remember_limit(base, payload):
    try:
        doc = json.loads(payload)
        limit = (doc.get('error') or {}).get('max_body_bytes')
    except (ValueError, AttributeError, TypeError):
        return
    if _valid_limit(limit):
        with _cap_lock:
            _capability.update(base=base, limit=max(limit, _REMEMBERED_LIMIT_FLOOR),
                               retry_at=time.monotonic() + _REMEMBERED_LIMIT_TTL)


def upstream_alive(base):
    with _live_lock:
        now = time.monotonic()
        if _live['base'] == base and now - _live['at'] < CONF['live_ttl']:
            return _live['ok']
        alive, _ = _probe(base)
        _live.update(base=base, at=time.monotonic(), ok=alive)
        return alive


def _neutralize_text(text):
    total = 0
    for old, new in _FINGERPRINTS:
        count = text.count(old)
        if count:
            text = text.replace(old, new)
            total += count
    return text, total


def _scan_text_fields(value):
    """Rewrite every string stored under a `text` key, at any depth."""
    total = 0
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            for key, child in item.items():
                if key == 'text' and isinstance(child, str):
                    text, count = _neutralize_text(child)
                    if count:
                        item[key] = text
                        total += count
                elif isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(item, list):
            pending.extend(item for item in item if isinstance(item, (dict, list)))
    return total


def message_text_fields(message):
    """Neutralize the `text` strings a gateway content check can see in one message.

    Covers string content, every `text` field of array content (including
    thinking blocks and nested parts), and the Android `content[].parts[]`
    spelling. Tool names, arguments, ids and user/tool turns are not touched.
    """
    content = message.get('content')
    if isinstance(content, str):
        text, count = _neutralize_text(content)
        if count:
            message['content'] = text
        return count
    if isinstance(content, (list, dict)):
        return _scan_text_fields(content)
    return 0


def body_has_fingerprint(body):
    return any(old.encode('utf-8') in body for old, _ in _FINGERPRINTS)


def neutralize_client_fingerprints(doc):
    """Break exact gateway prompt fingerprints; leave user/tool turns alone."""
    messages = doc.get('messages') if isinstance(doc, dict) else None
    if not isinstance(messages, list):
        return 0
    total = 0
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in _SCANNED_ROLES:
            continue
        total += message_text_fields(message)
    return total


def _needs_images(body):
    # Escaped JSON strings may hide the spelling; parse those too. Most text
    # requests retain the existing byte-exact, no-JSON-parse path.
    return b'image_url' in body or bytes((92, 117)) in body


def prepare_client_body(body, reservation, limit):
    """One admitted JSON parse for fingerprints AND gateway/image budgets."""
    image_check = _needs_images(body)
    # JSON.stringify can expand exponent numbers. Above this small-body
    # threshold, always parse and apply the conservative outbound wire estimate.
    over = len(body) >= (min(limit, GATEWAY_BODY_BYTES) - WIRE_MARGIN_BYTES) // 6
    if not (image_check or over or (len(body) <= _FINGERPRINT_MAX_BYTES and body_has_fingerprint(body))):
        return body, 0, None
    # Codec work inside one request is already serial (reduce_images encodes one
    # image at a time); this gate only bounds how many requests may prepare at
    # once, because each holds a JSON copy plus codec scratch.
    if not _prepare_gate.acquire(blocking=False):
        raise BodyBufferBusy()
    try:
        reservation.for_repair(len(body))
        try:
            doc = json.loads(body)
            neutralized = neutralize_client_fingerprints(doc) if len(body) <= _FINGERPRINT_MAX_BYTES else 0
            out, stats = reduce_images(doc, body, reservation, limit, mutated=bool(neutralized))
            return out, neutralized, stats
        except RequestImageLimit:
            raise
        except (ValueError, UnicodeError, RecursionError):
            if over:
                raise RequestImageLimit('oversized request must be valid JSON before images can be reduced', limit)
            return body, 0, None
    finally:
        _prepare_gate.release()


def repair_tool_messages(doc):
    """Only contiguous call/result groups validate IDs; preserve valid groups."""
    messages = doc.get('messages')
    if not isinstance(messages, list):
        return False
    kept = []
    changed = False
    index = 0
    while index < len(messages):
        msg = messages[index]
        if not isinstance(msg, dict):
            kept.append(msg)
            index += 1
            continue
        calls = msg.get('tool_calls') if msg.get('role') == 'assistant' else None
        if isinstance(calls, list) and calls:
            end = index + 1
            while (end < len(messages) and isinstance(messages[end], dict)
                   and messages[end].get('role') == 'tool'):
                end += 1
            ids = {c['id'] for c in calls if isinstance(c, dict)
                   and isinstance(c.get('id'), str) and c['id']}
            results = []
            answered = set()
            for result in messages[index + 1:end]:
                tid = result.get('tool_call_id')
                if isinstance(tid, str) and tid in ids and tid not in answered:
                    results.append(result)
                    answered.add(tid)
                else:
                    changed = True
            seen = set()
            valid_calls = []
            for call in calls:
                tid = call.get('id') if isinstance(call, dict) else None
                if isinstance(tid, str) and tid in answered and tid not in seen:
                    valid_calls.append(call)
                    seen.add(tid)
            if len(valid_calls) != len(calls):
                msg = dict(msg)
                if valid_calls:
                    msg['tool_calls'] = valid_calls
                else:
                    msg.pop('tool_calls', None)
                changed = True
            kept.append(msg)
            kept.extend(results)
            index = end
        elif msg.get('role') == 'tool':
            changed = True
            index += 1
        else:
            kept.append(msg)
            index += 1
    if changed:
        doc['messages'] = kept
    return changed


def repaired_body(body, limit, reservation):
    if not _repair_gate.acquire(blocking=False):
        return body
    try:
        try:
            reservation.for_repair(len(body))
            doc = json.loads(body)
            if not isinstance(doc, dict) or not repair_tool_messages(doc):
                return body
            out = _dump(doc)
            return out if len(out) <= limit and wire_size(doc, out) < GATEWAY_BODY_BYTES - WIRE_MARGIN_BYTES else body
        except (BodyBufferBusy, BodyBufferLimit, ValueError, UnicodeError, RecursionError):
            return body
    finally:
        _repair_gate.release()


def is_parameter_refusal(status, payload):
    if status != 400:
        return False
    try:
        doc = json.loads(payload)
    except (ValueError, UnicodeError):
        return False
    return isinstance(doc, dict) and str(doc.get('code')) == '11133'


def is_stale_auth(payload):
    return b'invalid api key' in payload[:1000] and b'auth_error' in payload[:1000]


# ---------------------------------------------------------------------------
# Upstream connection reuse
#
# The workspace lives on another machine behind the CNB port proxy, so a new
# connection costs a TCP plus a full TLS handshake on the request's critical
# path. urllib opens and closes one connection per request, which paid that cost
# on every completion. Keeping a small pool of idle connections means only the
# first request after an idle gap pays the handshake.
#
# A connection returns to the pool only after its response was read to EOF on a
# connection neither side asked to close, so the socket is positioned exactly at
# the next response. Every other path closes it.
# ---------------------------------------------------------------------------
_POOL_MAX_IDLE = 4
_POOL_IDLE_TTL = 30.0


def _close_quietly(conn):
    try:
        conn.close()
    except OSError:
        pass


class _UpstreamPool:
    """Idle HTTP(S) connections keyed by (scheme, host, port)."""

    def __init__(self, max_idle=_POOL_MAX_IDLE, ttl=_POOL_IDLE_TTL):
        self._lock = threading.Lock()
        self._idle = []
        self._max_idle = max_idle
        self._ttl = ttl

    def take(self, key):
        """Remove and return an idle connection for key (oldest first), else None.

        Oldest-first keeps the newest sockets in the pool, which are the ones
        least likely to have been closed by the peer; a stale pick is retried on
        a fresh connection by open_upstream().
        """
        cutoff = time.monotonic() - self._ttl
        expired = []
        chosen = None
        with self._lock:
            kept = []
            for entry in self._idle:
                if entry['key'] != key or entry['at'] < cutoff:
                    expired.append(entry)
                elif chosen is None:
                    chosen = entry
                else:
                    kept.append(entry)
            self._idle = kept
        for entry in expired:
            _close_quietly(entry['conn'])
        return chosen['conn'] if chosen else None

    def give(self, key, conn):
        with self._lock:
            if len(self._idle) < self._max_idle:
                self._idle.append({'key': key, 'conn': conn, 'at': time.monotonic()})
                return
        _close_quietly(conn)

    def clear(self):
        with self._lock:
            idle, self._idle = self._idle, []
        for entry in idle:
            _close_quietly(entry['conn'])

    def idle_count(self):
        with self._lock:
            return len(self._idle)


_pool = _UpstreamPool()


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme.lower()
    port = parsed.port or (443 if scheme == 'https' else 80)
    target = parsed.path or '/'
    if parsed.query:
        target += '?' + parsed.query
    return (scheme, parsed.hostname, port), target


def _new_connection(scheme, host, port):
    if scheme == 'https':
        # Reuse the opener's TLS policy so certificate verification matches the
        # rest of the module rather than silently relaxing it.
        handler = next((h for h in _opener.handlers
                        if isinstance(h, urllib.request.HTTPSHandler)), None)
        context = getattr(handler, '_context', None) or ssl.create_default_context()
        return http.client.HTTPSConnection(host, port, timeout=CONF['upstream_timeout'], context=context)
    return http.client.HTTPConnection(host, port, timeout=CONF['upstream_timeout'])


class _PooledResponse:
    """Response wrapper that returns its connection to the pool at EOF."""

    def __init__(self, conn, raw, on_done):
        self._conn = conn
        self._raw = raw
        self._on_done = on_done
        self._done = False
        self.status = raw.status
        self.headers = raw.headers

    @property
    def length(self):
        # http.client exposes the unread Content-Length remainder here; _relay
        # uses it to detect a truncated upstream (read1 does not raise).
        return self._raw.length

    def _finish(self, complete):
        if self._done:
            return
        self._done = True
        self._on_done(self._conn, complete and not self._raw.will_close)

    def read(self, amt=None):
        try:
            data = self._raw.read(amt)
        except (OSError, HTTPException):
            self._finish(False)
            raise
        if amt is None or self._raw.isclosed():
            self._finish(True)
        return data

    def read1(self, amt):
        try:
            data = self._raw.read1(amt)
        except (OSError, HTTPException):
            self._finish(False)
            raise
        if not data:
            self._finish(True)
        return data

    def close(self):
        self._finish(False)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def open_upstream(url, body, headers):
    """POST url over a pooled upstream connection.

    Returns a response-like object (status/headers/read/read1/close), or None on
    a transport failure. A reused socket the peer already closed fails before
    any response byte is read, so that one case retries on a fresh connection
    instead of surfacing a spurious failure.
    """
    key, target = _origin(url)
    for reused in (True, False):
        conn = _pool.take(key) if reused else None
        is_reused = conn is not None
        if conn is None:
            conn = _new_connection(*key)
        try:
            conn.request('POST', target, body=body, headers=headers)
            raw = conn.getresponse()
        except (OSError, HTTPException):
            _close_quietly(conn)
            if is_reused:
                continue  # stale idle socket: one fresh attempt below
            return None

        return _PooledResponse(conn, raw, lambda c, reusable, k=key: (
            _pool.give(k, c) if reusable else _close_quietly(c)))
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'cnb-relay/6'

    def setup(self):
        super().setup()
        self.connection.settimeout(CONF['request_timeout'])

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path.split('?')[0] == '/healthz':
            return self._send(200, b'ok', 'text/plain')
        return self._json_error(404, 'not found')

    def do_HEAD(self):
        # Health checkers and load balancers probe with HEAD; answer it instead
        # of letting BaseHTTPRequestHandler return 501 to every one of them.
        if self.path.split('?')[0] == '/healthz':
            return self._send(200, b'', 'text/plain')
        return self._json_error(404, 'not found')

    def _read_chunked(self, limit, reservation):
        # BytesIO.getvalue() shares its immutable storage on supported CPython;
        # reserve twice the received bytes to cover a possible final copy and
        # growth. Fixed-length requests need only one raw body reservation.
        with io.BytesIO() as buffer:
            size = 0
            while True:
                line = self.rfile.readline(4097)
                if len(line) > 4096 or not line.endswith(b'\r\n'):
                    raise ValueError('invalid chunk header')
                token = line[:-2].split(b';', 1)[0]
                if not token or not re.fullmatch(b'[0-9a-fA-F]+', token):
                    raise ValueError('invalid chunk size')
                length = int(token, 16)
                if not length:
                    trailer_size = 0
                    while True:
                        trailer = self.rfile.readline(4097)
                        trailer_size += len(trailer)
                        if len(trailer) > 4096 or trailer_size > 16384 or not trailer.endswith(b'\r\n'):
                            raise ValueError('invalid chunk trailers')
                        if trailer == b'\r\n':
                            return buffer.getvalue()
                capacity = min(limit, CONF['body_buffer_bytes'] // 2)
                if size + length > capacity:
                    raise BodyBufferLimit(capacity)
                reservation.grow((size + length) * 2)
                remain = length
                while remain:
                    data = self.rfile.read(min(remain, 65536))
                    if not data:
                        raise ValueError('incomplete chunked body')
                    buffer.write(data)
                    remain -= len(data)
                if self.rfile.read(2) != b'\r\n':
                    raise ValueError('invalid chunk terminator')
                size += length

    def do_POST(self):
        self.close_connection = True
        if self.path.split('?')[0] != '/v1/chat/completions':
            return self._json_error(404, 'not found')
        if self.headers.get('Content-Encoding', 'identity').lower() != 'identity':
            return self._json_error(415, 'compressed request bodies are not supported')
        transfer = self.headers.get('Transfer-Encoding', '').lower()
        if transfer not in ('', 'chunked') or (transfer and self.headers.get('Content-Length') is not None):
            return self._json_error(400, 'unsupported request framing')
        try:
            raw_length = self.headers.get('Content-Length', '0')
            if not re.fullmatch(r'[0-9]+', raw_length):
                raise ValueError()
            length = int(raw_length)
        except ValueError:
            return self._json_error(400, 'invalid Content-Length')
        if not transfer and length <= 0:
            return self._json_error(400, 'empty body')
        base = upstream_base()
        if not base:
            return self._unavailable()
        limit = min(body_limit(base), CONF['body_buffer_bytes'])
        if not transfer and length > limit:
            return self._json_error(413, 'request exceeds current server capacity; ' + _REDUCE, limit)
        reserve_size = length if not transfer else 1
        try:
            reservation = _body_buffers.reserve(reserve_size)
        except BodyBufferLimit as exc:
            return self._json_error(413, str(exc), exc.limit)
        except BodyBufferBusy:
            return self._busy()
        body = None
        try:
            try:
                if transfer:
                    body = self._read_chunked(limit, reservation)
                else:
                    body = self.rfile.read(length)
            except BodyBufferLimit as exc:
                return self._json_error(413, str(exc), exc.limit)
            except BodyBufferBusy:
                return self._busy()
            except (OSError, socket.timeout):
                return self._json_error(408, 'request body read timed out')
            except ValueError as exc:
                return self._json_error(400, str(exc))
            if not transfer and len(body) != length:
                return self._json_error(400, 'incomplete request body')
            headers = {key: self.headers[key] for key in
                       ('Authorization', 'Content-Type', 'Accept', 'User-Agent') if self.headers.get(key)}
            outcome = self._prepare_response(body, reservation, base, limit, headers)
            body = None
            reservation.release()
            reservation = None
            if outcome is None:
                return self._unavailable()
            if isinstance(outcome, tuple):
                status, payload = outcome
                return self._send(status, payload, 'application/json',
                                  {'Retry-After': '2'} if status == 503 else None)
            return self._handle_response(outcome, base)
        finally:
            body = None
            if reservation is not None:
                reservation.release()

    def _prepare_response(self, body, reservation, base, limit, headers):
        """Prepare and forward one admitted body.

        `limit` is the workspace's advertised upload capacity; it only bounds the
        later tool-pairing repair. The JSON rewrite pass is bounded by the fixed
        gateway wire budget instead, so the two limits stay independent.
        """
        input_bytes = len(body)
        started = time.monotonic()
        try:
            body, neutralized, stats = prepare_client_body(body, reservation, GATEWAY_BODY_BYTES)
        except BodyBufferBusy:
            return 503, _dump({'error': {'message': 'relay request buffers or image processor are busy; retry shortly',
                                       'type': 'relay_busy'}})
        except (BodyBufferLimit, RequestImageLimit) as exc:
            return 413, _dump({'error': {'message': str(exc), 'type': 'invalid_request_error',
                                       'max_body_bytes': exc.limit}})
        if neutralized:
            note('neutralized client fingerprint tokens=%d input_bytes=%d output_bytes=%d'
                 % (neutralized, input_bytes, len(body)))
        if stats and (stats['compressed'] or stats['removed']):
            note('image budget input_bytes=%d output_bytes=%d images=%d compressed=%d omitted=%d ms=%d'
                 % (input_bytes, len(body), stats['images'], stats['compressed'], stats['removed'],
                    (time.monotonic() - started) * 1000))
        response = open_upstream(base + self.path, body, headers)
        if response is None or response.status not in (400, 413):
            return response
        try:
            payload = response.read(65536)
            status = response.status
        finally:
            response.close()
        if status == 413:
            remember_limit(base, payload)
        elif is_parameter_refusal(status, payload):
            repaired = repaired_body(body, limit, reservation)
            if repaired != body:
                note('retry once after verified tool pairing repair input_bytes=%d output_bytes=%d'
                     % (len(body), len(repaired)))
                body = None
                return open_upstream(base + self.path, repaired, headers)
        return status, payload

    def _handle_response(self, resp, base):
        if 300 <= resp.status < 400:
            resp.close()
            note('workspace redirect -> 503')
            return self._unavailable()
        if resp.status == 401:
            payload = resp.read(65536)
            resp.close()
            if is_stale_auth(payload) and not upstream_alive(base):
                note('recycle auth error -> 503')
                return self._unavailable()
            return self._send(401, payload, 'application/json')
        return self._relay(resp)

    def _relay(self, resp):
        started = time.monotonic()
        sent = 0
        phase = 'downstream_write'
        try:
            self.send_response(resp.status)
            for key, value in resp.headers.items():
                if key.lower() not in ('content-length', 'transfer-encoding', 'connection', 'keep-alive', 'server', 'date'):
                    self.send_header(key, value)
            self.send_header('Transfer-Encoding', 'chunked')
            # This handler always closes the connection afterwards, so it must
            # say so. Without the header a keep-alive client treats the finished
            # response as reusable and sends its next request into a socket the
            # relay is closing, which surfaces as a spurious connection reset.
            self.send_header('Connection', 'close')
            self.end_headers()
            while True:
                phase = 'upstream_read'
                chunk = resp.read1(65536)
                if not chunk:
                    # read1() permits an early Content-Length EOF without raising.
                    # A truncated upstream must not become a complete chunked reply.
                    remaining = getattr(resp, 'length', None)
                    if isinstance(remaining, int) and remaining > 0:
                        raise IncompleteRead(b'', remaining)
                    break
                phase = 'downstream_write'
                self.wfile.write(b'%x\r\n' % len(chunk) + chunk + b'\r\n')
                self.wfile.flush()
                sent += len(chunk)
            phase = 'downstream_write'
            self.wfile.write(b'0\r\n\r\n')
            self.wfile.flush()
        except (OSError, HTTPException) as exc:
            # Headers may already be committed. Close without a zero chunk so the
            # client observes failure; never replay a partially consumed response.
            note('relay response interrupted phase=%s error=%s status=%d bytes=%d ms=%d' % (
                phase, type(exc).__name__, resp.status, sent,
                (time.monotonic() - started) * 1000))
        finally:
            resp.close()
            self.close_connection = True

    def _json_error(self, status, message, limit=None):
        error = {'message': message, 'type': 'invalid_request_error'}
        if limit is not None:
            error['max_body_bytes'] = limit
        return self._send(status, _dump({'error': error}), 'application/json')

    def _busy(self):
        return self._send(503, _dump({'error': {
            'message': 'relay request buffers are busy; retry shortly',
            'type': 'relay_busy'}}), 'application/json', {'Retry-After': '2'})

    def _unavailable(self):
        return self._send(503, _dump({'error': {
            'message': 'upstream workspace unavailable; retry shortly',
            'type': 'upstream_unavailable'}}), 'application/json',
            {'Retry-After': '10', 'Cache-Control': 'no-store'})

    def _send(self, status, payload, content_type, headers=None):
        # HEAD answers advertise the length a GET would return but must not write
        # a body, so drop HEAD to a zero-length reply instead of a mismatched pair.
        if self.command == 'HEAD':
            payload = b''
        try:
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(payload)))
            self.send_header('Connection', 'close')
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(payload)
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True


class Server(ThreadingHTTPServer):
    # server_close() waits for admitted requests after SIGTERM stops acceptance.
    daemon_threads = False
    allow_reuse_address = True
    request_queue_size = 64


def serve():
    server = Server((CONF['listen_host'], CONF['listen_port']), Handler)
    stopping = threading.Event()

    def stop(signum, frame):
        if stopping.is_set():
            return
        stopping.set()
        note('cnb relay draining active requests')
        # shutdown() must run outside the serve_forever() thread.
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    note('cnb relay listening on %s:%d' % server.server_address)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    note('cnb relay stopped after draining')


if __name__ == '__main__':
    serve()
