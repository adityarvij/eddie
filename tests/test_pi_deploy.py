"""Pi deployment (#18): install_pi.sh renders the systemd units and the token file.

Rendered with --dest, so systemd and /etc are never touched. Booting the units on a Pi is a hardware check.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / 'scripts' / 'install_pi.sh'
UNITS = sorted(p.name for p in (ROOT / 'deploy' / 'systemd').iterdir())
# eddie-update restarts eddie.target from a timer, so it isn't one of the target's services.
EDDIE_SERVICES = [name for name in UNITS if name.endswith('.service') and name != 'eddie-update.service']


def install(dest, *args):
    return subprocess.run(['bash', str(INSTALL), '--dest', dest, *args], capture_output=True, text=True, timeout=60)


def env(dest):
    lines = (Path(dest) / 'eddie.env').read_text().splitlines()
    return dict(line.split('=', 1) for line in lines if line and not line.startswith('#'))


@unittest.skipUnless((ROOT / '.venv' / 'bin' / 'python').exists(), 'needs uv sync --locked')
class InstallPi(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dest)

    def test_renders_every_unit_with_no_placeholder_left(self):
        result = install(self.dest)
        self.assertEqual(0, result.returncode, result.stderr)
        for name in UNITS:
            text = (Path(self.dest) / name).read_text()
            self.assertNotRegex(text, r'@[A-Z]+@', name)
            if name.endswith('.service'):
                self.assertIn(f'WorkingDirectory={ROOT}', text)
            if name in EDDIE_SERVICES:
                self.assertIn(f'EnvironmentFile={self.dest}/eddie.env', text)
                self.assertIn('WantedBy=eddie.target', text)

    def test_every_service_starts_something_that_exists(self):
        install(self.dest)
        for name in UNITS:
            text = (Path(self.dest) / name).read_text()
            match = re.search(r'^ExecStart=(\S+)(.*)$', text, re.M)
            if not name.endswith('.service'):
                continue
            program, args = match.group(1), match.group(2).split()
            self.assertTrue(os.access(program, os.X_OK), program)
            if '-m' in args:
                module = args[args.index('-m') + 1]
                if module.startswith('eddie.'):
                    module = module.split(':')[0]
                    self.assertTrue((ROOT / (module.replace('.', '/') + '.py')).exists(), module)

    def test_dns_binds_port_53_without_root(self):
        install(self.dest)
        text = (Path(self.dest) / 'eddie-dns.service').read_text()
        self.assertIn('AmbientCapabilities=CAP_NET_BIND_SERVICE', text)
        self.assertIn('CapabilityBoundingSet=CAP_NET_BIND_SERVICE', text)
        self.assertNotIn('User=root', text)

    def test_dashboard_runs_voice_only_with_the_voice_environment(self):
        install(self.dest)
        text = (Path(self.dest) / 'eddie-dashboard.service').read_text()
        voice = (ROOT / '.venv-frontend' / 'bin' / 'python').exists()
        self.assertEqual(voice, f'ExecStart={ROOT}/scripts/run_voice.sh' in text)
        self.assertEqual(not voice, 'frontend/serve.py' in text)

    def test_token_file_is_owner_only_and_kept_on_rerun(self):
        self.assertEqual(0, install(self.dest).returncode)
        mode = stat.S_IMODE((Path(self.dest) / 'eddie.env').stat().st_mode)
        self.assertEqual(0o600, mode)
        first = env(self.dest)
        self.assertGreaterEqual(len(first['EDDIE_API_TOKEN']), 32)
        self.assertGreaterEqual(len(first['EDDIE_DASHBOARD_TOKEN']), 32)
        self.assertNotEqual(first['EDDIE_API_TOKEN'], first['EDDIE_DASHBOARD_TOKEN'])
        self.assertNotIn('EDDIE_OLLAMA_URL', first)  # Ollama on the Pi is the default
        install(self.dest, '--ollama', 'http://172.20.10.2:11434')
        install(self.dest, '--ollama', 'http://172.20.10.3:11434')
        second = env(self.dest)
        self.assertEqual(first['EDDIE_API_TOKEN'], second['EDDIE_API_TOKEN'])
        self.assertEqual(first['EDDIE_DASHBOARD_TOKEN'], second['EDDIE_DASHBOARD_TOKEN'])
        self.assertEqual('http://172.20.10.3:11434', second['EDDIE_OLLAMA_URL'])
        self.assertEqual(1, (Path(self.dest) / 'eddie.env').read_text().count('EDDIE_OLLAMA_URL='))
        self.assertEqual(0o600, stat.S_IMODE((Path(self.dest) / 'eddie.env').stat().st_mode))

    def test_refuses_an_ollama_address_off_the_home_network(self):
        install(self.dest)
        before = (Path(self.dest) / 'eddie.env').read_text()
        result = install(self.dest, '--ollama', 'http://8.8.8.8:11434')
        self.assertEqual(1, result.returncode)
        self.assertIn('private IP', result.stderr)
        self.assertEqual(before, (Path(self.dest) / 'eddie.env').read_text())

    def test_update_timer_runs_the_updater_outside_the_target(self):
        install(self.dest)
        service = (Path(self.dest) / 'eddie-update.service').read_text()
        user = subprocess.run(['id', '-un'], capture_output=True, text=True).stdout.strip()
        self.assertIn(f'ExecStart={ROOT}/scripts/update_pi.sh {user}', service)
        self.assertNotIn('PartOf=eddie.target', service)  # it restarts the target, so it mustn't be stopped with it
        self.assertNotIn('EnvironmentFile', service)  # it needs no tokens
        timer = (Path(self.dest) / 'eddie-update.timer').read_text()
        self.assertIn('OnUnitActiveSec=5min', timer)
        self.assertIn('WantedBy=timers.target', timer)

    def test_rejects_unknown_arguments(self):
        self.assertEqual(2, install(self.dest, '--bogus').returncode)


class Scripts(unittest.TestCase):
    def test_pi_scripts_parse(self):
        for name in ('run_pi.sh', 'install_pi.sh', 'update_pi.sh'):
            result = subprocess.run(['bash', '-n', str(ROOT / 'scripts' / name)], capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr)

    def test_update_hold_exits_before_touching_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            hold = Path(tmp) / 'hold'
            hold.touch()
            env = {**os.environ, 'EDDIE_UPDATE_HOLD': str(hold), 'PATH': '/usr/bin:/bin'}
            result = subprocess.run(['bash', str(ROOT / 'scripts' / 'update_pi.sh'), 'nobody'],
                                    capture_output=True, text=True, env=env, timeout=10)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('Updates held', result.stdout)


if __name__ == '__main__':
    unittest.main()
