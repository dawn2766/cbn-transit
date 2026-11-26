"""Offline relay regressions. Bodies and images are synthetic; no live traffic.

Ported from cnb2dsh/test/imgsanitize_test.py and adapted to the current
cbn-transit relay:

* ``compatible_client_body(body, reservation)`` is now
  ``prepare_client_body(body, reservation, limit)`` returning
  ``(body, neutralized, stats)``.
* upstream calls go through ``open_upstream`` (a pooled http.client
  connection), no longer through a urllib opener.
* the relay now applies the 10 MiB gateway wire/image budget. A fake base64
  blob such as ``c3ludGhldGlj`` is not decodable, so the image guard omits it
  even when the body is small; image-bearing passthrough assertions therefore
  use a real 4x4 PNG, which the budget leaves byte-exact.
"""
import base64
import concurrent.futures
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import random
import socket
import sys
import threading
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
# image_budget lives next to imgsanitize.py; importing the relay by path does
# not put deploy/ on sys.path on its own.
sys.path.insert(0, str(ROOT / 'deploy'))
SOURCE = ROOT / 'deploy' / 'imgsanitize.py'
spec = importlib.util.spec_from_file_location('relay_under_test', SOURCE)
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)

import image_budget  # noqa: E402  (imported after the sys.path insert above)

# A real 4x4 PNG (95 decoded bytes). Small enough that the image budget leaves
# it untouched, so bodies containing it can still be asserted byte-exact.
TINY_PNG = ('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAQAAAAECAIAAAAmkwkpAAAAEUlE'
            'QVR4nGP8z4AATEhsPBwAM9EBBzDn4UwAAAAASUVORK5CYII=')

MIB = 1024 * 1024


def response(status, payload=b'', headers=None):
    result = mock.Mock(status=status)
    result.headers = headers or {}
    result.read.return_value = payload
    result.read1.side_effect = [payload, b'']
    return result


def prepared_handler():
    handler = relay.Handler.__new__(relay.Handler)
    handler.path = '/v1/chat/completions'
    return handler


def tool_repair_body():
    # A real tiny image: the reduce pass leaves it byte-exact, so these tests
    # observe tool-pairing repair rather than image omission.
    doc = {'model': 'deepseek-v4.1-flash', 'messages': [
        {'role': 'assistant', 'tool_calls': [{'id': 'call-1'}]},
        {'role': 'tool', 'tool_call_id': 'orphan', 'content': 'discarded'},
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': TINY_PNG}}]},
    ]}
    return relay._dump(doc), TINY_PNG


def reset_relay_state():
    """Isolate admission, pool and capability state between tests.

    The relay keeps these as module globals; a failing test must never let a
    held reservation or an acquired gate leak into the next one.
    """
    relay._body_buffers = relay._BodyBuffers()
    relay._prepare_gate = threading.BoundedSemaphore(2)
    relay._repair_gate = threading.Lock()
    relay._capability.update(base=None, limit=relay.CONF['body_buffer_bytes'], retry_at=0.0)
    relay._live.update(base=None, at=0.0, ok=False)
    relay._pool.clear()


class RelayStateMixin:
    def setUp(self):
        reset_relay_state()


class RelayTests(RelayStateMixin, unittest.TestCase):
    def test_healthy_request_is_forwarded_as_exact_raw_bytes(self):
        body = b'not JSON\x00\xff\r\n exact payload'
        reservation = relay._body_buffers.reserve(len(body))
        upstream = response(200)
        with mock.patch.object(relay, 'open_upstream', return_value=upstream) as opening:
            result = prepared_handler()._prepare_response(
                body, reservation, 'https://workspace', 1024, {'Content-Type': 'application/octet-stream'})
        self.assertIs(result, upstream)
        self.assertEqual(opening.call_args.args[1], body)
        self.assertEqual(opening.call_count, 1)
        reservation.release()
        self.assertEqual(relay._body_buffers.used, 0)

    def test_body_buffer_setting_is_positive_integer(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(relay._positive_int_env('CNB_RELAY_BODY_BUFFER_BYTES', 256), 256)
        for value in ('0', '-1', '1.5', '', 'true'):
            with self.subTest(value=value), mock.patch.dict(os.environ, {'CNB_RELAY_BODY_BUFFER_BYTES': value}):
                with self.assertRaises(ValueError):
                    relay._positive_int_env('CNB_RELAY_BODY_BUFFER_BYTES', 256)
        with mock.patch.dict(os.environ, {'CNB_RELAY_BODY_BUFFER_BYTES': '123'}):
            self.assertEqual(relay._positive_int_env('CNB_RELAY_BODY_BUFFER_BYTES', 256), 123)

    @staticmethod
    def fingerprint_body():
        return relay._dump({'model': 'deepseek-v4.1-flash', 'messages': [
            {'role': 'system', 'content': 'You are an AI agent powered by DeepSeek Harness.'},
            {'role': 'developer', 'content': [
                {'type': 'text', 'text': 'Tokens prefixed with @ are paths.'},
                {'type': 'image_url', 'image_url': {'url': TINY_PNG}},
            ]},
            {'role': 'assistant', 'content': 'DeepSeek Harness answered.'},
            {'role': 'user', 'content': 'DeepSeek Harness and Tokens prefixed with @ are paths.'},
            {'role': 'tool', 'content': 'DeepSeek Harness'},
        ]})

    def test_fingerprints_are_neutralized_only_in_scanned_roles(self):
        doc = json.loads(self.fingerprint_body())
        self.assertEqual(relay.neutralize_client_fingerprints(doc), 3)
        self.assertEqual(doc['messages'][0]['content'], 'You are an AI agent powered by DeepSeek.')
        self.assertEqual(doc['messages'][1]['content'][0]['text'], 'Tokens prefixed with (at) are paths.')
        self.assertEqual(doc['messages'][1]['content'][1]['type'], 'image_url')
        self.assertEqual(doc['messages'][2]['content'], 'DeepSeek answered.')
        self.assertEqual(doc['messages'][3]['content'],
                         'DeepSeek Harness and Tokens prefixed with @ are paths.')
        self.assertEqual(doc['messages'][4]['content'], 'DeepSeek Harness')

    def test_nested_and_non_string_text_fields_are_scanned(self):
        doc = {'messages': [
            {'role': 'assistant', 'content': [
                {'type': 'thinking', 'text': 'DeepSeek Harness reasoning'},
                {'type': 'text', 'text': 'DeepSeek Harness answer'},
            ]},
            {'role': 'system', 'content': {'type': 'text', 'text': 'DeepSeek Harness dict'}},
            {'role': 'developer', 'content': [{'type': 'text', 'parts': [
                {'type': 'text', 'text': 'DeepSeek Harness parts'}]}]},
            {'role': 'assistant', 'content': None, 'tool_calls': [
                {'function': {'name': 'f', 'arguments': '{"t":"DeepSeek Harness"}'}}]},
        ]}
        self.assertEqual(relay.neutralize_client_fingerprints(doc), 4)
        self.assertEqual(doc['messages'][0]['content'][0]['text'], 'DeepSeek reasoning')
        self.assertEqual(doc['messages'][0]['content'][1]['text'], 'DeepSeek answer')
        self.assertEqual(doc['messages'][1]['content']['text'], 'DeepSeek dict')
        self.assertEqual(doc['messages'][2]['content'][0]['parts'][0]['text'], 'DeepSeek parts')
        self.assertEqual(doc['messages'][3]['tool_calls'][0]['function']['arguments'],
                         '{"t":"DeepSeek Harness"}')

    def test_fingerprint_body_is_rewritten_before_upstream(self):
        body = self.fingerprint_body()
        reservation = relay._body_buffers.reserve(len(body))
        upstream = response(200)
        with mock.patch.object(relay, 'open_upstream', return_value=upstream) as opening:
            result = prepared_handler()._prepare_response(
                body, reservation, 'https://workspace', 1024, {'Content-Type': 'application/json'})
        self.assertIs(result, upstream)
        sent = opening.call_args.args[1]
        self.assertNotEqual(sent, body)
        fixed = json.loads(sent)
        self.assertEqual(fixed['messages'][0]['content'], 'You are an AI agent powered by DeepSeek.')
        self.assertEqual(fixed['messages'][1]['content'][0]['text'], 'Tokens prefixed with (at) are paths.')
        # The tiny healthy image must survive the rewrite untouched.
        self.assertEqual(fixed['messages'][1]['content'][1]['image_url']['url'], TINY_PNG)
        self.assertEqual(opening.call_count, 1)
        reservation.release()
        self.assertEqual(relay._body_buffers.used, 0)

    def test_body_without_fingerprint_is_byte_exact(self):
        body = b'not JSON\x00\xff\r\n exact payload'
        reservation = relay._body_buffers.reserve(len(body))
        # Current API: (body, neutralized, stats); stats is None when the body
        # never needed the JSON/image path at all.
        sent, changed, stats = relay.prepare_client_body(body, reservation, relay.GATEWAY_BODY_BYTES)
        self.assertIs(sent, body)
        self.assertEqual(changed, 0)
        self.assertIsNone(stats)
        reservation.release()
        self.assertEqual(relay._body_buffers.used, 0)

    def test_body_above_fingerprint_parse_bound_is_byte_exact(self):
        # The bound guards the fingerprint scan only. Use a fingerprint body
        # without images: an image-bearing body is parsed anyway on purpose
        # (its image has to be budgeted), which would not prove the bound.
        body = relay._dump({'model': 'deepseek-v4.1-flash', 'messages': [
            {'role': 'system', 'content': 'You are an AI agent powered by DeepSeek Harness.'}]})
        reservation = relay._body_buffers.reserve(len(body))
        with mock.patch.object(relay, '_FINGERPRINT_MAX_BYTES', len(body) - 1):
            sent, changed, stats = relay.prepare_client_body(body, reservation, relay.GATEWAY_BODY_BYTES)
        self.assertIs(sent, body)
        self.assertEqual(changed, 0)
        self.assertIsNone(stats)
        reservation.release()
        self.assertEqual(relay._body_buffers.used, 0)

    def test_prepare_client_body_reports_image_stats(self):
        body = relay._dump({'model': 'm', 'messages': [
            {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {'url': TINY_PNG}}]}]})
        reservation = relay._body_buffers.reserve(len(body))
        sent, changed, stats = relay.prepare_client_body(body, reservation, relay.GATEWAY_BODY_BYTES)
        self.assertIs(sent, body)
        self.assertEqual(changed, 0)
        self.assertEqual(stats, {'images': 1, 'removed': 0, 'compressed': 0})
        reservation.release()
        self.assertEqual(relay._body_buffers.used, 0)

    def test_refusal_detection_requires_400_and_top_level_code(self):
        self.assertTrue(relay.is_parameter_refusal(400, b'{"code":11133}'))
        self.assertFalse(relay.is_parameter_refusal(400, b'{"requestId":"11133"}'))
        self.assertFalse(relay.is_parameter_refusal(413, b'{"code":11133}'))

    def test_tool_repair_only_changes_invalid_contiguous_group(self):
        body, image = tool_repair_body()
        doc = json.loads(body)
        self.assertTrue(relay.repair_tool_messages(doc))
        self.assertEqual(len(doc['messages']), 2)
        self.assertEqual(doc['messages'][0], {'role': 'assistant'})
        self.assertEqual(doc['messages'][1]['content'][0]['image_url']['url'], image)
        valid = {'messages': [
            {'role': 'assistant', 'tool_calls': [{'id': 'same'}]},
            {'role': 'tool', 'tool_call_id': 'same', 'content': 'result'}]}
        before = relay._dump(valid)
        self.assertFalse(relay.repair_tool_messages(valid))
        self.assertEqual(relay._dump(valid), before)

    def test_11133_retries_once_only_after_minimal_tool_pair_repair(self):
        body, image = tool_repair_body()
        original = response(400, b'{"code":11133}')
        repaired = response(200)
        reservation = relay._body_buffers.reserve(len(body))
        with (mock.patch.dict(relay.CONF, {'body_buffer_bytes': len(body) * 4}),
              mock.patch.object(relay, 'open_upstream', side_effect=[original, repaired]) as opening):
            result = prepared_handler()._prepare_response(
                body, reservation, 'https://workspace', len(body) * 2, {'Content-Type': 'application/json'})
        self.assertIs(result, repaired)
        self.assertEqual(opening.call_count, 2)
        sent = opening.call_args_list[1].args[1]
        self.assertNotEqual(sent, body)
        fixed = json.loads(sent)
        self.assertEqual(len(fixed['messages']), 2)
        self.assertEqual(fixed['messages'][0], {'role': 'assistant'})
        self.assertEqual(fixed['messages'][1]['content'][0]['image_url']['url'], image)
        self.assertLessEqual(len(sent), len(body) * 2)
        original.close.assert_called_once()
        reservation.release()
        self.assertEqual(relay._body_buffers.used, 0)

    def test_generic_11133_keeps_original_body_without_repair_retry(self):
        body = relay._dump({'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': TINY_PNG}}]}]})
        original = response(400, b'{"code":11133}')
        reservation = relay._body_buffers.reserve(len(body))
        with mock.patch.object(relay, 'open_upstream', return_value=original) as opening:
            result = prepared_handler()._prepare_response(
                body, reservation, 'https://workspace', len(body) * 4, {'Content-Type': 'application/json'})
        self.assertEqual(result, (400, b'{"code":11133}'))
        self.assertEqual(opening.call_count, 1)
        self.assertEqual(opening.call_args.args[1], body)
        original.close.assert_called_once()
        reservation.release()

    def test_repair_gate_is_nonblocking_and_preserves_original_error(self):
        body, _ = tool_repair_body()
        original = response(400, b'{"code":11133}')
        reservation = relay._body_buffers.reserve(len(body))
        self.assertTrue(relay._repair_gate.acquire(blocking=False))
        try:
            with mock.patch.object(relay, 'open_upstream', return_value=original) as opening:
                result = prepared_handler()._prepare_response(
                    body, reservation, 'https://workspace', len(body) * 4, {'Content-Type': 'application/json'})
        finally:
            relay._repair_gate.release()
            reservation.release()
        self.assertEqual(result, (400, b'{"code":11133}'))
        self.assertEqual(opening.call_count, 1)
        self.assertEqual(opening.call_args.args[1], body)
        self.assertEqual(relay._body_buffers.used, 0)

    def test_repair_budget_failure_keeps_original_error_and_releases(self):
        # No image and no fingerprint in this body, so prepare_client_body takes
        # the byte-exact early return and does not grow the reservation. The
        # only admission failure left is the repair attempt's 4x reservation.
        body = relay._dump({'model': 'm', 'messages': [
            {'role': 'assistant', 'tool_calls': [{'id': 'call-1'}]},
            {'role': 'tool', 'tool_call_id': 'orphan', 'content': 'discarded'}]})
        buffers = relay._BodyBuffers()
        original = response(400, b'{"code":11133}')
        # Budget = 2x body: the upload itself fits, for_repair()'s 4x cannot.
        with (mock.patch.object(relay, '_body_buffers', buffers),
              mock.patch.dict(relay.CONF, {'body_buffer_bytes': len(body) * 2}),
              mock.patch.object(relay, 'open_upstream', return_value=original) as opening):
            reservation = buffers.reserve(len(body))
            result = prepared_handler()._prepare_response(
                body, reservation, 'https://workspace', len(body) * 2, {'Content-Type': 'application/json'})
            self.assertEqual(buffers.used, len(body))
            reservation.release()
            self.assertEqual(buffers.used, 0)
        self.assertEqual(result, (400, b'{"code":11133}'))
        self.assertEqual(opening.call_count, 1)
        self.assertEqual(opening.call_args.args[1], body)
        # The repair gate must be free again after the failed repair attempt.
        self.assertTrue(relay._repair_gate.acquire(blocking=False))
        relay._repair_gate.release()

    def test_413_remembers_smaller_upstream_cap_and_never_retries(self):
        buffers = relay._BodyBuffers()
        body = b'{"request":"synthetic"}'
        # Above the remembered-limit floor so the value is kept verbatim.
        cap = 8 * MIB
        original = response(413, b'{"error":{"max_body_bytes":%d}}' % cap)
        reservation = buffers.reserve(len(body))
        before = relay.time.monotonic()
        with mock.patch.object(relay, '_body_buffers', buffers), \
             mock.patch.object(relay, 'open_upstream', return_value=original) as opening:
            result = prepared_handler()._prepare_response(
                body, reservation, 'https://workspace', 4096, {'Content-Type': 'application/json'})
        self.assertEqual(result, (413, b'{"error":{"max_body_bytes":%d}}' % cap))
        self.assertEqual(opening.call_count, 1)
        self.assertEqual(relay._capability['base'], 'https://workspace')
        self.assertEqual(relay._capability['limit'], cap)
        # The remembered cap is temporary: body_limit() re-probes once it expires.
        self.assertGreater(relay._capability['retry_at'], before)
        self.assertLessEqual(relay._capability['retry_at'],
                             relay.time.monotonic() + relay._REMEMBERED_LIMIT_TTL)
        original.close.assert_called_once()
        reservation.release()
        self.assertEqual(buffers.used, 0)

    def test_remembered_413_cap_has_a_floor_and_expires(self):
        clock = [1000.0]
        with mock.patch.object(relay.time, 'monotonic', side_effect=lambda: clock[0]):
            # An absurd cap from a rewritten payload must not pin every later
            # request to 413, so remember_limit() clamps it to the floor.
            relay.remember_limit('https://workspace', b'{"error":{"max_body_bytes":1234}}')
            self.assertEqual(relay._capability['limit'], relay._REMEMBERED_LIMIT_FLOOR)
            self.assertEqual(relay._capability['base'], 'https://workspace')

            clock[0] += relay._REMEMBERED_LIMIT_TTL + 1
            with mock.patch.object(relay, '_probe', return_value=(True, 32 * MIB)) as probe:
                self.assertEqual(relay.body_limit('https://workspace'), 32 * MIB)
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(relay._capability['retry_at'], 0.0)

            # A larger remembered cap is kept as-is (no clamping downwards).
            relay.remember_limit('https://workspace', b'{"error":{"max_body_bytes":8388608}}')
            self.assertEqual(relay._capability['limit'], 8 * MIB)

    def test_failed_health_probe_uses_short_backoff_then_recovers(self):
        with (mock.patch.object(relay, '_probe', side_effect=[(False, None), (True, 32 * MIB)]) as probe,
              mock.patch.object(relay.time, 'monotonic', side_effect=[10, 10, 11, 13, 13])):
            self.assertEqual(relay.body_limit('https://one'), relay.CONF['body_buffer_bytes'])
            self.assertEqual(relay.body_limit('https://one'), relay.CONF['body_buffer_bytes'])
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(relay.body_limit('https://one'), 32 * MIB)
            self.assertEqual(probe.call_count, 2)

    def test_health_cap_is_cached_per_workspace_and_413_updates_it(self):
        with mock.patch.object(relay, '_probe', return_value=(True, 32 * MIB)) as probe:
            self.assertEqual(relay.body_limit('https://one'), 32 * MIB)
            self.assertEqual(relay.body_limit('https://one'), 32 * MIB)
            self.assertEqual(probe.call_count, 1)
            relay.body_limit('https://two')
            self.assertEqual(probe.call_count, 2)
        relay.remember_limit('https://two', b'{"error":{"max_body_bytes":8388608}}')
        self.assertEqual(relay._capability['limit'], 8 * MIB)
        self.assertEqual(relay._capability['base'], 'https://two')


class BodyBufferTests(RelayStateMixin, unittest.TestCase):
    def handler(self, body=b'{}', length=None, transfer_encoding=None):
        handler = relay.Handler.__new__(relay.Handler)
        handler.path = '/v1/chat/completions'
        values = {'Content-Length': str(len(body) if length is None else length)}
        if transfer_encoding:
            values['Transfer-Encoding'] = transfer_encoding
        handler.headers = values
        handler.rfile = mock.Mock()
        handler.rfile.read.return_value = body
        handler._send = mock.Mock()
        return handler

    def test_declared_length_over_minimum_raw_cap_is_413_before_read(self):
        for raw_budget, upstream_cap, length, expected in ((64, 8, 9, 8), (8, 64, 9, 8)):
            buffers = relay._BodyBuffers()
            with self.subTest(raw_budget=raw_budget), \
                 mock.patch.object(relay, '_body_buffers', buffers), \
                 mock.patch.dict(relay.CONF, {'body_buffer_bytes': raw_budget}), \
                 mock.patch.object(relay, 'upstream_base', return_value='https://workspace'), \
                 mock.patch.object(relay, 'body_limit', return_value=upstream_cap):
                handler = self.handler(body=b'x' * length)
                handler.do_POST()
                self.assertEqual(handler.rfile.read.call_count, 0)
                self.assertEqual(handler._send.call_args.args[0], 413)
                payload = json.loads(handler._send.call_args.args[1])
                self.assertEqual(payload['error']['max_body_bytes'], expected)
                self.assertEqual(buffers.used, 0)

    def test_temporary_budget_busy_is_503_before_read(self):
        buffers = relay._BodyBuffers()
        with (mock.patch.object(relay, '_body_buffers', buffers),
              mock.patch.dict(relay.CONF, {'body_buffer_bytes': 8}),
              mock.patch.object(relay, 'upstream_base', return_value='https://workspace'),
              mock.patch.object(relay, 'body_limit', return_value=64)):
            held = buffers.reserve(8)
            handler = self.handler(b'{}')
            handler.do_POST()
            self.assertEqual(handler.rfile.read.call_count, 0)
            self.assertEqual(handler._send.call_args.args[0], 503)
            self.assertEqual(json.loads(handler._send.call_args.args[1])['error']['type'], 'relay_busy')
            self.assertEqual(handler._send.call_args.args[3]['Retry-After'], '2')
            self.assertEqual(buffers.used, 8)
            held.release()
            self.assertEqual(buffers.used, 0)

    def test_incomplete_read_timeout_and_upstream_failure_release_reservation(self):
        buffers = relay._BodyBuffers()
        with (mock.patch.object(relay, '_body_buffers', buffers),
              mock.patch.dict(relay.CONF, {'body_buffer_bytes': 32}),
              mock.patch.object(relay, 'upstream_base', return_value='https://workspace'),
              mock.patch.object(relay, 'body_limit', return_value=32)):
            handler = self.handler(body=b'ab', length=3)
            handler.do_POST()
            self.assertEqual(handler._send.call_args.args[0], 400)
            self.assertEqual(buffers.used, 0)
            handler = self.handler()
            handler.rfile.read.side_effect = socket.timeout()
            handler.do_POST()
            self.assertEqual(handler._send.call_args.args[0], 408)
            self.assertEqual(buffers.used, 0)
            with mock.patch.object(relay, 'open_upstream', side_effect=OSError('synthetic failure')):
                with self.assertRaises(OSError):
                    self.handler().do_POST()
            self.assertEqual(buffers.used, 0)

    def test_reservation_is_released_before_response_streaming(self):
        buffers = relay._BodyBuffers()
        fake_response = response(200)
        with (mock.patch.object(relay, '_body_buffers', buffers),
              mock.patch.dict(relay.CONF, {'body_buffer_bytes': 32}),
              mock.patch.object(relay, 'upstream_base', return_value='https://workspace'),
              mock.patch.object(relay, 'body_limit', return_value=32),
              mock.patch.object(relay.Handler, '_prepare_response', return_value=fake_response),
              mock.patch.object(relay.Handler, '_handle_response') as handling):
            handler = self.handler(b'{}')
            handler.do_POST()
            self.assertEqual(handler.rfile.read.call_count, 1)
            self.assertEqual(buffers.used, 0)
            handling.assert_called_once_with(fake_response, 'https://workspace')

    def test_repair_exception_releases_reservation_and_gate(self):
        buffers = relay._BodyBuffers()
        body, _ = tool_repair_body()
        first = response(400, b'{"code":11133}')
        with (mock.patch.object(relay, '_body_buffers', buffers),
              mock.patch.dict(relay.CONF, {'body_buffer_bytes': len(body) * 4}),
              mock.patch.object(relay, 'upstream_base', return_value='https://workspace'),
              mock.patch.object(relay, 'body_limit', return_value=len(body) * 4),
              mock.patch.object(relay, 'open_upstream', return_value=first),
              mock.patch.object(relay, 'repaired_body', side_effect=RuntimeError('synthetic failure'))):
            with self.assertRaises(RuntimeError):
                self.handler(body).do_POST()
        self.assertEqual(buffers.used, 0)
        self.assertTrue(relay._repair_gate.acquire(blocking=False))
        relay._repair_gate.release()
        first.close.assert_called_once()


class RelayHTTPTests(RelayStateMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = relay.Server(('127.0.0.1', 0), relay.Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = '127.0.0.1'
        cls.port = cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        # Server.daemon_threads is False on purpose (drain-on-SIGTERM); a test
        # that forgets shutdown() would hang the process, so always do both.
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def open_post(self, body):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request('POST', '/v1/chat/completions', body=body,
                     headers={'Content-Type': 'application/json'})
        return conn, conn.getresponse()

    def post(self, body, headers=None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.request('POST', '/v1/chat/completions', body=body,
                     headers={'Content-Type': 'application/json', **(headers or {})})
        resp = conn.getresponse()
        status, response_headers, payload = resp.status, dict(resp.getheaders()), resp.read()
        conn.close()
        return status, response_headers, payload

    def chunked_post(self, chunks):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        conn.putrequest('POST', '/v1/chat/completions')
        conn.putheader('Content-Type', 'application/octet-stream')
        conn.putheader('Transfer-Encoding', 'chunked')
        conn.endheaders()
        for chunk in chunks:
            conn.send(('%x\r\n' % len(chunk)).encode('ascii') + chunk + b'\r\n')
        conn.send(b'0\r\n\r\n')
        resp = conn.getresponse()
        status, response_headers, payload = resp.status, dict(resp.getheaders()), resp.read()
        conn.close()
        return status, response_headers, payload

    def test_eight_long_local_streams_release_body_budget_and_request_bytes(self):
        finish = threading.Event()
        received = []
        connections = []
        captured_lock = threading.Lock()

        class StreamingUpstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                with captured_lock:
                    received.append(body)
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Transfer-Encoding', 'chunked')
                self.send_header('Connection', 'close')
                self.end_headers()
                try:
                    self.wfile.write(b'd\r\ndata: ready\n\n\r\n')
                    self.wfile.flush()
                    finish.wait(10)
                    self.wfile.write(b'0\r\n\r\n')
                    self.wfile.flush()
                except OSError:
                    pass
                self.close_connection = True

        class UpstreamServer(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 16

        upstream = UpstreamServer(('127.0.0.1', 0), StreamingUpstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % upstream.server_port
        pool = relay._UpstreamPool()
        responses = []

        def new_connection(scheme, host, port):
            # The real HTTPConnection type, so this also proves the relay does
            # not stash request bytes on the connection object.
            conn = http.client.HTTPConnection(host, port, timeout=5)
            with captured_lock:
                connections.append(conn)
            return conn

        try:
            with (mock.patch.object(relay, 'upstream_base', return_value=base),
                  mock.patch.object(relay, 'body_limit', return_value=1024),
                  mock.patch.object(relay, '_pool', pool),
                  mock.patch.object(relay, '_new_connection', side_effect=new_connection),
                  mock.patch.dict(relay.CONF, {'body_buffer_bytes': 16}),
                  concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor):
                responses = list(executor.map(lambda _: self.open_post(b'{}'), range(8)))
                self.assertEqual([resp.status for _, resp in responses], [200] * 8)
                self.assertFalse(finish.is_set())
                # Upload quota is released before the first byte is streamed,
                # so 8 concurrent long replies hold no body budget at all.
                self.assertEqual(relay._body_buffers.used, 0)
                self.assertEqual(len(received), 8)
                self.assertEqual(received, [b'{}'] * 8)
                for connection in connections:
                    self.assertFalse(any(isinstance(value, bytes) and value == b'{}'
                                         for value in vars(connection).values()))
                finish.set()
                for _, resp in responses:
                    self.assertEqual(resp.read(), b'data: ready\n\n')
        finally:
            finish.set()
            for conn, resp in responses:
                resp.close()
                conn.close()
            upstream.shutdown()
            upstream.server_close()
            thread.join()

    def test_http_chunked_body_uses_budget_and_preserves_bytes(self):
        received = []
        lock = threading.Lock()

        class CapturingUpstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                content = self.rfile.read(int(self.headers['Content-Length']))
                with lock:
                    received.append(content)
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), CapturingUpstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % upstream.server_port
        raw = b'chunk-one\x00chunk-two'
        try:
            with (mock.patch.object(relay, 'upstream_base', return_value=base),
                  mock.patch.object(relay, 'body_limit', return_value=128),
                  mock.patch.dict(relay.CONF, {'body_buffer_bytes': 128})):
                status, _, payload = self.chunked_post([b'chunk-one', b'\x00chunk-two'])
            self.assertEqual((status, payload), (200, b'ok'))
            self.assertEqual(received, [raw])
            self.assertEqual(relay._body_buffers.used, 0)
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join()

    def test_http_chunked_body_over_budget_is_413_without_upstream_replay(self):
        received = []

        class CapturingUpstream(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_POST(self):
                received.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.send_header('Content-Length', '2')
                self.end_headers()
                self.wfile.write(b'ok')

        upstream = ThreadingHTTPServer(('127.0.0.1', 0), CapturingUpstream)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:%d' % upstream.server_port
        try:
            with (mock.patch.object(relay, 'upstream_base', return_value=base),
                  mock.patch.object(relay, 'body_limit', return_value=8),
                  mock.patch.dict(relay.CONF, {'body_buffer_bytes': 8})):
                status, _, payload = self.chunked_post([b'123456', b'789'])
            self.assertEqual(status, 413)
            self.assertEqual(json.loads(payload)['error']['max_body_bytes'], 4)
            self.assertEqual(received, [])
            self.assertEqual(relay._body_buffers.used, 0)
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join()

    def test_wrong_key_remains_401_while_recycle_401_and_redirect_become_503(self):
        wrong_key = response(401, b'{"error":{"message":"invalid api key","type":"auth_error"}}')
        with (mock.patch.object(relay, 'upstream_base', return_value='https://workspace'),
              mock.patch.object(relay, 'body_limit', return_value=1024),
              mock.patch.object(relay, 'open_upstream', return_value=wrong_key) as opening,
              mock.patch.object(relay, 'upstream_alive', return_value=True)):
            status, _, _ = self.post(b'{}')
            self.assertEqual(status, 401)
            self.assertEqual(opening.call_count, 1)

        for status_code, alive in ((401, False), (302, False)):
            with self.subTest(status=status_code), \
                 mock.patch.object(relay, 'upstream_base', return_value='https://workspace'), \
                 mock.patch.object(relay, 'body_limit', return_value=1024), \
                 mock.patch.object(relay, 'open_upstream', return_value=response(
                     status_code, b'{"error":{"message":"invalid api key","type":"auth_error"}}')) as opening, \
                 mock.patch.object(relay, 'upstream_alive', return_value=alive):
                result, headers, _ = self.post(b'{}')
                self.assertEqual(result, 503)
                self.assertEqual(headers['Retry-After'], '10')
                self.assertEqual(opening.call_count, 1)

    def test_upstream_5xx_and_transport_failure_are_never_replayed(self):
        upstream = response(502, b'{"error":{"message":"upstream failed"}}',
                            {'Content-Type': 'application/json'})
        with (mock.patch.object(relay, 'upstream_base', return_value='https://workspace'),
              mock.patch.object(relay, 'body_limit', return_value=1024),
              mock.patch.object(relay, 'open_upstream', return_value=upstream) as opening):
            status, _, payload = self.post(b'{}')
            self.assertEqual(status, 502)
            self.assertIn(b'upstream failed', payload)
            self.assertEqual(opening.call_count, 1)

        with (mock.patch.object(relay, 'upstream_base', return_value='https://workspace'),
              mock.patch.object(relay, 'body_limit', return_value=1024),
              mock.patch.object(relay, 'open_upstream', return_value=None) as opening):
            status, _, payload = self.post(b'{}')
            self.assertEqual(status, 503)
            self.assertEqual(json.loads(payload)['error']['type'], 'upstream_unavailable')
            self.assertEqual(opening.call_count, 1)


# ---------------------------------------------------------------------------
# image_budget: new coverage for the 10 MiB gateway wire budget
# ---------------------------------------------------------------------------
def _big_jpeg_url():
    """Deterministic random-noise JPEG: compressible, unlike a flat colour."""
    rnd = random.Random(7)
    pixels = bytes(rnd.getrandbits(8) for _ in range(400 * 400 * 3))
    buffer = io.BytesIO()
    Image.frombytes('RGB', (400, 400), pixels).save(buffer, format='JPEG', quality=92)
    raw = buffer.getvalue()
    return 'data:image/jpeg;base64,' + base64.b64encode(raw).decode('ascii'), raw


def _parts(doc):
    """All inline content parts of every message, in order."""
    parts = []
    for message in doc['messages']:
        content = message.get('content')
        if isinstance(content, list):
            parts.extend(part for part in content if isinstance(part, dict))
    return parts


def _doc_with_images(*urls):
    return {'model': 'm', 'messages': [{'role': 'user', 'content': [
        {'type': 'image_url', 'image_url': {'url': url}} for url in urls]}]}


class ImageBudgetTests(RelayStateMixin, unittest.TestCase):
    def reduce(self, doc, limit=image_budget.GATEWAY_BODY_BYTES):
        body = image_budget.dump(doc)
        reservation = relay._body_buffers.reserve(1)
        try:
            sent, stats = image_budget.reduce_images(doc, body, reservation, limit)
        finally:
            reservation.release()
        return body, sent, stats

    def test_body_under_ceiling_with_healthy_image_is_unchanged(self):
        doc = _doc_with_images(TINY_PNG)
        body, sent, stats = self.reduce(doc)
        self.assertIs(sent, body)
        self.assertEqual(stats, {'images': 1, 'removed': 0, 'compressed': 0})

    def test_body_over_ceiling_compresses_image_before_omitting(self):
        url, raw = _big_jpeg_url()
        doc = {'model': 'm', 'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': url, 'detail': 'high'}}]}]}
        body, sent, stats = self.reduce(doc, 200 * 1024)
        self.assertFalse(sent is body)
        self.assertEqual(stats['images'], 1)
        self.assertEqual(stats['removed'], 0)
        self.assertEqual(stats['compressed'], 1)
        self.assertLess(len(sent), len(body))
        part = _parts(json.loads(sent))[0]
        self.assertEqual(part['type'], 'image_url')
        encoded = part['image_url']['url']
        self.assertTrue(encoded.startswith('data:image/jpeg;base64,'))
        self.assertLess(len(base64.b64decode(encoded.split(',', 1)[1])), len(raw))
        self.assertEqual(part['image_url']['detail'], 'high')

    def test_body_over_ceiling_omits_oldest_images_first(self):
        oldest, _ = _big_jpeg_url()
        newest = TINY_PNG
        doc = _doc_with_images(oldest, oldest, newest)
        # A ~1 KiB ceiling cannot hold the two big occurrences, but it can hold
        # the body once they are gone: the pass omits oldest-first and keeps the
        # newest image.
        _, sent, stats = self.reduce(doc, image_budget.WIRE_MARGIN_BYTES + 1000)
        self.assertEqual(stats['images'], 3)
        self.assertEqual(stats['removed'], 2)
        parts = _parts(json.loads(sent))
        self.assertEqual([part['type'] for part in parts], ['text', 'text', 'image_url'])
        self.assertIn('relay image #1', parts[0]['text'])
        self.assertIn('relay image #2', parts[1]['text'])
        self.assertEqual(parts[2]['image_url']['url'], newest)

    def test_request_image_limit_when_omitting_images_is_still_not_enough(self):
        oldest, _ = _big_jpeg_url()
        doc = _doc_with_images(oldest, TINY_PNG)
        with self.assertRaises(image_budget.RequestImageLimit) as caught:
            self.reduce(doc, image_budget.WIRE_MARGIN_BYTES + 100)
        self.assertEqual(caught.exception.limit, image_budget.WIRE_MARGIN_BYTES + 100)
        # The document was reduced in place, oldest-first, before giving up.
        parts = _parts(doc)
        self.assertEqual([part['type'] for part in parts], ['text', 'text'])
        self.assertIn('relay image #1', parts[0]['text'])
        self.assertIn('relay image #2', parts[1]['text'])

    def test_oldest_image_is_omitted_when_text_almost_fills_the_ceiling(self):
        # Body skeleton plus the tiny second image (and a little slack) is the
        # whole budget: the oldest, large image has to go, and it is the only
        # one that goes.
        oldest, _ = _big_jpeg_url()
        doc = _doc_with_images(oldest, TINY_PNG)
        body = image_budget.dump(doc)
        skeleton = len(body) - len(oldest) - len(TINY_PNG)
        limit = image_budget.WIRE_MARGIN_BYTES + skeleton + len(TINY_PNG) + 200
        _, sent, stats = self.reduce(doc, limit)
        self.assertFalse(sent is body)
        self.assertEqual(stats['removed'], 1)
        parts = _parts(json.loads(sent))
        self.assertEqual([part['type'] for part in parts], ['text', 'image_url'])
        self.assertIn('relay image #1', parts[0]['text'])
        self.assertEqual(parts[1]['image_url']['url'], TINY_PNG)
        self.assertLess(len(sent), limit)

    def test_non_image_fields_alone_over_ceiling_raise_request_image_limit(self):
        doc = {'model': 'm', 'messages': [{'role': 'user', 'content': 'x' * 20000}]}
        body = image_budget.dump(doc)
        reservation = relay._body_buffers.reserve(1)
        try:
            with self.assertRaises(image_budget.RequestImageLimit) as caught:
                image_budget.reduce_images(doc, body, reservation, 8192)
        finally:
            reservation.release()
        self.assertEqual(caught.exception.limit, 8192)
        self.assertIn('non-removable content', str(caught.exception))

    def test_max_images_excess_is_removed_oldest_first(self):
        doc = _doc_with_images(*([TINY_PNG] * (image_budget.MAX_IMAGES + 5)))
        body, sent, stats = self.reduce(doc)
        self.assertEqual(stats['images'], image_budget.MAX_IMAGES + 5)
        self.assertEqual(stats['removed'], 5)
        self.assertEqual(stats['compressed'], 0)
        parts = _parts(json.loads(sent))
        self.assertEqual(sum(1 for part in parts if part['type'] == 'image_url'),
                         image_budget.MAX_IMAGES)
        self.assertEqual(sum(1 for part in parts if part['type'] == 'text'), 5)
        for ordinal, part in enumerate(parts[:5], start=1):
            self.assertIn('relay image #%d' % ordinal, part['text'])

    def test_omitting_a_tiny_image_never_grows_the_body(self):
        # Regression: the placeholder used to be ~148 bytes, so removing ten 4x4
        # PNGs (~95 bytes each) made the request *larger* than the input while
        # reporting them as "omitted to fit the budget".
        doc = _doc_with_images(*([TINY_PNG] * (image_budget.MAX_IMAGES + 10)))
        body, sent, stats = self.reduce(doc)
        self.assertGreater(stats['removed'], 0)
        self.assertLessEqual(len(sent), len(body), 'omission must never grow the body')

    def test_undecodable_image_is_named_as_unsupported_not_as_a_budget_cut(self):
        # A placeholder is only worth it when it shrinks the body; when it does
        # not, the image is kept and the upstream decides. When it does fit, the
        # reason must say the format was undecodable rather than blaming the budget.
        broken = 'data:image/jpeg;base64,' + base64.b64encode(b'X' * 400000).decode('ascii')
        doc = _doc_with_images(broken)
        body, sent, stats = self.reduce(doc)
        self.assertEqual(stats['removed'], 1)
        self.assertLess(len(sent), len(body))
        placeholder = _parts(json.loads(sent))[0]['text']
        self.assertIn('undecodable', placeholder)
        self.assertNotIn('image budget', placeholder)

    def test_remote_image_that_cannot_be_resized_names_the_real_obstacle(self):
        # Remote URL images have no inline bytes: the relay cannot resize or drop
        # them, so "reduce/compress images" would be misleading advice.
        doc = {'model': 'm', 'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': 'https://example.com/a.png'}},
            {'type': 'text', 'text': 'x' * 12000}]}]}
        body = image_budget.dump(doc)
        reservation = relay._body_buffers.reserve(1)
        try:
            with self.assertRaises(image_budget.RequestImageLimit) as caught:
                image_budget.reduce_images(doc, body, reservation, 8192)
        finally:
            reservation.release()
        self.assertIn('remote URL images', str(caught.exception))

    def test_wire_size_adds_node_number_padding(self):
        plain = {'n': 1, 'ok': True, 's': 'x'}
        serialized = image_budget.dump(plain)
        self.assertEqual(image_budget._number_padding(plain), 0)
        self.assertEqual(image_budget.wire_size(plain, serialized), len(serialized))

        # Integers above 2**53 and floats can be spelled longer by
        # JSON.stringify on the Node hop (up to 25 characters).
        doc = {'n': 2 ** 53}
        serialized = image_budget.dump(doc)
        self.assertEqual(image_budget._number_padding(doc), 25 - len(str(2 ** 53)))
        self.assertEqual(image_budget.wire_size(doc, serialized),
                         len(serialized) + 25 - len(str(2 ** 53)))

        self.assertEqual(image_budget._number_padding([1.5]), 25 - len(repr(1.5)))
        # Booleans are ints in Python; they must not be counted as numbers.
        self.assertEqual(image_budget._number_padding({'t': True, 'f': False}), 0)

    def test_dump_is_shared_with_the_relay(self):
        # The relay imports dump from image_budget, so both must serialize
        # identically (this is what keeps the measured size authoritative).
        self.assertIs(relay._dump, image_budget.dump)


# ---------------------------------------------------------------------------
# _UpstreamPool / open_upstream: pooled connection reuse
# ---------------------------------------------------------------------------
class _FakeConnection:
    def __init__(self, response=None, failure=None):
        self.response = response
        self.failure = failure
        self.requests = []
        self.closed = 0

    def request(self, method, target, body=None, headers=None):
        self.requests.append((method, target, body, headers))
        if self.failure is not None:
            raise self.failure

    def getresponse(self):
        return self.response

    def close(self):
        self.closed += 1


class _FakeRawResponse:
    def __init__(self, status=200, payload=b'ok', will_close=False):
        self.status = status
        self.headers = {}
        self.length = len(payload)
        self.will_close = will_close
        self.data = payload
        self._closed = False

    def isclosed(self):
        return self._closed

    def read(self, amt=None):
        if amt is None:
            payload, self.data = self.data, b''
            self._closed = True
            return payload
        payload, self.data = self.data[:amt], self.data[amt:]
        if not self.data:
            self._closed = True
        return payload

    def read1(self, amt):
        return self.read(amt)


class UpstreamPoolTests(unittest.TestCase):
    KEY = ('https', 'sub-9001.cnb.run', 443)

    def test_take_returns_first_idle_entry_for_the_key(self):
        pool = relay._UpstreamPool()
        first, second = _FakeConnection(), _FakeConnection()
        pool.give(self.KEY, first)
        pool.give(self.KEY, second)
        # The docstring says "freshest", but the implementation hands back the
        # first (oldest) idle entry so the stalest socket is consumed first.
        self.assertIs(pool.take(self.KEY), first)
        self.assertEqual(pool.idle_count(), 1)
        self.assertIs(pool.take(self.KEY), second)
        self.assertIsNone(pool.take(self.KEY))

    def test_take_closes_expired_and_mismatched_entries(self):
        # A fake clock keeps this deterministic (time.monotonic() on Windows
        # only ticks every ~15 ms, so a real sleep would be flaky).
        clock = [1000.0]
        with mock.patch.object(relay.time, 'monotonic', side_effect=lambda: clock[0]):
            pool = relay._UpstreamPool(ttl=30.0)
            expired = _FakeConnection()
            pool.give(self.KEY, expired)
            clock[0] += 31.0
            self.assertIsNone(pool.take(self.KEY))
            self.assertEqual(expired.closed, 1)
            self.assertEqual(pool.idle_count(), 0)

            other = _FakeConnection()
            pool.give(('https', 'other.cnb.run', 443), other)
            self.assertIsNone(pool.take(self.KEY))
            self.assertEqual(other.closed, 1)
            self.assertEqual(pool.idle_count(), 0)

    def test_give_caps_idle_entries_at_pool_max_idle(self):
        pool = relay._UpstreamPool()
        connections = [_FakeConnection() for _ in range(relay._POOL_MAX_IDLE + 1)]
        for connection in connections:
            pool.give(self.KEY, connection)
        self.assertEqual(relay._POOL_MAX_IDLE, 4)
        self.assertEqual(pool.idle_count(), relay._POOL_MAX_IDLE)
        self.assertEqual(connections[-1].closed, 1)
        self.assertEqual([c.closed for c in connections[:-1]],
                         [0] * relay._POOL_MAX_IDLE)

    def test_clear_empties_the_pool_and_closes_every_connection(self):
        pool = relay._UpstreamPool()
        first, second = _FakeConnection(), _FakeConnection()
        pool.give(self.KEY, first)
        pool.give(('https', 'other.cnb.run', 443), second)
        pool.clear()
        self.assertEqual(pool.idle_count(), 0)
        self.assertEqual([first.closed, second.closed], [1, 1])
        pool.clear()  # idempotent


class OpenUpstreamTests(unittest.TestCase):
    URL = 'https://sub-9001.cnb.run/v1/chat/completions?stream=1'
    KEY = ('https', 'sub-9001.cnb.run', 443)

    def test_stale_reused_socket_is_retried_exactly_once_on_a_fresh_connection(self):
        pool = relay._UpstreamPool()
        stale = _FakeConnection(failure=OSError('peer closed idle socket'))
        pool.give(self.KEY, stale)
        fresh = _FakeConnection(response=_FakeRawResponse(status=200, payload=b'data: ok\n\n'))
        with (mock.patch.object(relay, '_pool', pool),
              mock.patch.object(relay, '_new_connection', return_value=fresh) as new_connection):
            result = relay.open_upstream(self.URL, b'{}', {'Content-Type': 'application/json'})
            self.assertIsNotNone(result)
            self.assertEqual(result.status, 200)
            self.assertEqual(new_connection.call_count, 1)
            self.assertEqual(stale.requests[0][:2], ('POST', '/v1/chat/completions?stream=1'))
            self.assertEqual(len(fresh.requests), 1)
            self.assertEqual(stale.closed, 1)
            # Reading to EOF hands the fresh connection back to the pool; the
            # on_done callback looks up relay._pool at call time, so the read
            # has to happen while the patch is active.
            self.assertEqual(result.read(), b'data: ok\n\n')
            self.assertEqual(pool.idle_count(), 1)
            result.close()
            self.assertEqual(pool.idle_count(), 1)

    def test_fresh_connection_failure_returns_none_without_another_retry(self):
        pool = relay._UpstreamPool()
        broken = _FakeConnection(failure=OSError('connect failed'))
        with (mock.patch.object(relay, '_pool', pool),
              mock.patch.object(relay, '_new_connection', return_value=broken) as new_connection):
            result = relay.open_upstream(self.URL, b'{}', {})
        self.assertIsNone(result)
        self.assertEqual(new_connection.call_count, 1)
        self.assertEqual(len(broken.requests), 1)
        self.assertEqual(broken.closed, 1)
        self.assertEqual(pool.idle_count(), 0)

    def test_used_connection_is_pooled_but_unreusable_one_is_closed(self):
        reusable = _FakeRawResponse(status=200, payload=b'ok', will_close=False)
        unreusable = _FakeRawResponse(status=200, payload=b'ok', will_close=True)
        for raw, pooled in ((reusable, 1), (unreusable, 0)):
            with self.subTest(will_close=raw.will_close):
                pool = relay._UpstreamPool()
                conn = _FakeConnection(response=raw)
                with (mock.patch.object(relay, '_pool', pool),
                      mock.patch.object(relay, '_new_connection', return_value=conn)):
                    result = relay.open_upstream(self.URL, b'{}', {})
                    self.assertEqual(result.read(), b'ok')
                    self.assertEqual(pool.idle_count(), pooled)
                self.assertEqual(conn.closed, 0 if pooled else 1)

    def test_origin_builds_key_and_request_target(self):
        key, target = relay._origin('https://sub-9001.cnb.run/v1/chat/completions?stream=1')
        self.assertEqual(key, ('https', 'sub-9001.cnb.run', 443))
        self.assertEqual(target, '/v1/chat/completions?stream=1')
        self.assertEqual(relay._origin('http://127.0.0.1:9001')[:1],
                         (('http', '127.0.0.1', 9001),))


if __name__ == '__main__':
    unittest.main(verbosity=2)
