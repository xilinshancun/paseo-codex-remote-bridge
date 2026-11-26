"""Exercise installed Claude instances against a temporary remote shell and CLI."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'scripts/install.py'
SESSION = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
sys.path.insert(0, str(ROOT / 'src'))
from paseo_codex_remote_bridge.claude_bridge import project_directory


# Run the same JavaScript operations as the Claude SDK for the filesystem integration case.
JS_PROJECT_DIR = """
let path = process.argv[1];
if (process.platform === 'darwin') path = path.normalize('NFC');
const slug = path.replace(/[^a-zA-Z0-9]/g, '-');
let hash = 0;
for (let i = 0; i < path.length; i++) hash = (31 * hash + path.charCodeAt(i)) | 0;
console.log(slug.length <= 200 ? slug : slug.slice(0, 200) + '-' + Math.abs(hash).toString(36));
"""

# Replace only the SSH hop. Every generated command passes through a real shell.
DRIVER = """
import sys
sys.path.insert(0, sys.argv.pop(1))
import claude_bridge
claude_bridge.ssh_args = lambda config: ['env', '-u', 'CLAUDE_CONFIG_DIR', '/bin/sh', '-c']
sys.exit(claude_bridge.main())
"""

CLI = """
import json, os, re, sys
from pathlib import Path
account = Path(os.environ.get('CLAUDE_CONFIG_DIR', Path.home() / '.claude'))
if sys.argv[1:] == ['auth', 'status']:
    print(json.dumps(str(account)))
else:
    assert os.environ['CLAUDE_CODE_ENTRYPOINT'] == 'sdk-ts'
    cwd = Path.cwd()
    project = os.environ.get('TEST_TRANSCRIPT_DIR') or re.sub(r'[^a-zA-Z0-9]', '-', str(cwd))
    session = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    path = account / 'projects' / project / (session + '.jsonl')
    if '--resume' in sys.argv:
        assert sys.argv[sys.argv.index('--resume') + 1] == session and path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([str(account), str(cwd), '--resume' in sys.argv]) + '\\n')
    print(json.dumps({'type': 'result', 'session_id': session}))
"""


class ClaudeAccountTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.home = self.base / 'SSH home'
        self.home.mkdir()
        self.project = self.base / 'Mac workspace'
        self.project.mkdir()
        self.test_env = {}
        self.cli = self.home / "CLI's $name"
        self.cli.write_text('#!' + sys.executable + '\n' + CLI)
        self.cli.chmod(0o755)

    def install(self, name, *extra):
        instance = self.base / name
        subprocess.run([
            sys.executable, str(INSTALLER), '--agent', 'claude', '--ssh-host', 'devbox',
            '--local-claude-bin', '/unused/claude', '--remote-claude-bin', '~/' + self.cli.name,
            '--profile', name, '--install-dir', str(instance), *extra,
        ], check=True, capture_output=True, timeout=10)
        return instance

    def launch(self, instance, *args):
        result = subprocess.run([sys.executable, '-c', DRIVER, str(instance), *args],
                                cwd=self.project, input='', capture_output=True, text=True,
                                env=dict(os.environ, HOME=str(self.home), CLAUDE_CONFIG_DIR=str(instance), **self.test_env),
                                timeout=15, check=True)
        return json.loads(result.stdout)

    def check_session(self, instance, account, workspace_root, resume=False):
        args = ['--input-format', 'stream-json']
        if resume:
            args.extend(['--resume', SESSION])
        self.assertEqual(self.launch(instance, *args), {'type': 'result', 'session_id': SESSION})
        [local_history] = (instance / 'projects').glob('*/' + SESSION + '.jsonl')
        name, remote_cwd, resumed = json.loads(local_history.read_text())
        self.assertEqual(Path(name), account)
        self.assertEqual(Path(remote_cwd).parent, workspace_root)
        self.assertEqual(resumed, resume)

    def test_two_accounts_share_one_ssh_home_and_restore_their_own_history(self):
        first = self.install('default')
        name = "team's $config; [work]"
        second = self.install('work', '--remote-claude-config-dir', '~/' + name)
        accounts = [(first, self.home / '.claude'), (second, self.home / name)]
        for instance, account in accounts:
            self.assertEqual(self.launch(instance, 'auth', 'status'), str(account))
            self.check_session(instance, account, account / 'paseo-mac-workspaces')
        for instance, account in accounts:
            self.check_session(instance, account, account / 'paseo-mac-workspaces', resume=True)

    def test_account_with_custom_session_root_and_old_instance_update(self):
        account = self.base / 'absolute account'
        custom_root = self.home / 'shared session root'
        second = self.install('work', '--remote-claude-config-dir', str(account),
                              '--remote-session-root', '~/shared session root')
        self.check_session(second, account, custom_root)
        older = self.install('old')
        config_file = older / 'bridge.json'
        legacy = config_file.read_bytes()
        self.assertNotIn('remote_claude_config_dir', json.loads(legacy))
        (older / 'claude_bridge.py').write_text('old code')
        subprocess.run([sys.executable, str(INSTALLER), '--update', '--install-dir', str(older)],
                       check=True, capture_output=True, timeout=10)
        self.assertEqual(legacy, config_file.read_bytes())
        default = self.home / '.claude'
        self.check_session(older, default, default / 'paseo-mac-workspaces')

    def test_symlinked_account_finds_history_under_physical_session_cwd(self):
        for profile, root, args in [('default', '.claude', []),
                                    ('work', 'second', ['--remote-claude-config-dir', '~/second'])]:
            target = self.base / (profile + ' physical')
            target.mkdir()
            account = self.home / root
            account.symlink_to(target, target_is_directory=True)
            self.check_session(self.install(profile, *args), account, target / 'paseo-mac-workspaces')

    def test_project_directory_utf16_boundaries_and_hashes(self):
        self.assertEqual(project_directory('/' + 'x' * 197 + '😀', darwin=False), '-' + 'x' * 197 + '--')
        self.assertEqual(project_directory('/' + 'x' * 198 + '😀', darwin=False), '-' + 'x' * 198 + '--czg48e')
        deep = '/' + 'q' * 204 + '/🍋e\u0301'
        prefix = '-' + 'q' * 199
        self.assertEqual(project_directory(deep, darwin=False), prefix + '-n15gvf')
        self.assertEqual(project_directory(deep, darwin=True), prefix + '-s8lhne')

    @unittest.skipUnless(shutil.which('node'), 'JavaScript transcript-path parity needs Node')
    def test_deep_workspace_and_remote_transcript_match_javascript(self):
        import hashlib
        self.project = self.project / ('a' * 90) / ('b' * 90 + '🍋e\u0301')
        self.project.mkdir(parents=True)
        cwd = self.project.resolve()
        digest = hashlib.sha256(str(cwd).encode()).hexdigest()[:20]

        def sdk_project_directory(path):
            return subprocess.check_output(['node', '-e', JS_PROJECT_DIR, str(path)], text=True).strip()

        for name, root, args in [('default', '.claude', []),
                                 ('work', 'second', ['--remote-claude-config-dir', '~/second'])]:
            account = self.home / root
            target = self.base / ('🍋' + 'c' * 100) / ('d' * 86 + 'e\u0301') / name
            target.mkdir(parents=True)
            account.symlink_to(target, target_is_directory=True)
            session_root = (target / 'paseo-mac-workspaces').resolve()
            instance = self.install(name, *args)
            self.test_env['TEST_TRANSCRIPT_DIR'] = sdk_project_directory(session_root / digest)
            self.check_session(instance, account, session_root)
            self.assertTrue((instance / 'projects' / sdk_project_directory(cwd) / (SESSION + '.jsonl')).is_file())

    def test_option_rejects_codex_updates_and_relative_paths(self):
        instance = self.install('valid')
        shared = ['--remote-claude-config-dir', '~/second']
        for args in (['--update', '--install-dir', str(instance), *shared],
                     ['--ssh-host', 'devbox', '--local-codex-bin', '/unused/codex',
                      '--install-dir', str(self.base / 'invalid'), *shared],
                     ['--agent', 'claude', '--ssh-host', 'devbox', '--local-claude-bin', '/unused/claude',
                      '--install-dir', str(self.base / 'invalid'), '--remote-claude-config-dir', 'relative']):
            with self.subTest(args=args):
                result = subprocess.run([sys.executable, str(INSTALLER), *args],
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 2)
                self.assertFalse((self.base / 'invalid').exists())


if __name__ == '__main__':
    unittest.main()
