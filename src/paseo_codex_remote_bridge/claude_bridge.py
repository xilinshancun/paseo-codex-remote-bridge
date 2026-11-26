#!/usr/bin/env python3
"""Paseo stdio transport: Claude Code on an SSH host, workspace tools on this Mac.

The installed copy is standalone. It reads bridge.json beside this file.
SSH identity, host keys, and the account login stay outside this repository.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import struct
import subprocess
import sys
import unicodedata
import urllib.request

MAC_TOOLS = {'Bash', 'Read', 'Write', 'Edit', 'NotebookEdit', 'TaskStop'}
REMOTE_TOOLS = ('AskUserQuestion,EnterPlanMode,ExitPlanMode,TodoWrite,TaskCreate,TaskGet,'
                'TaskList,TaskUpdate,WebSearch,WebFetch,ToolSearch')


def validate_host(host):
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:\-]*', host):
        raise ValueError('ssh host must be a hostname or SSH alias, without options')
    return host


def remote_path(path):
    if not isinstance(path, str) or '\x00' in path:
        raise ValueError('invalid remote path')
    if path == '~':
        return '"$HOME"'
    if path.startswith('~/'):
        return '"$HOME"/' + shlex.quote(path[2:])
    if path.startswith('/'):
        return shlex.quote(path)
    raise ValueError('remote paths must be absolute or begin with ~/')


def load_config(folder=None):
    folder = Path(__file__).resolve().parent if folder is None else Path(folder)
    config = json.loads((folder / 'bridge.json').read_text(encoding='utf-8'))
    if config.get('agent', 'claude') != 'claude':
        raise ValueError('bridge.json is not a claude installation')
    validate_host(config['ssh_host'])
    remote_path(config['remote_claude_bin'])
    remote_path(config['remote_session_root'])
    if 'remote_claude_config_dir' in config:
        remote_path(config['remote_claude_config_dir'])
    local = Path(config['local_claude_bin'])
    if not local.is_absolute():
        raise ValueError('local claude binary must be an absolute path')
    return folder, config


def ssh_args(config):
    return ['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            validate_host(config['ssh_host'])]


def project_directory(path, *, darwin):
    # Claude's JS encoder replaces and hashes UTF-16 code units, including both halves of emoji.
    if darwin:
        path = unicodedata.normalize('NFC', path)
    units = [unit for (unit,) in struct.iter_unpack('<H', path.encode('utf-16-le'))]
    slug = ''.join(chr(unit) if unit < 128 and chr(unit).isalnum() else '-' for unit in units)
    if len(slug) <= 200:
        return slug

    hashed = 0
    for unit in units:
        hashed = (31 * hashed + unit) & 0xffffffff
    magnitude = hashed if hashed < 0x80000000 else 0x100000000 - hashed
    suffix = ''
    while magnitude:
        magnitude, digit = divmod(magnitude, 36)
        suffix = '0123456789abcdefghijklmnopqrstuvwxyz'[digit] + suffix
    return slug[:200] + '-' + (suffix or '0')


def remote_claude_command(config, args, sdk=False):
    env = []
    if 'remote_claude_config_dir' in config:
        env.append('CLAUDE_CONFIG_DIR=' + remote_path(config['remote_claude_config_dir']))
    if sdk:
        env.extend(['CLAUDE_CODE_ENTRYPOINT=sdk-ts', 'CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1'])
    command = ['exec', 'env', *env] if env else ['exec']
    command.append(remote_path(config['remote_claude_bin']))
    command.extend(shlex.quote(arg) for arg in args)
    return ' '.join(command)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def transform_args(args):
    out, servers = [], {}
    i = 0
    has_tools = False
    while i < len(args):
        arg = args[i]
        if arg == '--mcp-config':
            i += 1
            while i < len(args) and not args[i].startswith('--'):
                raw = args[i]
                data = json.loads(raw if raw.lstrip().startswith('{') else Path(raw).read_text())
                servers.update(data.get('mcpServers', {}))
                i += 1
            continue
        if arg == '--setting-sources':
            out.extend([arg, ''])
            i += 2
            continue
        if arg == '--tools':
            has_tools = True
            value = args[i + 1]
            out.extend([arg, REMOTE_TOOLS if value else ''])
            i += 2
            continue
        if arg in ('--settings', '--agents') and i + 1 < len(args):
            value = args[i + 1]
            if not value.lstrip().startswith('{') and Path(value).is_file():
                value = compact(json.loads(Path(value).read_text()))
            out.extend([arg, value])
            i += 2
            continue
        out.append(arg)
        i += 1
    if not has_tools:
        out.extend(['--tools', REMOTE_TOOLS])
    if '--setting-sources' not in out:
        out.extend(['--setting-sources', ''])
    if '--strict-mcp-config' not in out:
        out.append('--strict-mcp-config')
    if 'mac' in servers:
        raise ValueError('MCP server name "mac" is reserved by the Claude bridge')
    local = {k: v for k, v in servers.items() if v.get('type') != 'sdk'}
    remote = {k: ({'type': 'sdk', 'name': k} if k in local else v) for k, v in servers.items()}
    remote['mac'] = {'type': 'sdk', 'name': 'mac'}
    out.extend(['--mcp-config', compact({'mcpServers': remote})])
    return out, local


class StdioMcp:
    def __init__(self, command, args, cwd, env=None, filtered=False):
        self.command, self.args, self.cwd = command, args, cwd
        self.env, self.filtered = env, filtered
        self.proc = None
        self.lock = asyncio.Lock()

    async def call(self, message):
        async with self.lock:
            if self.proc is None:
                self.proc = await asyncio.create_subprocess_exec(
                    self.command, *self.args, cwd=self.cwd, env=self.env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL, limit=16 * 1024 * 1024,
                    start_new_session=True)
            if self.filtered and message.get('method') == 'tools/call':
                if message.get('params', {}).get('name') not in MAC_TOOLS:
                    return {'jsonrpc': '2.0', 'id': message.get('id'),
                            'error': {'code': -32601, 'message': 'Tool is not exposed by the Mac bridge'}}
            self.proc.stdin.write((compact(message) + '\n').encode())
            await self.proc.stdin.drain()
            if 'id' not in message:
                return {'jsonrpc': '2.0', 'id': 0, 'result': {}}
            try:
                while True:
                    line = await self.proc.stdout.readline()
                    if not line:
                        raise RuntimeError('Local MCP process exited')
                    response = json.loads(line)
                    if response.get('id') != message['id']:
                        continue
                    if self.filtered and message.get('method') == 'tools/list':
                        tools = response.get('result', {}).get('tools', [])
                        response['result']['tools'] = [t for t in tools if t['name'] in MAC_TOOLS]
                        for tool in response['result']['tools']:
                            tool['description'] = ("Runs on the user's Mac, in the local project. "
                                                    + tool.get('description', ''))
                            tool.setdefault('annotations', {})['readOnlyHint'] = tool['name'] == 'Read'
                    return response
            except asyncio.CancelledError:
                await self.close()
                raise

    async def close(self):
        if self.proc and self.proc.returncode is None:
            os.killpg(self.proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.proc.wait(), 2)
            except asyncio.TimeoutError:
                os.killpg(self.proc.pid, signal.SIGKILL)
                await self.proc.wait()
        self.proc = None


class HttpMcp:
    def __init__(self, config):
        self.config = config
        self.session = None
        self.protocol = '2025-03-26'
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, message):
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   **self.config.get('headers', {})}
        if self.session:
            headers['Mcp-Session-Id'] = self.session
            headers['MCP-Protocol-Version'] = self.protocol
        req = urllib.request.Request(self.config['url'], data=compact(message).encode(), headers=headers)
        with self.opener.open(req, timeout=600) as response:
            self.session = response.headers.get('Mcp-Session-Id') or self.session
            if response.status == 202 or 'id' not in message:
                return {'jsonrpc': '2.0', 'id': 0, 'result': {}}
            if 'text/event-stream' in response.headers.get('Content-Type', ''):
                data = []
                for line in response:
                    line = line.decode().rstrip('\r\n')
                    if line.startswith('data:'):
                        data.append(line[5:].lstrip())
                    elif not line and data:
                        result = json.loads('\n'.join(data))
                        data = []
                        if result.get('id') == message.get('id'):
                            return result
                raise RuntimeError('MCP event stream ended without a response')
            result = json.loads(response.read())
            if message.get('method') == 'initialize':
                self.protocol = result.get('result', {}).get('protocolVersion', self.protocol)
            return result

    async def call(self, message):
        return await asyncio.to_thread(self.request, message)

    async def close(self):
        pass


class Bridge:
    def __init__(self, args, folder, config):
        self.folder, self.config = folder, config
        self.ssh = ssh_args(config)
        self.local_claude = str(Path(config['local_claude_bin']))
        self.cwd = str(Path.cwd().resolve())
        digest = hashlib.sha256(self.cwd.encode()).hexdigest()[:20]
        self.remote_cwd_shell = remote_path(config['remote_session_root'].rstrip('/') + '/' + digest)
        self.args, configs = transform_args(args)
        env = dict(os.environ)
        for key in list(env):
            if key.startswith('ANTHROPIC_') or key in ('CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDECODE'):
                del env[key]
        env['CLAUDE_CONFIG_DIR'] = str(folder / 'tools')
        self.servers = {'mac': StdioMcp(self.local_claude,
                        ['--setting-sources', '', '--strict-mcp-config', 'mcp', 'serve'],
                        self.cwd, env, filtered=True)}
        for name, cfg in configs.items():
            if cfg.get('type') == 'http':
                self.servers[name] = HttpMcp(cfg)
            elif cfg.get('type', 'stdio') == 'stdio':
                self.servers[name] = StdioMcp(cfg['command'], cfg.get('args', []), self.cwd,
                                            {**os.environ, **cfg.get('env', {})})
            else:
                raise ValueError('Unsupported local MCP transport for ' + name + ': ' + str(cfg.get('type')))
        self.tasks = {}
        self.proc = None
        self.session = None
        self.write_lock = asyncio.Lock()
        self.sync_lock = asyncio.Lock()

    async def send(self, message):
        async with self.write_lock:
            if self.proc.returncode is None and not self.proc.stdin.is_closing():
                self.proc.stdin.write((compact(message) + '\n').encode())
                await self.proc.stdin.drain()

    def initialize(self, message):
        request = message['request']
        request['sdkMcpServers'] = list(dict.fromkeys(request.get('sdkMcpServers', []) + list(self.servers)))
        aliases = request.setdefault('toolAliases', {})
        aliases.update({tool: 'mcp__mac__' + tool for tool in MAC_TOOLS})
        prompt = (
            '\n\n## Remote Claude / Mac workspace\n'
            'The Claude process and model connection run on the SSH host. ALL project files, '
            'shell commands and edits belong to the user\'s Mac, NOT the remote filesystem. '
            'The actual project working directory is ' + self.cwd + '. '
            'Use mcp__mac__Read, Bash, Edit, Write and NotebookEdit for project work. '
            'Bash executes on macOS in the project working directory; use it for grep/glob/git/tests. '
            'Read applicable AGENTS.md and CLAUDE.md in this Mac directory and its ancestors '
            'with Mac tools before working. Use Paseo MCP for workspace/agent/browser operations. '
            'The remote current directory is only session storage, never a project checkout. '
            'Do not use remote paths for user files. Permission choices are owned by Paseo; '
            'do not work around a denied tool request.\n')
        prior = request.get('appendSystemPrompt') or ''
        request['appendSystemPrompt'] = ('\n'.join(prior) if isinstance(prior, list) else prior) + prompt

    async def input(self, reader):
        while True:
            line = await reader.readline()
            if not line:
                self.proc.stdin.close()
                return
            message = json.loads(line)
            req = message.get('request', {})
            if message.get('type') == 'control_request' and req.get('subtype') == 'initialize':
                self.initialize(message)
            if message.get('type') == 'control_cancel_request':
                task = self.tasks.get(message.get('request_id'))
                if task:
                    task.cancel()
                    continue
            await self.send(message)

    async def handle_mcp(self, message):
        rid = message['request_id']
        try:
            request = message['request']
            response = await self.servers[request['server_name']].call(request['message'])
            await self.send({'type': 'control_response', 'response': {
                'subtype': 'success', 'request_id': rid, 'response': {'mcp_response': response}}})
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            await self.send({'type': 'control_response', 'response': {
                'subtype': 'error', 'request_id': rid, 'error': str(exc)}})
        finally:
            self.tasks.pop(rid, None)

    async def output(self):
        while True:
            line = await self.proc.stdout.readline()
            if not line:
                return
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                sys.stderr.buffer.write(line)
                sys.stderr.buffer.flush()
                continue
            req = message.get('request', {})
            if (message.get('type') == 'control_request' and req.get('subtype') == 'mcp_message'
                    and req.get('server_name') in self.servers):
                task = asyncio.create_task(self.handle_mcp(message))
                self.tasks[message['request_id']] = task
                continue
            if message.get('session_id'):
                self.session = message['session_id']
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
            if message.get('type') == 'result':
                await self.sync_history()

    async def sync_history(self):
        if not self.session or not re.fullmatch(r'[a-fA-F0-9-]{36}', self.session):
            return
        async with self.sync_lock:
            probe = await asyncio.create_subprocess_exec(
                *self.ssh, 'cd ' + self.remote_cwd_shell + ' && uname -s && pwd -P',
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                data, _ = await asyncio.wait_for(probe.communicate(), 15)
            except asyncio.TimeoutError:
                probe.kill()
                await probe.wait()
                return
            if probe.returncode != 0:
                return
            system, _, cwd = data.partition(b'\n')
            absolute = cwd.removesuffix(b'\n').decode()
            if not absolute.startswith('/'):
                return
            folder = project_directory(absolute, darwin=system == b'Darwin')
            config_dir = self.config.get('remote_claude_config_dir', '~/.claude')
            history = config_dir.rstrip('/') + '/projects/' + folder + '/' + self.session + '.jsonl'
            process = await asyncio.create_subprocess_exec(
                *self.ssh, 'cat -- ' + remote_path(history),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            try:
                body, _ = await asyncio.wait_for(process.communicate(), 15)
                if process.returncode == 0 and body:
                    local_folder = project_directory(self.cwd, darwin=sys.platform == 'darwin')
                    destination = self.folder / 'projects' / local_folder
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    path = destination / (self.session + '.jsonl')
                    temp = path.with_suffix('.jsonl.tmp')
                    temp.write_bytes(body)
                    temp.chmod(0o600)
                    temp.replace(path)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def run(self):
        (self.folder / 'tools').mkdir(parents=True, exist_ok=True, mode=0o700)
        command = ('mkdir -p ' + self.remote_cwd_shell + ' && cd ' + self.remote_cwd_shell
                   + ' && ' + remote_claude_command(self.config, self.args, sdk=True))
        self.proc = await asyncio.create_subprocess_exec(
            *self.ssh, command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=None, limit=16 * 1024 * 1024)
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=16 * 1024 * 1024)
        transport, _ = await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer)
        input_task = asyncio.create_task(self.input(reader))
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: self.proc.terminate() if self.proc.returncode is None else None)
        try:
            await self.output()
            return await self.proc.wait()
        finally:
            input_task.cancel()
            transport.close()
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
            await asyncio.gather(*(s.close() for s in self.servers.values()), return_exceptions=True)
            if self.proc.returncode is None:
                self.proc.terminate()
                await self.proc.wait()
            await self.sync_history()


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        folder, config = load_config()
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        os.write(2, b'claude remote bridge: invalid configuration\n')
        return 127
    if args in (['--version'], ['-v'], ['--help'], ['-h'], ['auth', 'status']):
        return subprocess.call([*ssh_args(config), remote_claude_command(config, args)])
    if '--input-format' not in args or 'stream-json' not in args:
        print('This launcher is for Paseo/Claude Agent SDK streaming sessions. '
              'Use --version or auth status for diagnostics.', file=sys.stderr)
        return 2
    try:
        return asyncio.run(Bridge(args, folder, config).run())
    except (ValueError, OSError, RuntimeError) as exc:
        print('claude remote bridge: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
