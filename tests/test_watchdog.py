"""Execute watchdog functions with fake HTTP/API/sleep helpers; never contact CNB.

Moved from cnb2dsh/test/deploy/test_watchdog.py: the controller now lives in
this repository (cbn-transit/deploy/cnb-ai-watchdog.sh), so its regressions
belong here too.
"""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'deploy' / 'cnb-ai-watchdog.sh'
SOURCE = SCRIPT.read_text(encoding='utf-8')
def find_bash():
    override = os.environ.get('DEPLOY_TEST_BASH')
    if override:
        return override
    if sys.platform == 'win32':
        # System32/bash.exe is a WSL launcher, not the local shell these
        # subprocess tests need. Prefer Git for Windows even when WSL is first.
        roots = []
        git = shutil.which('git')
        if git:
            roots.append(Path(git).resolve().parent.parent)
        for variable in ('ProgramFiles', 'ProgramW6432', 'ProgramFiles(x86)', 'LOCALAPPDATA'):
            value = os.environ.get(variable)
            if value:
                roots.append(Path(value) / ('Programs/Git' if variable == 'LOCALAPPDATA' else 'Git'))
        for root in roots:
            for relative in ('bin/bash.exe', 'usr/bin/bash.exe'):
                candidate = root / relative
                if candidate.is_file():
                    return str(candidate)
        bash = shutil.which('bash')
        if bash and Path(bash).parent.name.lower() not in ('system32', 'sysnative'):
            return bash
        return None
    return shutil.which('bash')


BASH = find_bash()


def function(name, next_marker):
    return SOURCE[SOURCE.index(name + '() {'):SOURCE.index(next_marker, SOURCE.index(name + '() {'))]


@unittest.skipUnless(BASH, 'Bash is required for deployment shell regressions')
class WatchdogTests(unittest.TestCase):
    def bash(self, code):
        env = dict(os.environ, AUDIT_PYTHON=Path(sys.executable).as_posix())
        result = subprocess.run([BASH, '-c', 'set -uo pipefail\n' + code],
                                text=True, encoding='utf-8', errors='replace',
                                capture_output=True, timeout=10, env=env)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        return result.stdout

    def fake_death(self, fresh='old-sn', fixed=False, direct=False):
        return self.bash('''
LAST_FAKEDEATH_FILE=/dev/null
FAIL_FILE=/dev/null
FAKEDEATH_COOLDOWN=1800
API=https://synthetic.invalid
UPSTREAM_PORT=9001
REPO=test/repo
date() { printf '100000\\n'; }
cat() { printf '0\\n'; }
log() { printf '%s\\n' "$*"; }
mark_ok() { printf 'HEALTHY\\n'; }
list_running_info() { LIST_INFO="''' + fresh + ''' synthetic"; }
probe_fixed() { return ''' + ('0' if fixed else '1') + '''; }
probe_url() { printf 'DIRECT %s\\n' "$1"; return ''' + ('0' if direct else '1') + '''; }
api_call() { printf 'API %s %s %s\\n' "$1" "$2" "$3"; API_CODE=503; API_BODY=unavailable; }
''' + function('fake_death', '# poll_recover') + '''
fake_death old-sn || true
''')

    def test_changed_generation_is_never_stopped(self):
        output = self.fake_death(fresh='new-sn')
        self.assertIn('running sn changed', output)
        self.assertNotIn('API POST', output)

    def test_fixed_domain_recovery_aborts_stop(self):
        output = self.fake_death(fixed=True)
        self.assertIn('recovered before stop', output)
        self.assertNotIn('API POST', output)

    def test_healthy_direct_target_is_not_stopped_for_relay_outage(self):
        output = self.fake_death(direct=True)
        self.assertIn('https://synthetic-9001.cnb.run/health', output)
        self.assertIn('healthy directly', output)
        self.assertNotIn('API POST', output)

    def test_unhealthy_unchanged_target_uses_only_expected_sn(self):
        output = self.fake_death()
        self.assertIn('API POST https://synthetic.invalid/workspace/stop {"sn":"old-sn"}', output)
        self.assertNotIn('workspace/start', output)

    def test_locked_out_invocation_cannot_probe_or_mutate_failures(self):
        main = SOURCE[SOURCE.index('# ---------------------------------------------------------------- main ----'):]
        output = self.bash('''
LOCK=/dev/null
flock() { return 1; }
log() { printf '%s\\n' "$*"; }
probe_fixed() { printf 'UNEXPECTED_PROBE\\n'; return 1; }
cat() { printf 'UNEXPECTED_STATE_READ\\n'; }
''' + main)
        self.assertIn('SKIP:', output)
        self.assertNotIn('UNEXPECTED_', output)

    def test_invalid_workspace_list_cannot_trigger_no_running_start(self):
        listing = function('list_running_info', 'read_reg_token()')
        for body, expected in [('{}', 2), ('{"list":[]}', 1),
                               ('{"list":[{"status":"starting"}]}', 3),
                               ('{"list":[{"status":"running","sn":"one","business_id":"synthetic","create_time":"2026-01-01"}]}', 0)]:
            with self.subTest(body=body):
                output = self.bash('''
API=https://synthetic.invalid
REPO=test/repo
python3() { "$AUDIT_PYTHON" "$@"; }
api_call() { API_CODE=200; API_BODY='手工BODY'; }
'''.replace('手工BODY', body) + listing + '''
list_running_info; result=$?
printf 'RESULT=%s\\n' "$result"
''')
                self.assertIn('RESULT=%s' % expected, output)

    def test_probe_requires_http_200_and_valid_json_status(self):
        probe = function('probe_url', '# api_call')
        for body, code, expected in [('{"status": "ok"}', 200, 0),
                                      ('{"status":"ok"}', 503, 1),
                                      ('<html>"status":"ok"</html>', 200, 1),
                                      ('[]', 200, 1)]:
            with self.subTest(body=body, code=code):
                output = self.bash('''
PROBE_TIMEOUT=1
python3() { "$AUDIT_PYTHON" "$@"; }
curl() { printf '%s\\n%s' '手工BODY' '手工CODE'; }
'''.replace('手工BODY', body).replace('手工CODE', str(code)) + probe + '''
probe_url https://synthetic.invalid; result=$?
printf 'RESULT=%s\\n' "$result"
''')
                self.assertIn('RESULT=%s' % expected, output)


if __name__ == '__main__':
    unittest.main()
