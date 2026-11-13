import base64
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'src' / 'paseo_codex_remote_bridge'
sys.path.insert(0, str(PACKAGE))
from image_bridge import transform
BRIDGE = str(PACKAGE / 'image_bridge.py')
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')

class Tests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.image = Path(tmp.name) / '图片 空格.png'
        self.image.write_bytes(PNG)
    def frame(self, method='turn/start'):
        return json.dumps(dict(id=900719925474099312345, method=method, params=dict(input=[dict(type='text', text='汉字\u2028😀\u2029\\n'), dict(type='localImage', path=str(self.image), detail='original')])), ensure_ascii=False).encode() + b'\n'
    def run_child(self, code, data=b''):
        return subprocess.run([sys.executable, BRIDGE, sys.executable, '-c', code], input=data, capture_output=True, timeout=8)
    def test_fields(self):
        for method in ('turn/start', 'turn/steer'):
            frame = self.frame(method)
            result = json.loads(transform(frame, lambda: self.fail('warn')))
            self.assertEqual(result['id'], json.loads(frame)['id'])
            self.assertEqual(result['params']['input'][0], json.loads(frame)['params']['input'][0])
            image = result['params']['input'][1]
            self.assertEqual(image['type'], 'image')
            self.assertEqual(image['detail'], 'original')
            self.assertNotIn('path', image)
            self.assertEqual(base64.b64decode(image['url'].split(',')[1]), PNG)
    def test_unchanged(self):
        frames = [b'raw\n', b'{bad}\n', b'\xff\n', b'[]\n', b'null\n', self.frame('thread/resume'), b'{ "id":1,"result":{"type":"localImage","path":"x"}}\n', b'{"method":"turn/start","params":{"input":[{"type":"text","text":"localImage/path"},{"type":"image","url":"data:x"},{"type":"file","path":"x"},{"type":"localImage","path":2}]}}\n']
        for f in frames:
            self.assertEqual(transform(f, lambda: self.fail('warn')), f)
    def test_missing_invalid_and_redacted_warning(self):
        for data in (None, b'not image'):
            if data is None: self.image.unlink()
            else: self.image.write_bytes(data)
            frame = self.frame()
            result = self.run_child('import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.exit(9)', frame)
            self.assertEqual(result.returncode, 9)
            self.assertEqual(result.stdout, frame)
            self.assertEqual(result.stderr, b'codex-e image bridge: image unavailable locally; preserving native input\n')
    def test_continuous_frames_eof(self):
        data = b'{ "id":0 }\n' + self.frame() + self.frame('turn/steer') + b'final raw'
        result = self.run_child('import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())', data)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b'{ "id":0 }\n' + transform(self.frame(), lambda: None) + transform(self.frame('turn/steer'), lambda: None) + b'final raw')
        self.assertEqual(result.stderr, b'')
    def test_crlf_large_frame_and_no_final_lf(self):
        obj = json.loads(self.frame())
        obj['params']['input'][0]['text'] *= 20000
        obj['params']['input'].append(obj['params']['input'][1].copy())
        for ending in (b'\r\n', b''):
            frame = json.dumps(obj, ensure_ascii=False).encode() + ending
            result = self.run_child('import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())', frame)
            self.assertEqual(result.returncode, 0)
            out = json.loads(result.stdout)
            self.assertEqual(out['params']['input'][0], obj['params']['input'][0])
            self.assertEqual(sum(x['type'] == 'image' for x in out['params']['input']), 2)
            self.assertEqual(result.stdout.endswith(b'\n'), bool(ending))
            if ending: self.assertTrue(result.stdout.endswith(ending))
    def test_early_exit_open_stdin(self):
        with subprocess.Popen([sys.executable, BRIDGE, sys.executable, '-c', 'raise SystemExit(37)'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
            self.assertEqual(p.wait(timeout=5), 37)
            self.assertEqual(p.stdout.read(), b'')
            self.assertEqual(p.stderr.read(), b'')
    def test_broken_child_stdin(self):
        result = self.run_child('import os,time; os.close(0); time.sleep(.1); raise SystemExit(23)', b'x\n' * 1000000)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(result.stderr, b'')
    def test_signal_status(self):
        result = self.run_child('import os,signal; os.kill(os.getpid(), signal.SIGTERM)')
        self.assertEqual(result.returncode, 143)
    def test_signal_forward(self):
        with subprocess.Popen([sys.executable, BRIDGE, sys.executable, '-c', 'import time; print("ready",flush=True); time.sleep(30)'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
            self.assertEqual(p.stdout.readline(), b'ready\n')
            p.send_signal(signal.SIGTERM)
            self.assertEqual(p.wait(timeout=5), 143)
            self.assertEqual(p.stderr.read(), b'')
    def test_broken_stdout(self):
        with subprocess.Popen([sys.executable, BRIDGE, sys.executable, '-c', 'import os,time; time.sleep(.1); os.write(1,b"result")'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as p:
            p.stdout.close()
            self.assertNotEqual(p.wait(timeout=5), 0)
    def test_wrapper_dispatch_and_argument_quoting(self):
        import shlex
        folder = self.image.parent
        install = folder / 'installed bridge'
        subprocess.run([sys.executable, str(ROOT / 'scripts/install.py'),
                        '--ssh-host', 'test-devbox', '--local-codex-bin', '/test/codex',
                        '--install-dir', str(install)], check=True, capture_output=True)
        fake = folder / 'fake-ssh'
        fake.write_text('#!' + sys.executable + '\nimport json,sys\nprint(json.dumps(sys.argv[1:]),flush=True)\nsys.stdout.buffer.write(sys.stdin.buffer.read())\n')
        fake.chmod(0o755)
        launcher = install / 'launcher.py'
        launcher.write_text(launcher.read_text().replace('/usr/bin/ssh', str(fake)))
        for args, bridged in [(['app-server'], True), (['app-server', '--enable', 'goals'], True), (['exec', '--image', 'local image.png', 'text $HOME `literal`'], False), (['app-server', 'generate-json-schema', '--out', 'x'], False)]:
            frame = self.frame()
            result = subprocess.run([str(install / 'codex-remote')] + args, input=frame, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            arguments, output = result.stdout.split(b'\n', 1)
            ssh_args = json.loads(arguments)
            self.assertEqual(ssh_args[-2], 'test-devbox')
            suffix = ssh_args[-1].split('exec "$HOME"/.local/bin/codex', 1)[1]
            self.assertEqual(shlex.split(suffix), args)
            self.assertEqual(output, transform(frame, lambda: None) if bridged else frame)
            self.assertEqual(result.stderr, b'')

    def test_launch_failure(self):
        result = subprocess.run([sys.executable, BRIDGE, '/nonexistent/image-test'], input=b'', capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 127)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'codex-e image bridge: child launch failed\n')
if __name__ == '__main__': unittest.main()
