#!/usr/bin/env python3
"""Loopback-only Streamable HTTP MCP server exposing Mac workspace tools to remote Antigravity."""
import argparse
import base64
import fcntl
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile
import threading

VERSION = '1.0.0'
SUPPORTED_PROTOCOLS = ('2025-06-18', '2025-03-26', '2024-11-05')
DEFAULT_PROTOCOL = '2025-03-26'
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_BYTES = 512 * 1024
MAX_READ_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAC_EXTRA_BINS = (
    '/opt/homebrew/bin',
    '/opt/homebrew/sbin',
    '/usr/local/bin',
    '/usr/local/go/bin',
)
HOME_EXTRA_BINS = (
    '.local/bin',
    'go/bin',
    '.orbstack/bin',
    '.cargo/bin',
)

TOOLS = [
    {
        'name': 'exec',
        'description': (
            "Execute a shell command on the user's Mac via /bin/bash. "
            'Use for macOS-side tools such as gh, devbox, go, kubectl, helm, and docker.'
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'command': {
                    'type': 'string',
                    'description': 'Command line passed to /bin/bash -c on macOS.',
                },
                'cwd': {
                    'type': 'string',
                    'description': 'Optional Mac working directory (absolute or ~/ path).',
                },
                'timeout_seconds': {
                    'type': 'integer',
                    'minimum': 1,
                    'maximum': 3600,
                    'description': 'Timeout in seconds (default 180, max 3600).',
                },
            },
            'required': ['command'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'read_file',
        'description': (
            "Read a UTF-8 text file or PNG/JPEG/GIF/WebP image from the user's Mac filesystem."
        ),
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Mac file path (absolute or ~/ path).',
                },
                'start_line': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': 'Optional 1-based start line for text files.',
                },
                'end_line': {
                    'type': 'integer',
                    'minimum': 1,
                    'description': 'Optional 1-based inclusive end line for text files.',
                },
            },
            'required': ['path'],
            'additionalProperties': False,
        },
    },
    {
        'name': 'write_file',
        'description': "Atomically write a UTF-8 text file on the user's Mac filesystem.",
        'inputSchema': {
            'type': 'object',
            'properties': {
                'path': {
                    'type': 'string',
                    'description': 'Mac file path (absolute or ~/ path).',
                },
                'content': {
                    'type': 'string',
                    'description': 'UTF-8 file content to write.',
                },
                'overwrite': {
                    'type': 'boolean',
                    'description': 'Set true to overwrite an existing regular file.',
                },
            },
            'required': ['path', 'content'],
            'additionalProperties': False,
        },
    },
]


def parse_listen(address):
    if not isinstance(address, str) or ':' not in address:
        raise ValueError('listen must be 127.0.0.1:PORT')
    host, raw_port = address.rsplit(':', 1)
    if host != '127.0.0.1':
        raise ValueError('MCP server must bind strictly to 127.0.0.1')
    port = int(raw_port)
    if not 0 <= port <= 65535:
        raise ValueError('invalid port')
    return host, port


def resolve_mac_path(raw):
    if not isinstance(raw, str) or not raw or '\x00' in raw:
        raise ValueError('path must be a non-empty string')
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        raise ValueError('path must be absolute or begin with ~/')
    return expanded


def detect_image_mime(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    if len(data) >= 12 and data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return 'image/webp'
    return None


def augmented_env():
    env = dict(os.environ)
    existing = [p for p in env.get('PATH', '').split(os.pathsep) if p]
    seen = set(existing)
    home = Path.home()
    extras = list(MAC_EXTRA_BINS) + [str(home / rel) for rel in HOME_EXTRA_BINS]
    extras.append('/Applications/Docker.app/Contents/Resources/bin')
    prepend = [p for p in extras if p not in seen and os.path.isdir(p)]
    if prepend or not existing:
        fallback = existing or ['/usr/bin', '/bin', '/usr/sbin', '/sbin']
        env['PATH'] = os.pathsep.join(prepend + fallback)
    return env


def truncate_bytes(data, limit):
    if len(data) <= limit:
        return data.decode('utf-8', errors='replace'), False
    return data[:limit].decode('utf-8', errors='replace'), True


def text_result(text, is_error=False):
    return {'content': [{'type': 'text', 'text': text}], 'isError': bool(is_error)}


def load_actions(path):
    actions = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(actions, dict) or not actions:
        raise ValueError('expected a nonempty map of named actions')
    for name, argv in actions.items():
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', name):
            raise ValueError('invalid action name')
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and chr(0) not in a for a in argv):
            raise ValueError('expected a fixed command argument list')
        if not Path(argv[0]).is_absolute():
            raise ValueError('expected an absolute executable path')
    return actions


class MacToolState:
    def __init__(self, token, default_cwd, server_name='paseo-mac-bridge', actions=None):
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError('token is too short')
        self.expected_auth = 'Bearer ' + token
        self.default_cwd = Path(default_cwd).expanduser().resolve()
        if not self.default_cwd.is_dir():
            raise ValueError('default cwd does not exist')
        self.server_name = server_name
        self.actions = actions
        self.tools = TOOLS if actions is None else [
            {'name': name, 'description': name.replace('_', ' '),
             'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}}
            for name in actions
        ]
        self.revoked_sessions = set()
        self.lock = threading.Lock()

    def authorized(self, header):
        if not isinstance(header, str):
            return False
        return hmac.compare_digest(header.encode('utf-8'), self.expected_auth.encode('utf-8'))

    def revoke_session(self, session_id):
        if session_id:
            with self.lock:
                self.revoked_sessions.add(session_id)

    def session_revoked(self, session_id):
        if not session_id:
            return False
        with self.lock:
            return session_id in self.revoked_sessions

    def run_exec(self, args):
        command = args.get('command')
        if not isinstance(command, str) or not command.strip():
            return text_result('command must be a non-empty string', is_error=True)
        raw_cwd = args.get('cwd')
        try:
            cwd = self.default_cwd if raw_cwd in (None, '') else resolve_mac_path(raw_cwd).resolve()
        except ValueError as exc:
            return text_result(str(exc), is_error=True)
        if not cwd.is_dir():
            return text_result(f'working directory does not exist: {cwd}', is_error=True)
        timeout = args.get('timeout_seconds', 180)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 3600:
            return text_result('timeout_seconds must be an integer between 1 and 3600', is_error=True)
        return self.run_command(['/bin/bash', '-c', command], cwd, timeout)

    def run_command(self, argv, cwd, timeout):
        proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            env=augmented_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout_raw, stderr_raw = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                stdout_raw, stderr_raw = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                stdout_raw, stderr_raw = proc.communicate()
            except ProcessLookupError:
                stdout_raw, stderr_raw = b'', b''
        stdout_text, out_trunc = truncate_bytes(stdout_raw, MAX_OUTPUT_BYTES)
        stderr_text, err_trunc = truncate_bytes(stderr_raw, MAX_OUTPUT_BYTES)
        parts = [f'cwd: {cwd}', f'exit_code: {"timeout" if timed_out else proc.returncode}']
        if stdout_text:
            parts.append('--- stdout ---\n' + stdout_text + ('\n[stdout truncated]' if out_trunc else ''))
        if stderr_text:
            parts.append('--- stderr ---\n' + stderr_text + ('\n[stderr truncated]' if err_trunc else ''))
        if timed_out:
            parts.append(f'command timed out after {timeout}s')
        return text_result('\n'.join(parts), is_error=(timed_out or proc.returncode != 0))

    def run_read_file(self, args):
        try:
            target = resolve_mac_path(args.get('path'))
        except ValueError as exc:
            return text_result(str(exc), is_error=True)
        if not target.is_file():
            return text_result(f'file not found: {target}', is_error=True)
        start_line = args.get('start_line')
        end_line = args.get('end_line')
        if start_line is not None and (isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1):
            return text_result('start_line must be an integer >= 1', is_error=True)
        if end_line is not None and (isinstance(end_line, bool) or not isinstance(end_line, int) or end_line < 1):
            return text_result('end_line must be an integer >= 1', is_error=True)
        if start_line is not None and end_line is not None and end_line < start_line:
            return text_result('end_line must be >= start_line', is_error=True)
        try:
            size = target.stat().st_size
            with target.open('rb') as stream:
                header = stream.read(16)
                mime = detect_image_mime(header) if start_line is None and end_line is None else None
                if mime is not None:
                    if size > MAX_IMAGE_BYTES:
                        return text_result(f'image exceeds {MAX_IMAGE_BYTES} bytes', is_error=True)
                    data = header + stream.read(MAX_IMAGE_BYTES + 1 - len(header))
                    return {
                        'content': [{
                            'type': 'image',
                            'data': base64.b64encode(data).decode('ascii'),
                            'mimeType': mime,
                        }],
                        'isError': False,
                    }
                raw = header + stream.read(MAX_READ_BYTES + 1 - len(header))
        except OSError as exc:
            return text_result(f'read failed: {exc}', is_error=True)
        truncated = len(raw) > MAX_READ_BYTES
        text = raw[:MAX_READ_BYTES].decode('utf-8', errors='replace')
        if start_line is not None or end_line is not None:
            lines = text.splitlines(keepends=True)
            lo = (start_line or 1) - 1
            hi = end_line if end_line is not None else len(lines)
            text = ''.join(lines[lo:hi])
        if truncated:
            text += '\n[file truncated at 256 KiB]'
        return text_result(text, is_error=False)

    def run_write_file(self, args):
        try:
            target = resolve_mac_path(args.get('path'))
        except ValueError as exc:
            return text_result(str(exc), is_error=True)
        content = args.get('content')
        if not isinstance(content, str):
            return text_result('content must be a string', is_error=True)
        overwrite = bool(args.get('overwrite', False))
        if target.is_symlink():
            return text_result('refusing to write through symlink', is_error=True)
        if target.exists() and not overwrite:
            return text_result(f'file already exists (set overwrite=true): {target}', is_error=True)
        if target.exists() and target.is_dir():
            return text_result(f'target is a directory: {target}', is_error=True)
        parent = target.parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
            fd, temp_path = tempfile.mkstemp(prefix='.mcp-write-', dir=str(parent))
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(content.encode('utf-8'))
                os.replace(temp_path, target)
            finally:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
        except OSError as exc:
            return text_result(f'write failed: {exc}', is_error=True)
        return text_result(f'wrote {len(content.encode("utf-8"))} bytes to {target}', is_error=False)

    def handle_rpc(self, message):
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
            return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}, None
        if 'id' not in message:
            return None, None
        req_id = message['id']
        method = message.get('method')
        params = message.get('params') or {}
        if not isinstance(params, dict):
            return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32602, 'message': 'Invalid params'}}, None
        if method == 'initialize':
            requested = params.get('protocolVersion')
            protocol = requested if requested in SUPPORTED_PROTOCOLS else DEFAULT_PROTOCOL
            session_id = secrets.token_urlsafe(24)
            return {
                'jsonrpc': '2.0',
                'id': req_id,
                'result': {
                    'protocolVersion': protocol,
                    'capabilities': {'tools': {'listChanged': False}},
                    'serverInfo': {'name': self.server_name, 'version': VERSION},
                    'instructions': 'Call a listed Mac action with an empty object.' if self.actions is not None else (
                        "Executes commands and reads/writes files directly on the user's Mac host. "
                        'Use exec for macOS CLI tools (gh, devbox, go, kubectl, docker) and '
                        'read_file for Mac paths outside sshfs mounts.'
                    ),
                },
            }, session_id
        if method == 'ping':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {}}, None
        if method == 'tools/list':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {'tools': self.tools}}, None
        if method == 'resources/list':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {'resources': []}}, None
        if method == 'prompts/list':
            return {'jsonrpc': '2.0', 'id': req_id, 'result': {'prompts': []}}, None
        if method == 'tools/call':
            name = params.get('name')
            tool_args = params.get('arguments', {})
            # agy 1.2.13 also sends HTTP tools/call arguments as a JSON string.
            if isinstance(tool_args, str):
                try:
                    tool_args = json.loads(tool_args)
                except (ValueError, RecursionError):
                    tool_args = None
            if not isinstance(tool_args, dict):
                return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32602, 'message': 'arguments must be an object'}}, None
            if self.actions is not None:
                if not isinstance(name, str) or name not in self.actions or tool_args != {}:
                    return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32602, 'message': 'Unknown action or unexpected arguments'}}, None
                try:
                    result = self.run_command(self.actions[name], self.default_cwd, 120)
                except OSError:
                    result = text_result('Configured Mac command could not start', is_error=True)
                return {'jsonrpc': '2.0', 'id': req_id, 'result': result}, None
            if name == 'exec':
                return {'jsonrpc': '2.0', 'id': req_id, 'result': self.run_exec(tool_args)}, None
            if name == 'read_file':
                return {'jsonrpc': '2.0', 'id': req_id, 'result': self.run_read_file(tool_args)}, None
            if name == 'write_file':
                return {'jsonrpc': '2.0', 'id': req_id, 'result': self.run_write_file(tool_args)}, None
            return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32602, 'message': f'Unknown tool: {name}'}}, None
        return {'jsonrpc': '2.0', 'id': req_id, 'error': {'code': -32601, 'message': f'Method not found: {method}'}}, None


def valid_loopback_header(value):
    if not value:
        return True
    host = value.split(':', 1)[0].strip().lower()
    return host in ('127.0.0.1', 'localhost')


def valid_origin_header(value):
    if not value:
        return True
    lowered = value.strip().lower()
    return lowered.startswith(('http://127.0.0.1:', 'http://127.0.0.1/', 'http://localhost:', 'http://localhost/')) or lowered in ('http://127.0.0.1', 'http://localhost')


class McpHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, state):
        super().__init__(server_address, McpRequestHandler)
        self.state = state


class McpRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, _format, *_args):
        return

    def send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_empty(self, status, extra_headers=None):
        self.send_response(status)
        self.send_header('Content-Length', '0')
        self.send_header('Cache-Control', 'no-store')
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def check_request_guards(self):
        path = self.path.split('?', 1)[0]
        if path not in ('/mcp', '/mcp/'):
            self.send_json(404, {'error': 'not found'})
            return False
        if not valid_loopback_header(self.headers.get('Host', '')) or not valid_origin_header(self.headers.get('Origin', '')):
            self.send_json(403, {'error': 'forbidden host or origin'})
            return False
        if not self.server.state.authorized(self.headers.get('Authorization')):
            self.send_json(401, {'error': 'unauthorized'})
            return False
        session_id = self.headers.get('Mcp-Session-Id')
        if session_id and self.server.state.session_revoked(session_id):
            self.send_json(404, {'error': 'session terminated'})
            return False
        return True

    def do_GET(self):
        if not self.check_request_guards():
            return
        self.send_empty(405, {'Allow': 'POST, DELETE'})

    def do_DELETE(self):
        if not self.check_request_guards():
            return
        self.server.state.revoke_session(self.headers.get('Mcp-Session-Id'))
        self.send_empty(204)

    def do_POST(self):
        if not self.check_request_guards():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.send_json(400, {'error': 'invalid content-length'})
            return
        if length <= 0 or length > MAX_BODY_BYTES:
            self.send_json(413 if length > MAX_BODY_BYTES else 400, {'error': 'invalid body size'})
            return
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self.send_json(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}})
            return
        extra = {}
        incoming_session = self.headers.get('Mcp-Session-Id')
        if incoming_session:
            extra['Mcp-Session-Id'] = incoming_session
        if isinstance(payload, list):
            if not payload:
                self.send_json(400, {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}})
                return
            responses = []
            for item in payload:
                resp, new_session = self.server.state.handle_rpc(item)
                if new_session:
                    extra['Mcp-Session-Id'] = new_session
                if resp is not None:
                    responses.append(resp)
            if not responses:
                self.send_empty(202, extra)
                return
            self.send_json(200, responses, extra)
            return
        resp, new_session = self.server.state.handle_rpc(payload)
        if new_session:
            extra['Mcp-Session-Id'] = new_session
        if resp is None:
            self.send_empty(202, extra)
            return
        self.send_json(200, resp, extra)


def merge_mcp_config_bytes(existing_raw, fragment_raw, replace=False):
    fragment = json.loads(fragment_raw.decode('utf-8') if isinstance(fragment_raw, bytes) else fragment_raw)
    incoming = fragment.get('mcpServers') if isinstance(fragment, dict) else None
    if not isinstance(incoming, dict) or not incoming:
        raise ValueError('invalid remote-mcp_config.json')
    stripped = (existing_raw.decode('utf-8') if isinstance(existing_raw, bytes) else existing_raw).strip()
    base = json.loads(stripped) if stripped else {'mcpServers': {}}
    if not isinstance(base, dict):
        raise ValueError('existing mcp_config.json must be a JSON object')
    servers = base.setdefault('mcpServers', {})
    if not isinstance(servers, dict):
        raise ValueError('existing mcpServers must be an object')
    for name, value in incoming.items():
        if name in servers and not replace:
            raise ValueError(f'MCP server {name!r} already exists; pass --replace to overwrite')
        servers[name] = value
    return (json.dumps(base, indent=2) + '\n').encode('utf-8')


def merge_mcp_config(target, fragment, replace):
    target.parent.mkdir(parents=True, exist_ok=True)
    # Lock a stable sibling: replacing the destination changes its inode.
    lock_fd = os.open(str(target) + '.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, 'rb+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if target.is_symlink():
            raise ValueError('refusing symlink mcp_config.json')
        existing = target.read_bytes() if target.exists() else b''
        merged = merge_mcp_config_bytes(existing, fragment, replace=replace)
        fd, temp_name = tempfile.mkstemp(prefix='.mcp-config-', dir=target.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(merged)
            os.replace(temp_name, target)
        finally:
            Path(temp_name).unlink(missing_ok=True)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (['--version'], ['-v']):
        print('paseo-antigravity-mcp ' + VERSION)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--listen', default='127.0.0.1:18740')
    parser.add_argument('--token-file')
    parser.add_argument('--default-cwd', default=str(Path.home()))
    parser.add_argument('--actions-file')
    parser.add_argument('--server-name', default='paseo-mac-bridge')
    parser.add_argument('--merge-config', help='Merge stdin remote-mcp_config.json into target mcp_config.json path')
    parser.add_argument('--replace', action='store_true')
    options = parser.parse_args(args)
    if options.merge_config:
        try:
            target = Path(options.merge_config).expanduser()
            merge_mcp_config(target, sys.stdin.buffer.read(), options.replace)
            return 0
        except (OSError, ValueError, json.JSONDecodeError):
            os.write(2, b'antigravity mcp bridge: merge refused\n')
            return 2
    if not options.token_file:
        os.write(2, b'antigravity mcp bridge: --token-file is required\n')
        return 2
    try:
        host, port = parse_listen(options.listen)
        token = Path(options.token_file).read_text(encoding='utf-8').strip()
        state = MacToolState(token=token, default_cwd=options.default_cwd, server_name=options.server_name,
                             actions=load_actions(options.actions_file) if options.actions_file else None)
        server = McpHTTPServer((host, port), state)
    except (OSError, ValueError):
        os.write(2, b'antigravity mcp bridge: invalid configuration or bind failed\n')
        return 127

    def handle_stop(_signum, _frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, handle_stop)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
