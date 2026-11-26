"""Subprocess Streamable HTTP MCP protocol, auth, and tool execution tests."""
import base64
import concurrent.futures
import json
import plistlib
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII='
)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class AntigravityMcpServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.workspace = self.base / 'workspace'
        self.workspace.mkdir()
        self.install = self.base / 'install'
        self.port = free_port()
        subprocess.run([
            sys.executable, str(ROOT / 'scripts/install.py'),
            '--agent', 'antigravity',
            '--ssh-host', 'devbox-test',
            '--port', str(self.port),
            '--default-cwd', str(self.workspace),
            '--install-dir', str(self.install),
        ], check=True, capture_output=True, timeout=10)
        self.wrapper = str(self.install / 'antigravity-mcp-server')
        self.token = (self.install / 'mcp-server.token').read_text().strip()
        self.url = f'http://127.0.0.1:{self.port}/mcp'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.proc = subprocess.Popen(
            [self.wrapper, '--listen', f'127.0.0.1:{self.port}',
             '--token-file', str(self.install / 'mcp-server.token'),
             '--default-cwd', str(self.workspace)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self.addCleanup(self._stop_server)
        self._wait_ready()

    def _stop_server(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for pipe in (self.proc.stdout, self.proc.stderr):
            if pipe is not None:
                pipe.close()

    def _wait_ready(self):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.fail(f'MCP server exited early: {self.proc.stderr.read()!r}')
            try:
                status, _, _ = self.request('POST', {'jsonrpc': '2.0', 'id': 0, 'method': 'ping'})
                if status == 200:
                    return
            except urllib.error.URLError:
                time.sleep(0.05)
        self.fail('MCP server did not start listening in time')

    def request(self, method, body=None, headers=None):
        req_headers = {'Authorization': f'Bearer {self.token}'}
        if body is not None:
            data = json.dumps(body).encode('utf-8')
            req_headers['Content-Type'] = 'application/json'
        else:
            data = None
        if headers:
            req_headers.update(headers)
        req = urllib.request.Request(self.url, data=data, headers=req_headers, method=method)
        try:
            with self.opener.open(req, timeout=10) as resp:
                raw = resp.read()
                return resp.status, dict(resp.headers.items()), (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as err:
            raw = err.read()
            parsed = json.loads(raw) if raw else None
            return err.code, dict(err.headers.items()), parsed

    def test_fixed_actions_restrict_discovery_and_dispatch(self):
        self._stop_server()
        actions = self.base / 'fixed.json'
        actions.write_text(json.dumps({'mac_info': [sys.executable, '-V']}))
        folder = self.base / 'fixed'
        subprocess.run([sys.executable, str(ROOT / 'scripts/install.py'), '--agent', 'antigravity',
                        '--ssh-host', 'devbox-test', '--port', str(self.port),
                        '--actions-file', str(actions), '--install-dir', str(folder)],
                       check=True, capture_output=True, timeout=15)
        argv = plistlib.loads((folder / 'mcp-server.plist').read_bytes())['ProgramArguments']
        self.token = (folder / 'mcp-server.token').read_text().strip()
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._wait_ready()
        def rpc(method, params):
            status, _, body = self.request('POST', {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})
            self.assertEqual(status, 200)
            return body
        self.assertEqual([t['name'] for t in rpc('tools/list', {})['result']['tools']], ['mac_info'])
        for arguments in ({}, '{}'):
            body = rpc('tools/call', {'name': 'mac_info', 'arguments': arguments})
            self.assertFalse(body['result']['isError'])
            self.assertIn('Python', body['result']['content'][0]['text'])
        for invalid in ('', '[]', '"{}"', '{"cwd":"/"}', [], None):
            body = rpc('tools/call', {'name': 'mac_info', 'arguments': invalid})
            self.assertEqual(body['error']['code'], -32602)
        for name, args in [('exec', {}), ('read_file', {}), ('write_file', {}), ('mac_info', {'cwd': '/'})]:
            self.assertIn('error', rpc('tools/call', {'name': name, 'arguments': args}))

    def test_auth_and_dns_rebinding_guards(self):
        status, _, _ = self.request('POST', {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                    headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(status, 401)
        req = urllib.request.Request(self.url, data=b'{}', method='POST',
                                     headers={'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.opener.open(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

        status, _, _ = self.request('POST', {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                    headers={'Origin': 'https://attacker.example'})
        self.assertEqual(status, 403)
        status, _, _ = self.request('POST', {'jsonrpc': '2.0', 'id': 1, 'method': 'ping'},
                                    headers={'Host': f'attacker.example:{self.port}'})
        self.assertEqual(status, 403)

    def test_non_loopback_bind_is_refused(self):
        res = subprocess.run(
            [self.wrapper, '--listen', '0.0.0.0:18899',
             '--token-file', str(self.install / 'mcp-server.token')],
            capture_output=True, timeout=5,
        )
        self.assertEqual(res.returncode, 127)
        self.assertNotIn(self.token.encode(), res.stderr)

    def test_lifecycle_tools_and_session_revocation(self):
        status, headers, body = self.request('POST', {
            'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
            'params': {'protocolVersion': '2025-03-26', 'capabilities': {}, 'clientInfo': {'name': 'agy'}},
        })
        self.assertEqual(status, 200)
        self.assertEqual(body['result']['protocolVersion'], '2025-03-26')
        session_id = headers.get('Mcp-Session-Id') or headers.get('mcp-session-id')
        self.assertTrue(session_id)

        status, _, body = self.request('POST', {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
                                       headers={'Mcp-Session-Id': session_id})
        self.assertEqual(status, 202)
        self.assertIsNone(body)

        status, _, _ = self.request('GET', headers={'Mcp-Session-Id': session_id})
        self.assertEqual(status, 405)

        status, _, listed = self.request('POST', {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
                                         headers={'Mcp-Session-Id': session_id})
        self.assertEqual(status, 200)
        self.assertEqual([t['name'] for t in listed['result']['tools']], ['exec', 'read_file', 'write_file'])

        # exec success & non-zero exit
        status, _, ran = self.request('POST', {
            'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call',
            'params': {'name': 'exec', 'arguments': {'command': 'pwd && printf "ok-%s" "$((20+22))" '}},
        }, headers={'Mcp-Session-Id': session_id})
        self.assertEqual(status, 200)
        self.assertFalse(ran['result']['isError'])
        self.assertIn('ok-42', ran['result']['content'][0]['text'])
        self.assertIn(str(self.workspace), ran['result']['content'][0]['text'])

        status, _, failed = self.request('POST', {
            'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call',
            'params': {'name': 'exec', 'arguments': {'command': 'echo boom >&2; exit 7'}},
        })
        self.assertEqual(status, 200)
        self.assertTrue(failed['result']['isError'])
        self.assertIn('exit_code: 7', failed['result']['content'][0]['text'])
        self.assertIn('boom', failed['result']['content'][0]['text'])

        # exec timeout kills process group cleanly
        status, _, timed_out = self.request('POST', {
            'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call',
            'params': {'name': 'exec', 'arguments': {'command': 'sleep 30', 'timeout_seconds': 1}},
        })
        self.assertEqual(status, 200)
        self.assertTrue(timed_out['result']['isError'])
        self.assertIn('exit_code: timeout', timed_out['result']['content'][0]['text'])

        # write_file + read_file text + read_file PNG image
        target = self.workspace / 'notes.txt'
        status, _, wrote = self.request('POST', {
            'jsonrpc': '2.0', 'id': 6, 'method': 'tools/call',
            'params': {'name': 'write_file', 'arguments': {'path': str(target), 'content': 'a\nb\nc\nd\n'}},
        })
        self.assertFalse(wrote['result']['isError'])
        status, _, dup = self.request('POST', {
            'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
            'params': {'name': 'write_file', 'arguments': {'path': str(target), 'content': 'x'}},
        })
        self.assertTrue(dup['result']['isError'])

        status, _, sliced = self.request('POST', {
            'jsonrpc': '2.0', 'id': 8, 'method': 'tools/call',
            'params': {'name': 'read_file', 'arguments': json.dumps({'path': str(target), 'start_line': 2, 'end_line': 3})},
        })
        self.assertFalse(sliced['result']['isError'])
        self.assertEqual(sliced['result']['content'][0]['text'], 'b\nc\n')

        img = self.workspace / 'pixel.png'
        img.write_bytes(PNG)
        status, _, img_res = self.request('POST', {
            'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'read_file', 'arguments': {'path': str(img)}},
        })
        self.assertFalse(img_res['result']['isError'])
        block = img_res['result']['content'][0]
        self.assertEqual(block['type'], 'image')
        self.assertEqual(block['mimeType'], 'image/png')
        self.assertEqual(base64.b64decode(block['data']), PNG)

        # DELETE revokes session
        status, _, _ = self.request('DELETE', headers={'Mcp-Session-Id': session_id})
        self.assertEqual(status, 204)
        status, _, _ = self.request('POST', {'jsonrpc': '2.0', 'id': 10, 'method': 'ping'},
                                    headers={'Mcp-Session-Id': session_id})
        self.assertEqual(status, 404)

    def test_parallel_config_merges_preserve_each_server(self):
        target = self.base / 'config' / 'mcp_config.json'

        def add_server(name):
            fragment = json.dumps({'mcpServers': {name: {'serverUrl': self.url}}}).encode()
            subprocess.run([self.wrapper, '--merge-config', str(target)], input=fragment,
                           capture_output=True, timeout=10, check=True)

        names = ['mac_alpha', 'mac_beta', 'mac_gamma', 'mac_delta']
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(names)) as workers:
            list(workers.map(add_server, names))
        self.assertEqual(set(json.loads(target.read_text())['mcpServers']), set(names))
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_merge_config_handles_empty_file_and_preserves_other_servers(self):
        remote_cfg = self.base / 'mcp_config.json'
        remote_cfg.write_bytes(b'')  # 0-byte file, matching fresh ~/.gemini/config/mcp_config.json
        fragment = (self.install / 'remote-mcp_config.json').read_bytes()
        res = subprocess.run([self.wrapper, '--merge-config', str(remote_cfg)],
                             input=fragment, capture_output=True, timeout=5)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(stat.S_IMODE(remote_cfg.stat().st_mode), 0o600)
        merged = json.loads(remote_cfg.read_text())
        self.assertIn('mac', merged['mcpServers'])

        # Refuses overwriting without --replace, preserves unrelated servers with --replace
        merged['mcpServers']['other'] = {'serverUrl': 'https://example.invalid/mcp'}
        remote_cfg.write_text(json.dumps(merged))
        self.assertEqual(subprocess.run([self.wrapper, '--merge-config', str(remote_cfg)],
                                        input=fragment, capture_output=True, timeout=5).returncode, 2)
        self.assertEqual(subprocess.run([self.wrapper, '--merge-config', str(remote_cfg), '--replace'],
                                        input=fragment, capture_output=True, timeout=5).returncode, 0)
        updated = json.loads(remote_cfg.read_text())
        self.assertIn('other', updated['mcpServers'])
        self.assertIn('mac', updated['mcpServers'])


if __name__ == '__main__':
    unittest.main()
