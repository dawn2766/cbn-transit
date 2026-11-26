"""Guard the relay's nginx deployment boundary; no nginx is installed or run.

The .cnb.yml assertions stay in cnb2dsh (they describe the workspace repo).
This file only keeps the nginx-vhost intent that now lives in cbn-transit.
"""
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
VHOST = REPO / 'deploy' / 'nginx' / 'sites-available' / 'ai.example.com'
COMPLETIONS = 'location = /v1/chat/completions {'
REGISTER = 'location = /ops/register {'
CATCH_ALL = 'location / {'
NAMED_UNAVAILABLE = 'location @upstream_unavailable {'


def strip_comments(config):
    """Drop `#` comments so only real directives are inspected."""
    return '\n'.join(line.split('#', 1)[0] for line in config.splitlines())


def balanced_block(config, header, occurrence=0):
    """Return the text inside the n-th `header { ... }` block, braces excluded."""
    start = -1
    for _ in range(occurrence + 1):
        start = config.index(header, start + 1)
    brace = config.index('{', start)
    depth = 0
    for index in range(brace, len(config)):
        if config[index] == '{':
            depth += 1
        elif config[index] == '}':
            depth -= 1
            if depth == 0:
                return config[brace + 1:index]
    raise AssertionError('unbalanced braces after %r' % header)


def error_page_source_codes(block):
    """Status codes nginx intercepts in a block (the `=NNN` target is excluded)."""
    codes = []
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped.startswith('error_page '):
            continue
        for token in stripped.split()[1:]:
            if token.startswith('='):
                break
            if token.isdigit():
                codes.append(token)
    return codes


class DeploymentConfigTests(unittest.TestCase):
    def setUp(self):
        self.raw = VHOST.read_text(encoding='utf-8')
        self.config = strip_comments(self.raw)
        # The vhost has two server blocks (port 80 redirect, then the real one).
        self.server = balanced_block(self.config, 'server {', occurrence=1)

    def test_relay_does_not_bypass_the_completion_relay(self):
        # A direct proxy_pass for completions would skip the image budget,
        # fingerprint neutralisation and the single tool-repair retry. The
        # comment block above the location may mention the anti-pattern, so
        # only real directives count here.
        self.assertNotIn('@chat_direct', self.config)
        self.assertNotIn('@chat_direct', self.server)

    def test_completions_location_disables_replay_and_buffering(self):
        block = balanced_block(self.server, COMPLETIONS)
        # One attempt only: a timed-out POST must never be re-sent upstream.
        self.assertIn('proxy_next_upstream off;', block)
        # SSE must reach the client as it is produced.
        self.assertIn('proxy_buffering off;', block)
        self.assertIn('proxy_cache off;', block)
        self.assertIn('proxy_http_version 1.1;', block)

    def test_uploads_are_admitted_by_the_relay_not_by_nginx(self):
        # Server-level: no independent upload cap and no full-body spooling, so
        # the relay's own JSON 413/503 answer is what the client sees.
        self.assertIn('client_max_body_size 0;', self.server)
        self.assertIn('proxy_request_buffering off;', self.server)
        # ...and those two directives genuinely sit outside every location
        # block, otherwise they would only cover one route.
        for header in (REGISTER, COMPLETIONS, NAMED_UNAVAILABLE, CATCH_ALL):
            block = balanced_block(self.server, header)
            self.assertNotIn('client_max_body_size 0;', block)
            self.assertNotIn('proxy_request_buffering off;', block)

    def test_catch_all_location_never_rewrites_a_busy_503(self):
        block = balanced_block(self.server, CATCH_ALL)
        # The workspace proxy answers 503 ("server_busy" + Retry-After) for its
        # own admission pressure; converting it would destroy that explanation.
        self.assertNotIn('503', error_page_source_codes(block),
                         'server_busy 503 must keep its explanation and Retry-After')
        # Redirects and gateway failures still become the retryable 503.
        codes = error_page_source_codes(block)
        for code in ('301', '302', '303', '307', '308', '502', '504'):
            self.assertIn(code, codes)
        self.assertIn('@upstream_unavailable', block)
        self.assertIn('proxy_intercept_errors on;', block)

    def test_completions_transport_failures_become_retryable_503(self):
        block = balanced_block(self.server, COMPLETIONS)
        # Only transport errors are intercepted here; the relay's own bounded
        # 413/503 JSON must pass through unchanged.
        codes = error_page_source_codes(block)
        self.assertNotIn('503', codes)
        self.assertIn('502', codes)
        self.assertIn('504', codes)
        self.assertIn('@upstream_unavailable', block)
        upstream_block = balanced_block(self.server, NAMED_UNAVAILABLE)
        self.assertIn('Retry-After', upstream_block)
        self.assertIn('upstream_unavailable', upstream_block)

    def test_completions_are_proxied_to_the_relay_process(self):
        block = balanced_block(self.server, COMPLETIONS)
        self.assertIn('proxy_pass http://cnb_imgsanitize;', block)


if __name__ == '__main__':
    unittest.main()
