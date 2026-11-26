#!/usr/bin/env python3
"""Generate installation files only. Never connect, activate services, or edit Paseo."""
import argparse
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shlex
import sys
import tempfile

# Importing the source package must not generate files in the checkout.
sys.dont_write_bytecode = True
REPOSITORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY / 'src'))
from paseo_codex_remote_bridge.launcher import remote_path, validate_host

SOURCE = REPOSITORY / 'src' / 'paseo_codex_remote_bridge'
CODEX_RUNTIME = ('codex-remote', 'launcher.py', 'image_bridge.py')
CLAUDE_RUNTIME = ('claude-remote', 'claude_bridge.py')
ANTIGRAVITY_RUNTIME = ('antigravity-mcp-server', 'antigravity_mcp.py')
CODEX_INSTANCE = ('bridge.json', 'provider.json', 'exec-server.plist', 'ssh-tunnel.plist',
                  'exec-server.token', 'remote-environments.toml')
CLAUDE_INSTANCE = ('bridge.json', 'provider.json')
ANTIGRAVITY_INSTANCE = ('bridge.json', 'mcp-server.plist', 'ssh-tunnel.plist',
                        'mcp-server.token', 'remote-mcp_config.json')
DEFAULT_PROFILES = {
    'codex': 'codex-remote',
    'claude': 'claude-remote',
    'antigravity': 'antigravity-remote',
}


def write_new(path, data, mode):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, 'wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(data)


def runtime_files(folder, agent):
    nl = chr(10)
    launcher = '#!/bin/sh' + nl + 'exec ' + shlex.quote(sys.executable) + ' '
    if agent == 'claude':
        launcher += shlex.quote(str(folder / 'claude_bridge.py')) + ' "$@"' + nl
        return {'claude-remote': (launcher.encode('utf-8'), 0o755),
                'claude_bridge.py': ((SOURCE / 'claude_bridge.py').read_bytes(), 0o644)}
    if agent == 'antigravity':
        launcher += shlex.quote(str(folder / 'antigravity_mcp.py')) + ' "$@"' + nl
        return {'antigravity-mcp-server': (launcher.encode('utf-8'), 0o755),
                'antigravity_mcp.py': ((SOURCE / 'antigravity_mcp.py').read_bytes(), 0o644)}
    launcher += shlex.quote(str(folder / 'launcher.py')) + ' "$@"' + nl
    return {'codex-remote': (launcher.encode('utf-8'), 0o755),
            'launcher.py': ((SOURCE / 'launcher.py').read_bytes(), 0o644),
            'image_bridge.py': ((SOURCE / 'image_bridge.py').read_bytes(), 0o644)}


def installed_agent(folder):
    data = json.loads((folder / 'bridge.json').read_text(encoding='utf-8'))
    agent = data.get('agent', 'codex')
    if agent not in ('codex', 'claude', 'antigravity'):
        raise ValueError('invalid agent')
    return agent


def plist(label, arguments, folder):
    return plistlib.dumps(dict(Label=label, ProgramArguments=arguments, RunAtLoad=True,
                               KeepAlive=True, WorkingDirectory=str(folder)))


def selected_agent(options):
    agent = options.agent or 'codex'
    if agent not in ('codex', 'claude', 'antigravity'):
        raise ValueError('agent must be codex, claude, or antigravity')
    return agent


def claude_instance_files(options, folder, profile, host):
    nl = chr(10)
    remote_bin = options.remote_claude_bin or '~/.local/bin/claude'
    config_dir = options.remote_claude_config_dir
    if config_dir is not None:
        remote_path(config_dir)
    session_root = options.remote_session_root or (config_dir or '~/.claude').rstrip('/') + '/paseo-mac-workspaces'
    remote_path(remote_bin)
    remote_path(session_root)
    local_bin = Path(options.local_claude_bin).expanduser()
    if not local_bin.is_absolute():
        raise ValueError('local claude binary must be an absolute path')
    config = dict(version=1, agent='claude', profile=profile, ssh_host=host,
                  remote_claude_bin=remote_bin, local_claude_bin=str(local_bin),
                  remote_session_root=session_root)
    if config_dir is not None:
        config['remote_claude_config_dir'] = config_dir
    provider = {'agents': {'providers': {profile: {
        'extends': 'claude', 'label': profile,
        'command': [str(folder / 'claude-remote')],
        'env': {'CLAUDE_CONFIG_DIR': str(folder)}}}}}
    return {
        'bridge.json': ((json.dumps(config, indent=2) + nl).encode(), 0o600),
        'provider.json': ((json.dumps(provider, indent=2) + nl).encode(), 0o644),
    }


def antigravity_instance_files(options, folder, profile, host):
    nl = chr(10)
    port = options.port if options.port is not None else 18740
    if not 1 <= port <= 65535:
        raise ValueError('port must be between 1 and 65535')
    server_name = options.mcp_server_name or 'mac'
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', server_name):
        raise ValueError('invalid mcp server name')
    default_cwd = Path(options.default_cwd or str(Path.home())).expanduser()
    if not default_cwd.is_absolute():
        raise ValueError('default cwd must be an absolute path')
    token = secrets.token_urlsafe(32)
    config = dict(version=1, agent='antigravity', profile=profile, ssh_host=host,
                  port=port, mcp_server_name=server_name, default_cwd=str(default_cwd))
    mcp_config = {
        'mcpServers': {
            server_name: {
                'serverUrl': f'http://127.0.0.1:{port}/mcp',
                'headers': {'Authorization': f'Bearer {token}'},
                'timeoutSeconds': 600,
                'tools': {'exec': {'eager': True}},
            }
        }
    }
    local_args = [
        str(folder / 'antigravity-mcp-server'),
        '--listen', f'127.0.0.1:{port}',
        '--token-file', str(folder / 'mcp-server.token'),
        '--default-cwd', str(default_cwd),
    ]
    fixed_files = {}
    if options.actions_file:
        from paseo_codex_remote_bridge.antigravity_mcp import load_actions
        actions = load_actions(options.actions_file)
        fixed_files['actions.json'] = ((json.dumps(actions, indent=2) + nl).encode(), 0o600)
        local_args += ['--actions-file', str(folder / 'actions.json')]
        mcp_config['mcpServers'][server_name]['tools'] = {name: {'eager': True} for name in actions}
    tunnel_args = ['/usr/bin/ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o',
                   'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o',
                   'ServerAliveCountMax=3', '-R',
                   '127.0.0.1:{0}:127.0.0.1:{0}'.format(port), host]
    label = 'local.paseo-codex-remote-bridge.' + profile
    return {
        'bridge.json': ((json.dumps(config, indent=2) + nl).encode(), 0o600),
        **fixed_files,
        'mcp-server.token': ((token + nl).encode(), 0o600),
        'remote-mcp_config.json': ((json.dumps(mcp_config, indent=2) + nl).encode(), 0o600),
        'mcp-server.plist': (plist(label + '.mcp-server', local_args, folder), 0o644),
        'ssh-tunnel.plist': (plist(label + '.ssh-tunnel', tunnel_args, folder), 0o644),
    }


def instance_files(options, folder):
    nl = chr(10)
    agent = selected_agent(options)
    profile = options.profile or DEFAULT_PROFILES[agent]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', profile):
        raise ValueError('profile must contain only letters, digits, dots, underscores or hyphens')
    host = validate_host(options.ssh_host)
    if agent == 'claude':
        return claude_instance_files(options, folder, profile, host)
    if agent == 'antigravity':
        return antigravity_instance_files(options, folder, profile, host)
    remote_home = options.remote_codex_home or '~/.codex-remote'
    remote_bin = options.remote_codex_bin or '~/.local/bin/codex'
    remote_path(remote_home)
    remote_path(remote_bin)
    local_bin = Path(options.local_codex_bin).expanduser()
    if not local_bin.is_absolute():
        raise ValueError('local codex binary must be an absolute path')
    port = options.port if options.port is not None else 18733
    if not 1 <= port <= 65535:
        raise ValueError('port must be between 1 and 65535')
    config = dict(version=1, agent='codex', profile=profile, ssh_host=host,
                  remote_codex_home=remote_home, remote_codex_bin=remote_bin,
                  local_codex_bin=str(local_bin), port=port)
    provider = {'agents': {'providers': {profile: {
        'extends': 'codex', 'label': profile, 'command': [str(folder / 'codex-remote')]}}}}
    if options.route_paseo_mcp:
        provider['agents']['providers'][profile]['options'] = {'mcp_servers': {'paseo': {'environment_id': 'mac'}}}
    token = secrets.token_urlsafe(32)
    url = 'ws://127.0.0.1:' + str(port)
    environment = ('default = "mac"' + nl + 'include_local = false' + nl + nl
                   + '[[environments]]' + nl
                   + 'id = "mac"' + nl + 'url = ' + json.dumps(url) + nl
                   + 'auth_bearer_token = ' + json.dumps(token) + nl)
    local_args = [str(local_bin), 'exec-server', '--listen', url, '--ws-auth',
                  'capability-token', '--ws-token-file', str(folder / 'exec-server.token'),
                  '--concurrent-requests', '16']
    tunnel_args = ['/usr/bin/ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o',
                   'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=15', '-o',
                   'ServerAliveCountMax=3', '-R',
                   '127.0.0.1:{0}:127.0.0.1:{0}'.format(port), host]
    label = 'local.paseo-codex-remote-bridge.' + profile
    return {
        'bridge.json': ((json.dumps(config, indent=2) + nl).encode(), 0o600),
        'provider.json': ((json.dumps(provider, indent=2) + nl).encode(), 0o644),
        'exec-server.token': ((token + nl).encode(), 0o600),
        'remote-environments.toml': (environment.encode(), 0o600),
        'exec-server.plist': (plist(label + '.exec-server', local_args, folder), 0o644),
        'ssh-tunnel.plist': (plist(label + '.ssh-tunnel', tunnel_args, folder), 0o644),
    }


def install(options):
    if options.route_paseo_mcp and (selected_agent(options) != 'codex' or options.update):
        raise ValueError('--route-paseo-mcp requires a new Codex installation')
    if options.remote_claude_config_dir is not None and (options.agent != 'claude' or options.update):
        raise ValueError('--remote-claude-config-dir requires a new Claude installation')
    if options.actions_file and (options.agent != 'antigravity' or options.update):
        raise ValueError('--actions-file requires a new Antigravity instance')
    if options.update and options.install_dir is None:
        raise ValueError('--update requires explicit --install-dir')
    if options.update and any(getattr(options, field) is not None for field in
                              ('ssh_host', 'profile', 'remote_codex_home', 'remote_codex_bin',
                               'local_codex_bin', 'port', 'agent', 'local_claude_bin',
                               'remote_claude_bin', 'remote_session_root',
                               'default_cwd', 'mcp_server_name')):
        raise ValueError('--update accepts only --install-dir; instance settings are preserved')
    agent = selected_agent(options)
    profile = options.profile or DEFAULT_PROFILES[agent]
    # Validate before using profile to form any directory path.
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,79}', profile):
        raise ValueError('invalid profile')
    target = options.install_dir or str(Path.home() / '.local/share/paseo-codex-remote-bridge' / profile)
    folder = Path(target).expanduser().absolute()
    if folder.is_symlink():
        raise ValueError('install directory must not be a symlink')
    folder = folder.resolve()
    if folder == REPOSITORY or REPOSITORY in folder.parents:
        raise ValueError('generated installations must be outside the repository')
    if options.update:
        agent = installed_agent(folder)
        if agent == 'claude':
            names = CLAUDE_RUNTIME + CLAUDE_INSTANCE
        elif agent == 'antigravity':
            names = ANTIGRAVITY_RUNTIME + ANTIGRAVITY_INSTANCE
        else:
            names = CODEX_RUNTIME + CODEX_INSTANCE
        # Inspect names and types. The agent key selects the file set; tokens stay unread.
        for name in names:
            path = folder / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('update requires a complete installation with regular files')
        files = runtime_files(folder, agent)
        for name, (data, mode) in files.items():
            # Replace code atomically, without following an existing destination symlink.
            fd, temporary = tempfile.mkstemp(prefix='.update-', dir=folder)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    os.fchmod(stream.fileno(), mode)
                    stream.write(data)
                os.replace(temporary, folder / name)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
    else:
        if agent == 'claude':
            if not options.ssh_host or not options.local_claude_bin:
                raise ValueError('new claude installations require --ssh-host and --local-claude-bin')
            if any(getattr(options, f) is not None for f in ('remote_codex_home', 'remote_codex_bin', 'local_codex_bin', 'port', 'default_cwd', 'mcp_server_name')):
                raise ValueError('incompatible options for claude installation')
        elif agent == 'antigravity':
            if not options.ssh_host:
                raise ValueError('new antigravity installations require --ssh-host')
            if any(getattr(options, f) is not None for f in ('remote_codex_home', 'remote_codex_bin', 'local_codex_bin', 'local_claude_bin', 'remote_claude_bin', 'remote_session_root')):
                raise ValueError('incompatible options for antigravity installation')
        else:
            if not options.ssh_host or not options.local_codex_bin:
                raise ValueError('new installations require --ssh-host and --local-codex-bin')
            if any(getattr(options, f) is not None for f in ('local_claude_bin', 'remote_claude_bin', 'remote_session_root', 'default_cwd', 'mcp_server_name')):
                raise ValueError('incompatible options for codex installation')
        files = runtime_files(folder, agent)
        files.update(instance_files(options, folder))
        # mkdir fails even for an empty existing directory; never overwrite implicitly.
        folder.mkdir(mode=0o700, parents=True, exist_ok=False)
        for name, (data, mode) in files.items():
            write_new(folder / name, data, mode)
    return folder


def main(argv=None):
    nl = chr(10)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh-host')
    parser.add_argument('--profile')
    parser.add_argument('--remote-codex-home')
    parser.add_argument('--remote-codex-bin')
    parser.add_argument('--local-codex-bin')
    parser.add_argument('--agent', choices=('codex', 'claude', 'antigravity'))
    parser.add_argument('--local-claude-bin')
    parser.add_argument('--remote-claude-bin')
    parser.add_argument('--remote-claude-config-dir', help='remote Claude account configuration directory')
    parser.add_argument('--route-paseo-mcp', action='store_true',
                        help='route Paseo MCP through the Mac on a daemon with Codex MCP environment support')
    parser.add_argument('--remote-session-root')
    parser.add_argument('--default-cwd')
    parser.add_argument('--mcp-server-name')
    parser.add_argument('--actions-file')
    parser.add_argument('--port', type=int)
    parser.add_argument('--install-dir')
    parser.add_argument('--update', action='store_true')
    options = parser.parse_args(argv)
    try:
        install(options)
    except (OSError, ValueError, TypeError):
        # No exception text: it could contain user-controlled paths/configuration.
        parser.exit(2, 'Installation refused: check options, destination, and existing files.' + nl)
    print('Runtime code updated.' if options.update else 'Installation files generated; no services activated.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
