import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/paseo_codex_remote_bridge/agy_workspace'))
import agy_workspace
import remote_workspace


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.folder = self.root / 'install with spaces'
        self.env = {**os.environ, 'HOME': str(self.root), 'PYTHONDONTWRITEBYTECODE': '1'}

    def install(self, *args, check=True):
        return subprocess.run([sys.executable, ROOT / 'scripts/install_agy_workspace.py',
                               '--install-dir', self.folder, *args], capture_output=True,
                              env=self.env, check=check, timeout=10)

    def test_install_update_and_refused_inputs(self):
        self.assertNotEqual(self.install('--ssh-host', '-op', check=False).returncode, 0)
        self.assertNotEqual(self.install('--ssh-host', 'devbox', '--remote-agy-bin',
                                         'x/bin', check=False).returncode, 0)
        self.assertFalse(self.folder.exists())
        self.install('--ssh-host', 'devbox')
        provider = json.loads((self.folder / 'provider.json').read_text())
        self.assertEqual(provider, {'agents': {'providers': {'antigravity': {
            'command': [str(self.folder / 'agy-workspace')]}}}})
        cfg = self.folder / 'bridge.json'
        saved = cfg.read_bytes()
        helper = self.folder / 'remote_workspace.py'
        helper.write_text('old helper')
        self.install('--update')
        self.assertEqual(cfg.read_bytes(), saved)
        self.assertEqual(helper.read_bytes(), Path(remote_workspace.__file__).read_bytes())
        self.assertNotEqual(self.install('--ssh-host', 'devbox', check=False).returncode, 0)
        helper.unlink()
        helper.symlink_to(cfg)
        self.assertNotEqual(self.install('--update', check=False).returncode, 0)
        self.assertEqual(cfg.read_bytes(), saved)

    def test_probe_runs_the_remote_helper_with_literal_argv(self):
        self.install('--ssh-host', 'devbox')
        remote_dir = self.root / '.local/share/paseo-agy-workspace'
        remote_dir.mkdir(parents=True)
        shutil.copy(self.folder / 'remote_workspace.py', remote_dir)
        fake = self.root / 'bin/local agy'
        fake.parent.mkdir()
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        fake.chmod(0o700)
        cmd = agy_workspace.remote({'ssh_host': 'devbox'}, 'probe', '~/bin/local agy',
                                   'models', 'two words', '$(false); test')
        self.assertEqual(cmd[-2], 'devbox')
        result = subprocess.run(['/bin/sh', '-c', cmd[-1]], capture_output=True, text=True,
                                env=self.env, check=True)
        self.assertEqual(result.stdout.splitlines(), ['models', 'two words', '$(false); test'])

    def test_resume_mac_history_and_reject_wrong_workspace(self):
        fake = self.root / 'local agy'
        fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
        fake.chmod(0o700)
        self.install('--ssh-host', 'devbox', '--local-agy-bin', str(fake))
        hist = self.root / '.gemini/antigravity-cli/conversations'
        hist.mkdir(parents=True)
        ident = str(uuid.uuid4())
        (hist / (ident + '.pb')).touch()
        launcher = str(self.folder / 'agy-workspace')
        args = ['--input-format', 'stream-json', '--conversation', ident]
        result = subprocess.run([launcher, *args], check=True, env=self.env,
                                cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.stdout.splitlines(), args)
        args = ['--input-format', 'stream-json', '--add-dir', str(self.root)]
        for cwd in (self.root, self.folder):
            result = subprocess.run([launcher, *args], env=self.env, cwd=cwd, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'select a Mac project directory', result.stderr)

    def test_remote_image_and_mount_cleanup(self):
        self.install('--ssh-host', 'devbox')
        helper = [sys.executable, self.folder / 'remote_workspace.py']
        ident = uuid.uuid4().hex
        session = self.root / '.cache/paseo-agy-workspace' / ident
        session.parent.mkdir(mode=0o700, parents=True)
        (session / 'images').mkdir(parents=True)
        folder = 'paseo-antigravity-images-pc123a'
        name = str(uuid.uuid4()) + '.png'
        args = [*helper, 'image', ident, folder, name]
        subprocess.run(args, input=b'image bytes', check=True, env=self.env)
        self.assertEqual((session / 'images' / folder / name).read_bytes(), b'image bytes')
        result = subprocess.run([*args[:-1], '../oops'], input=b'no', env=self.env, capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        shutil.rmtree(session)
        fake = self.root / 'sshfs'
        fake.write_text('#!/bin/sh\nif [ "$1" = -h ]; then echo " -o passive"; exit; fi\n'
                        '[ "$5" = passive,auto_unmount,idmap=user ] || exit 24\nexit 23\n')
        fake.chmod(0o700)
        result = subprocess.run([*helper, 'mount', ident, '/Mac Project', str(fake)], env=self.env)
        self.assertEqual(result.returncode, 23)
        self.assertFalse(session.exists())

    def test_stream_forwards_images_before_frames_and_returns_child_status(self):
        self.install('--ssh-host', 'devbox')
        # Replace only the process commands for Mac SFTP and SSH. Exercise the actual
        # socketpair, attachment opens, ordered stdin forwarding and exit handling.
        worker = self.root / 'worker.py'
        worker.write_text(
            'import os, pathlib, sys\n'
            'action, *args = sys.argv[1:]\n'
            'received = pathlib.Path.home() / "received"\n'
            'if action == "image": received.write_bytes(sys.stdin.buffer.read())\n'
            'elif action == "run":\n'
            '    for line in sys.stdin.buffer:\n'
            '        if b"Attached image" in line and received.read_bytes() != b"test image": sys.exit(3)\n'
            '        sys.stdout.buffer.write(line)\n'
            '    sys.exit(17)\n'
            'else: sys.stdin.buffer.read()\n')
        driver = self.root / 'driver.py'
        driver.write_text(
            'import sys\n'
            'sys.path.insert(0, sys.argv[1])\n'
            'import agy_workspace as bridge\n'
            'base = [sys.executable, sys.argv[2]]\n'
            'bridge.sftp_command = lambda project: base + ["sftp"]\n'
            'bridge.remote = lambda config, *args: base + list(args)\n'
            'sys.exit(bridge.main(sys.argv[3:]))\n')
        project = self.root / 'Mac project'
        project.mkdir()
        temp = self.root / 'tmp'
        temp.mkdir()
        images = temp / 'paseo-antigravity-images-X15B2g'
        images.mkdir(mode=0o700)
        image = images / (str(uuid.uuid4()) + '.png')
        image.write_bytes(b'test image')
        image.chmod(0o600)
        frames = b'{"type": "other"}\r\n' + json.dumps({
            'message': {'content': 'Attached image: ' + str(image)}}).encode() + b'\n'
        args = [sys.executable, driver, self.folder, worker, '--input-format', 'stream-json',
                '--add-dir', str(project)]
        result = subprocess.run(args, input=frames, cwd=project, env={**self.env, 'TMPDIR': str(temp)},
                                capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(result.stdout, frames)
        (self.root / 'received').unlink()
        image.unlink()
        image.symlink_to(self.folder / 'bridge.json')
        refused = subprocess.run(args, input=frames, cwd=project, env={**self.env, 'TMPDIR': str(temp)},
                                 capture_output=True, timeout=15)
        self.assertNotEqual(refused.returncode, 17)
        self.assertFalse((self.root / 'received').exists())
        self.assertNotIn(b'Attached image:', refused.stdout)

    @unittest.skipUnless(sys.platform == 'darwin', 'requires the Mac SFTP sandbox')
    def test_mac_sandbox_allows_project_only(self):
        self.install('--ssh-host', 'devbox')
        project = self.root / 'workspace'
        project.mkdir()
        (project / 'readable').write_text('project')
        outside = self.root / 'private'
        outside.write_text('outside')
        (project / 'link').symlink_to(outside)
        command = shlex.join([agy_workspace.SANDBOX, '-D', 'PROJECT=' + str(project), '-f',
                              str(self.folder / 'workspace.sb'), agy_workspace.SFTP, '-d', str(project)])
        for name, expected in [('readable', 0), ('link', 1), (str(outside), 1)]:
            out = self.root / 'got'
            result = subprocess.run(['/usr/bin/sftp', '-q', '-D', command, '-b', '-', 'unused'],
                                    input=('get ' + name + ' ' + str(out) + '\n').encode(),
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode == 0, expected == 0, result.stderr)
            if out.exists():
                self.assertEqual(out.read_text(), 'project')
                out.unlink()


if __name__ == '__main__':
    unittest.main()
