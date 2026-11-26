"""Offline deployment regressions. No env credentials or real nginx are used.

Ported from cnb2dsh/test/deploy/test_register.py; ``deploy/cnb-register.py`` is
byte-identical between the two repositories, so only the path moved.
"""
import importlib.util
import io
import json
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from http.server import HTTPServer

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'cnb-register.py'
SPEC = importlib.util.spec_from_file_location('register_under_test', SCRIPT)
reg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reg)
OLD = 'map $request_uri $cnb_ai_upstream { default old-9001.cnb.run:443; }\n'
NEW = 'map $request_uri $cnb_ai_upstream { default new-9001.cnb.run:443; }\n'


class RegisterSwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'upstream.conf'
        self.path.write_text(OLD)
        self.conf_patch = patch.object(reg, 'CONF', str(self.path))
        self.conf_patch.start()
        self.addCleanup(self.conf_patch.stop)
        reg.APPLIED = None

    def runner(self, *args, **kwargs):
        self.assertGreater(kwargs['timeout'], 0)
        return subprocess.CompletedProcess(args[0], 0, '', '')

    def test_unhealthy_candidate_never_replaces_working_map(self):
        with patch.object(reg, 'candidate_healthy', return_value=False), patch.object(reg.subprocess, 'run') as run:
            result = reg.switch('new')
        self.assertFalse(result['ok'])
        self.assertEqual(self.path.read_text(), OLD)
        run.assert_not_called()

    def test_existing_map_must_be_applied_once_then_duplicate_is_cheap(self):
        self.path.write_text(NEW)
        with patch.object(reg, 'candidate_healthy', return_value=True), patch.object(reg.subprocess, 'run', side_effect=self.runner) as run:
            self.assertTrue(reg.switch('new')['switched'])
            self.assertEqual(run.call_count, 2)
            self.assertFalse(reg.switch('new')['switched'])
            self.assertEqual(run.call_count, 2)

    def test_reload_exception_restores_disk_and_runtime_and_retry_reloads(self):
        calls = []
        runtime = [OLD]
        fail = [True]
        def run(args, **kwargs):
            calls.append(args)
            if args[0] == 'systemctl':
                # Model a signal that applied before the command was interrupted.
                runtime[0] = self.path.read_text()
                if fail[0]:
                    fail[0] = False
                    raise OSError('mock reload interrupted')
            return self.runner(args, **kwargs)
        with patch.object(reg, 'candidate_healthy', return_value=True), patch.object(reg.subprocess, 'run', side_effect=run):
            result = reg.switch('new')
            self.assertFalse(result['ok'])
            self.assertTrue(result['rolled_back'])
            self.assertEqual(self.path.read_text(), OLD)
            self.assertEqual(runtime[0], OLD)
            self.assertEqual(len(calls), 4)
            self.assertTrue(reg.switch('new')['ok'])
        self.assertEqual(runtime[0], NEW)
        self.assertEqual(len(calls), 6)
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [self.path])

    def test_nginx_validation_timeout_rolls_back_and_does_not_reload(self):
        def run(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs['timeout'])
        with patch.object(reg, 'candidate_healthy', return_value=True), patch.object(reg.subprocess, 'run', side_effect=run) as run_mock:
            result = reg.switch('new')
        self.assertFalse(result['ok'])
        self.assertTrue(result['rolled_back'])
        self.assertEqual(self.path.read_text(), OLD)
        self.assertEqual(run_mock.call_count, 1)
        self.assertIsNone(reg.APPLIED)

    def test_first_registration_failure_restores_absence(self):
        self.path.unlink()
        with patch.object(reg, 'candidate_healthy', return_value=True), patch.object(reg.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1, '', 'mock invalid config')):
            result = reg.switch('new')
        self.assertFalse(result['ok'])
        self.assertTrue(result['rolled_back'])
        self.assertFalse(self.path.exists())

    def test_reload_failure_reports_rollback_failure(self):
        def run(args, **kwargs):
            if args[0] == 'systemctl':
                return subprocess.CompletedProcess(args, 1, '', 'mock reload failure')
            return self.runner(args, **kwargs)
        with patch.object(reg, 'candidate_healthy', return_value=True), patch.object(reg.subprocess, 'run', side_effect=run):
            result = reg.switch('new')
        self.assertFalse(result['ok'])
        self.assertFalse(result['rolled_back'])
        self.assertIn('rollback_detail', result)
        self.assertIsNone(reg.APPLIED)
        self.assertEqual(self.path.read_text(), OLD)


class CandidateHealthTests(unittest.TestCase):
    def check(self, status, body):
        class Response(io.BytesIO):
            pass
        response = Response(body)
        response.status = status
        with patch.object(reg._health_opener, 'open', return_value=response) as opened:
            result = reg.candidate_healthy('synthetic')
        self.assertEqual(opened.call_args.args[0], 'https://synthetic-9001.cnb.run/health')
        return result

    def test_requires_http_200_and_json_status(self):
        self.assertTrue(self.check(200, b'{"status": "ok"}'))
        self.assertFalse(self.check(503, b'{"status":"ok"}'))
        self.assertFalse(self.check(200, b'HTML status ok'))
        self.assertFalse(self.check(200, b'[]'))
        self.assertFalse(self.check(200, b'{"status":"down"}'))


class AuthorizationTests(unittest.TestCase):
    """X-Reg-Token comparison must never raise out of the handler."""

    def authorized(self, token, header):
        handler = reg.H.__new__(reg.H)
        handler.headers = {'X-Reg-Token': header}
        with patch.object(reg, 'TOKEN', token):
            return handler.authorized()

    def test_matching_token_authorizes_and_wrong_token_does_not(self):
        self.assertTrue(self.authorized('synthetic-token', 'synthetic-token'))
        self.assertFalse(self.authorized('synthetic-token', 'synthetic'))
        self.assertFalse(self.authorized('synthetic-token', ''))

    def test_empty_configured_token_rejects_everything(self):
        self.assertFalse(self.authorized('', 'synthetic-token'))
        self.assertFalse(self.authorized('', ''))

    def test_non_ascii_header_is_rejected_instead_of_raising(self):
        # hmac.compare_digest raises TypeError on a non-ASCII str; comparing
        # bytes keeps a crafted header from escaping the handler as a 502.
        self.assertFalse(self.authorized('synthetic-token', '中文'))
        self.assertTrue(self.authorized('中文', '中文'))


class RequestTimeoutTests(unittest.TestCase):
    def test_incomplete_authenticated_body_releases_single_threaded_server(self):
        with patch.object(reg, 'TOKEN', 'synthetic-test-token'), patch.object(reg, 'READ_TIMEOUT', 0.1):
            server = HTTPServer(('127.0.0.1', 0), reg.H)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(b'POST /register HTTP/1.0\r\nX-Reg-Token: synthetic-test-token\r\nContent-Length: 100\r\n\r\n{')
                    answer = client.recv(4096)
                    self.assertIn(b' 408 ', answer)
                with socket.create_connection(server.server_address, timeout=2) as client:
                    client.sendall(b'GET / HTTP/1.0\r\nX-Reg-Token: synthetic-test-token\r\n\r\n')
                    self.assertIn(b' 200 ', client.recv(4096))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
