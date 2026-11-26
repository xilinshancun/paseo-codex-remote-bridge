"""File-only installation tests; never run SSH, launchctl, Codex, or a model."""
import json
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from paseo_codex_remote_bridge.launcher import app_server_input, remote_path, ssh_command
try:
    import tomllib
except ImportError:
    tomllib = None


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.install = self.base / 'installed bridge $dollar; quote\' 空格'
        self.args = ['--ssh-host', 'devbox-test', '--profile', 'codex-test',
                     '--remote-codex-home', '~/.codex-test',
                     '--remote-codex-bin', '~/.local/bin/codex',
                     '--local-codex-bin', '/Applications/Test App/bin/codex',
                     '--port', '18844', '--install-dir', str(self.install)]

    def run_install(self, args=None):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/install.py')] +
                              (self.args if args is None else args), cwd=self.base,
                              capture_output=True, timeout=30)

    def create(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_generated_json_plist_permissions_and_service_contracts(self):
        result = self.create()
        self.assertEqual(set(x.name for x in self.install.iterdir()), {
            'codex-remote', 'launcher.py', 'image_bridge.py', 'bridge.json', 'provider.json',
            'exec-server.plist', 'ssh-tunnel.plist', 'exec-server.token', 'remote-environments.toml'})
        config = json.loads((self.install / 'bridge.json').read_text())
        self.assertEqual(config['ssh_host'], 'devbox-test')
        self.assertEqual(config['port'], 18844)
        provider = json.loads((self.install / 'provider.json').read_text())
        self.assertEqual(provider['agents']['providers']['codex-test']['extends'], 'codex')
        self.assertEqual(provider['agents']['providers']['codex-test']['label'], 'codex-test')
        self.assertNotIn('type', provider['agents']['providers']['codex-test'])
        self.assertEqual(provider['agents']['providers']['codex-test']['command'],
                         [str(self.install / 'codex-remote')])
        local = plistlib.loads((self.install / 'exec-server.plist').read_bytes())
        tunnel = plistlib.loads((self.install / 'ssh-tunnel.plist').read_bytes())
        for plist in (local, tunnel):
            self.assertIs(plist['RunAtLoad'], True)
            self.assertIs(plist['KeepAlive'], True)
            self.assertEqual(plist['WorkingDirectory'], str(self.install))
        self.assertNotEqual(local['Label'], tunnel['Label'])
        self.assertEqual(local['ProgramArguments'], [config['local_codex_bin'], 'exec-server',
            '--listen', 'ws://127.0.0.1:18844', '--ws-auth', 'capability-token',
            '--ws-token-file', str(self.install / 'exec-server.token'), '--concurrent-requests', '16'])
        self.assertEqual(tunnel['ProgramArguments'], ['/usr/bin/ssh', '-N', '-T', '-o',
            'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3', '-R', '127.0.0.1:18844:127.0.0.1:18844', 'devbox-test'])
        for name in ('exec-server.token', 'remote-environments.toml'):
            self.assertEqual(stat.S_IMODE((self.install / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.install.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.install / 'codex-remote').stat().st_mode), 0o755)
        self.assertEqual((self.install / 'image_bridge.py').read_bytes(),
                         (ROOT / 'src/paseo_codex_remote_bridge/image_bridge.py').read_bytes())
        # Keep test failures from displaying the newly generated capability.
        token = (self.install / 'exec-server.token').read_bytes().strip()
        self.assertTrue(token not in result.stdout and token not in result.stderr)
        self.assertTrue(len(token) >= 32)
        subprocess.run(['/bin/sh', '-n', str(self.install / 'codex-remote')], check=True)

    @unittest.skipIf(tomllib is None, 'TOML parsing uses standard-library tomllib on Python >=3.11')
    def test_generated_toml_parses_and_matches_capability(self):
        self.create()
        environment = tomllib.loads((self.install / 'remote-environments.toml').read_text())
        self.assertEqual(environment['default'], 'mac')
        self.assertIs(environment['include_local'], False)
        self.assertEqual(len(environment['environments']), 1)
        item = environment['environments'][0]
        self.assertEqual(item['id'], 'mac')
        self.assertEqual(item['url'], 'ws://127.0.0.1:18844')
        self.assertTrue(item['auth_bearer_token'] == (self.install / 'exec-server.token').read_text().strip())

    def test_paseo_mcp_routing_is_an_opt_in_for_codex_only(self):
        result = self.run_install(self.args + ['--route-paseo-mcp'])
        self.assertEqual(result.returncode, 0, result.stderr)
        entry, = json.loads((self.install / 'provider.json').read_text())['agents']['providers'].values()
        self.assertEqual(entry['options'], {'mcp_servers': {'paseo': {'environment_id': 'mac'}}})
        before = (self.install / 'provider.json').read_bytes()
        self.assertNotEqual(self.run_install(['--update', '--install-dir', str(self.install),
                                              '--route-paseo-mcp']).returncode, 0)
        self.assertEqual((self.install / 'provider.json').read_bytes(), before)
        for agent in ('antigravity', 'claude'):
            dest = self.base / agent
            result = self.run_install(['--agent', agent, '--ssh-host', 'devbox', '--route-paseo-mcp',
                                       '--install-dir', str(dest)])
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(dest.exists())

    def test_existing_install_and_empty_directory_are_refused(self):
        self.create()
        before = {x.name: x.read_bytes() for x in self.install.iterdir()}
        result = self.run_install()
        self.assertEqual(result.returncode, 2)
        self.assertTrue(before == {x.name: x.read_bytes() for x in self.install.iterdir()})
        empty = self.base / 'empty'
        empty.mkdir()
        self.assertEqual(self.run_install(['--ssh-host', 'devbox', '--local-codex-bin', '/test/codex',
                                         '--install-dir', str(empty)]).returncode, 2)
        self.assertEqual(list(empty.iterdir()), [])

    def test_update_changes_only_code_and_preserves_instance_bytes_and_modes(self):
        self.create()
        protected = ('bridge.json', 'provider.json', 'exec-server.token',
                     'remote-environments.toml', 'exec-server.plist', 'ssh-tunnel.plist')
        before = {name: ((self.install / name).read_bytes(), (self.install / name).stat().st_mode,
                         (self.install / name).stat().st_mtime_ns) for name in protected}
        for name in ('image_bridge.py', 'launcher.py', 'codex-remote'):
            (self.install / name).write_text('old code\n')
        result = self.run_install(['--update', '--install-dir', str(self.install)])
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {name: ((self.install / name).read_bytes(), (self.install / name).stat().st_mode,
                        (self.install / name).stat().st_mtime_ns) for name in protected}
        self.assertTrue(before == after)  # Never display protected bytes in assertion failures.
        self.assertEqual((self.install / 'image_bridge.py').read_bytes(),
                         (ROOT / 'src/paseo_codex_remote_bridge/image_bridge.py').read_bytes())
        self.assertNotEqual((self.install / 'launcher.py').read_text(), 'old code\n')
        self.assertEqual(stat.S_IMODE((self.install / 'codex-remote').stat().st_mode), 0o755)

    def test_update_requires_explicit_directory_complete_install_and_no_config_options(self):
        self.assertEqual(self.run_install(['--update']).returncode, 2)
        self.assertEqual(self.run_install(['--update', '--install-dir', str(self.install)]).returncode, 2)
        self.assertFalse(self.install.exists())
        self.create()
        self.assertEqual(self.run_install(['--update', '--install-dir', str(self.install),
                                          '--ssh-host', 'replacement']).returncode, 2)
        (self.install / 'bridge.json').unlink()
        self.assertEqual(self.run_install(['--update', '--install-dir', str(self.install)]).returncode, 2)

    def test_invalid_hosts_ports_paths_and_profiles_do_not_create_directory(self):
        cases = [('ssh-host', '-oProxyCommand=bad'), ('ssh-host', 'host with space'),
                 ('ssh-host', 'host\ncommand'), ('profile', '../escape'), ('port', '0'),
                 ('port', '65536'), ('remote-codex-home', '$HOME/bad'),
                 ('remote-codex-bin', '~another/bin/codex'), ('local-codex-bin', 'relative')]
        for field, value in cases:
            args = self.args.copy()
            args[args.index('--' + field) + 1] = value
            result = self.run_install(args)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(self.install.exists())

    def test_repository_destinations_and_symlinks_refused(self):
        args = self.args.copy()
        args[-1] = str(ROOT / 'generated-test-do-not-create')
        self.assertEqual(self.run_install(args).returncode, 2)
        self.assertFalse(Path(args[-1]).exists())
        target = self.base / 'actual'
        target.mkdir()
        self.install.symlink_to(target, target_is_directory=True)
        self.assertEqual(self.run_install().returncode, 2)
        self.assertEqual(list(target.iterdir()), [])

    def test_independent_installs_get_distinct_capabilities(self):
        self.create()
        other = self.base / 'other'
        args = self.args.copy()
        args[-1] = str(other)
        self.assertEqual(self.run_install(args).returncode, 0)
        self.assertTrue((self.install / 'exec-server.token').read_bytes() !=
                        (other / 'exec-server.token').read_bytes())

    def test_no_args_uses_direct_ssh_and_invalid_runtime_config_is_redacted(self):
        self.create()
        fake = self.base / 'fake-ssh'
        fake.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n')
        fake.chmod(0o755)
        launcher = self.install / 'launcher.py'
        launcher.write_text(launcher.read_text().replace('/usr/bin/ssh', str(fake)))
        result = subprocess.run([str(self.install / 'codex-remote')], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)[-2], 'devbox-test')
        config = json.loads((self.install / 'bridge.json').read_text())
        config['ssh_host'] = '-oProxyCommand=do-not-run'
        (self.install / 'bridge.json').write_text(json.dumps(config))
        result = subprocess.run([str(self.install / 'codex-remote')], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'codex remote bridge: invalid configuration or launcher unavailable\n')

    def test_default_install_directory_and_profile(self):
        home = self.base / 'isolated-home'
        home.mkdir()
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install.py'),
                                 '--ssh-host', 'devbox', '--local-codex-bin', '/test/codex'],
                                env=dict(os.environ, HOME=str(home)), cwd=self.base,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = home / '.local/share/paseo-codex-remote-bridge/codex-remote'
        config = json.loads((expected / 'bridge.json').read_text())
        self.assertEqual(config['profile'], 'codex-remote')
        self.assertEqual(config['port'], 18733)
        self.assertEqual(config['remote_codex_home'], '~/.codex-remote')
        self.assertEqual(config['remote_codex_bin'], '~/.local/bin/codex')
        self.assertFalse((home / '.ssh').exists())
        self.assertFalse((home / '.paseo').exists())
        self.assertFalse((home / 'Library/LaunchAgents').exists())

    def test_update_refuses_symlinks_without_touching_external_files(self):
        self.create()
        external = self.base / 'external-file'
        external.write_text('preserve me')
        code = self.install / 'launcher.py'
        code.unlink()
        code.symlink_to(external)
        result = self.run_install(['--update', '--install-dir', str(self.install)])
        self.assertEqual(result.returncode, 2)
        self.assertEqual(external.read_text(), 'preserve me')
        self.assertTrue(code.is_symlink())

    def test_remote_shell_expands_only_home_and_preserves_all_arguments(self):
        # Execute the generated remote *shell command* locally against a harmless fixture.
        # No SSH, Codex, or service is launched.
        home = self.base / 'remote HOME with spaces'
        home.mkdir()
        binary = home / "bin's $literal; codex"
        binary.write_text('#!' + sys.executable + '\nimport json,os,sys\nprint(json.dumps([os.environ["CODEX_HOME"],sys.argv[1:]]))\n')
        binary.chmod(0o755)
        config = dict(ssh_host='devbox', remote_codex_home="~/.codex quote' $dollar; `literal`",
                      remote_codex_bin='~/' + binary.name)
        marker = self.base / 'MUST-NOT-EXIST'
        args = ['app-server', '--flag', '', 'a b', "single'quote", 'double"quote',
                '汉字😀', 'line\nbreak', '$(touch ' + str(marker) + ')', '`false`', '$HOME', '*']
        command = ssh_command(config, args)
        result = subprocess.run(['/bin/sh', '-c', command[-1]], env=dict(os.environ, HOME=str(home)),
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual_home, actual_args = json.loads(result.stdout)
        self.assertEqual(actual_home, str(home / config['remote_codex_home'][2:]))
        self.assertEqual(actual_args, args)
        self.assertFalse(marker.exists())
        # Absolute remote paths remain literal even with shell metacharacters.
        config.update(remote_codex_home=str(home / '$unchanged'), remote_codex_bin=str(binary))
        result = subprocess.run(['/bin/sh', '-c', ssh_command(config, args)[-1]],
                                capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [config['remote_codex_home'], args])

    def test_app_server_detection_does_not_expand_cli_image_scope(self):
        for args in ([], ['exec', '--image', 'x'], ['--config', 'x', 'app-server'],
                     ['app-server', 'generate-json-schema']):
            self.assertFalse(app_server_input(args))
        for args in (['app-server'], ['app-server', '--enable', 'goals']):
            self.assertTrue(app_server_input(args))


class ClaudeInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.install = Path(self.temp.name) / 'claude'
        self.args = ['--agent', 'claude', '--ssh-host', 'devbox-test', '--profile', 'claude-test',
                     '--local-claude-bin', '/Applications/Test App/bin/claude',
                     '--remote-claude-bin', '~/.local/bin/claude',
                     '--remote-session-root', '~/.claude/paseo-mac-workspaces',
                     '--install-dir', str(self.install)]

    def test_claude_install_has_no_token_tunnel_or_machine_secrets(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install.py')] + self.args,
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = {x.name for x in self.install.iterdir()}
        self.assertEqual(names, {'claude-remote', 'claude_bridge.py', 'bridge.json', 'provider.json'})
        self.assertFalse((self.install / 'exec-server.token').exists())
        self.assertFalse((self.install / 'ssh-tunnel.plist').exists())
        config = json.loads((self.install / 'bridge.json').read_text())
        self.assertEqual(config['agent'], 'claude')
        self.assertEqual(config['ssh_host'], 'devbox-test')
        self.assertNotIn('port', config)
        provider = json.loads((self.install / 'provider.json').read_text())['agents']['providers']['claude-test']
        self.assertEqual(provider['extends'], 'claude')
        installed = self.install.resolve()
        self.assertEqual(provider['command'], [str(installed / 'claude-remote')])
        self.assertEqual(provider['env']['CLAUDE_CONFIG_DIR'], str(installed))
        source = (self.install / 'claude_bridge.py').read_text()
        self.assertNotIn('20.125.', source)
        self.assertNotIn('azure_cc_us', source)
        self.assertNotIn('auth_bearer_token', source)
        self.assertEqual(stat.S_IMODE((self.install / 'bridge.json').stat().st_mode), 0o600)
        subprocess.run(['/bin/sh', '-n', str(self.install / 'claude-remote')], check=True)

    def test_claude_update_preserves_bridge_json(self):
        subprocess.run([sys.executable, str(ROOT / 'scripts/install.py')] + self.args,
                       check=True, capture_output=True, timeout=10)
        before = (self.install / 'bridge.json').read_bytes()
        (self.install / 'claude_bridge.py').write_text('old\n')
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/install.py'), '--update',
                                 '--install-dir', str(self.install)], capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.install / 'bridge.json').read_bytes(), before)
        self.assertNotEqual((self.install / 'claude_bridge.py').read_text(), 'old\n')


class AntigravityInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.install = self.base / 'antigravity bridge 空格'
        self.args = ['--agent', 'antigravity', '--ssh-host', 'devbox-test',
                     '--profile', 'agy-test', '--port', '18845',
                     '--default-cwd', '/Users/example/projects/demo',
                     '--mcp-server-name', 'mac',
                     '--install-dir', str(self.install)]

    def run_install(self, args=None):
        return subprocess.run([sys.executable, str(ROOT / 'scripts/install.py')] +
                              (self.args if args is None else args), cwd=self.base,
                              capture_output=True, timeout=30)

    def test_antigravity_generated_files_permissions_and_mcp_contract(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual({x.name for x in self.install.iterdir()}, {
            'antigravity-mcp-server', 'antigravity_mcp.py', 'bridge.json',
            'mcp-server.token', 'remote-mcp_config.json',
            'mcp-server.plist', 'ssh-tunnel.plist',
        })
        self.assertEqual(stat.S_IMODE(self.install.stat().st_mode), 0o700)
        for name in ('bridge.json', 'mcp-server.token', 'remote-mcp_config.json'):
            self.assertEqual(stat.S_IMODE((self.install / name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((self.install / 'antigravity-mcp-server').stat().st_mode), 0o755)
        token = (self.install / 'mcp-server.token').read_text().strip()
        self.assertTrue(len(token) >= 32)
        self.assertNotIn(token.encode(), result.stdout + result.stderr)

        mcp_cfg = json.loads((self.install / 'remote-mcp_config.json').read_text())
        entry = mcp_cfg['mcpServers']['mac']
        self.assertEqual(entry['serverUrl'], 'http://127.0.0.1:18845/mcp')
        self.assertTrue(entry['headers']['Authorization'] == 'Bearer ' + token)
        self.assertEqual(entry['timeoutSeconds'], 600)
        self.assertEqual(entry['tools']['exec'], {'eager': True})

        local = plistlib.loads((self.install / 'mcp-server.plist').read_bytes())
        tunnel = plistlib.loads((self.install / 'ssh-tunnel.plist').read_bytes())
        self.assertEqual(local['ProgramArguments'], [
            str(self.install / 'antigravity-mcp-server'),
            '--listen', '127.0.0.1:18845',
            '--token-file', str(self.install / 'mcp-server.token'),
            '--default-cwd', '/Users/example/projects/demo',
        ])
        self.assertEqual(tunnel['ProgramArguments'], [
            '/usr/bin/ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o',
            'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o',
            'ServerAliveCountMax=3', '-R', '127.0.0.1:18845:127.0.0.1:18845', 'devbox-test',
        ])
        subprocess.run(['/bin/sh', '-n', str(self.install / 'antigravity-mcp-server')], check=True)

    def test_antigravity_update_preserves_tokens_and_plists(self):
        self.assertEqual(self.run_install().returncode, 0)
        protected = ('bridge.json', 'mcp-server.token', 'remote-mcp_config.json',
                     'mcp-server.plist', 'ssh-tunnel.plist')
        before = {n: ((self.install / n).read_bytes(), (self.install / n).stat().st_mode) for n in protected}
        (self.install / 'antigravity_mcp.py').write_text('stale\n')
        result = self.run_install(['--update', '--install-dir', str(self.install)])
        self.assertEqual(result.returncode, 0, result.stderr)
        after = {n: ((self.install / n).read_bytes(), (self.install / n).stat().st_mode) for n in protected}
        self.assertTrue(before == after)
        self.assertNotEqual((self.install / 'antigravity_mcp.py').read_text(), 'stale\n')

    def test_antigravity_rejects_foreign_or_unsafe_flags(self):
        bad_cases = [
            self.args + ['--local-codex-bin', '/bin/codex'],
            self.args + ['--local-claude-bin', '/bin/claude'],
            ['--agent', 'antigravity', '--ssh-host', 'devbox', '--default-cwd', 'relative/path',
             '--install-dir', str(self.install)],
            ['--agent', 'antigravity', '--ssh-host', 'devbox', '--mcp-server-name', '../bad',
             '--install-dir', str(self.install)],
        ]
        for case in bad_cases:
            self.assertEqual(self.run_install(case).returncode, 2)
            self.assertFalse(self.install.exists())


if __name__ == '__main__':
    unittest.main()
