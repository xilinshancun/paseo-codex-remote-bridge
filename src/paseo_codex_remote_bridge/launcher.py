"""Launch remote Codex; keep image conversion scoped to Paseo app-server."""
import json
import os
from pathlib import Path
import re
import shlex
import sys


def validate_host(host):
    if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:\-]*', host):
        raise ValueError('ssh host must be a hostname or SSH alias, without options')
    return host


def remote_path(path):
    """Expand only ~/ using the remote shell's HOME; quote all other bytes."""
    if not isinstance(path, str) or '\x00' in path:
        raise ValueError('invalid remote path')
    if path == '~':
        return '"$HOME"'
    if path.startswith('~/'):
        return '"$HOME"/' + shlex.quote(path[2:])
    if path.startswith('/'):
        return shlex.quote(path)
    raise ValueError('remote paths must be absolute or begin with ~/')


def app_server_input(args):
    # Same scope as the original bridge: no global-option or exec --image scan.
    return bool(args) and args[0] == 'app-server' and (len(args) == 1 or args[1].startswith('--'))


def ssh_command(config, args):
    host = validate_host(config['ssh_host'])
    home = remote_path(config['remote_codex_home'])
    binary = remote_path(config['remote_codex_bin'])
    command = 'export PATH="$HOME/.local/bin:$PATH"; export CODEX_HOME=' + home
    command += '; exec ' + binary
    if args:
        command += ' ' + shlex.join(args)
    return ['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=20',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', host, command]


def main():
    try:
        folder = Path(__file__).resolve().parent
        config = json.loads((folder / 'bridge.json').read_text(encoding='utf-8'))
        args = sys.argv[1:]
        command = ssh_command(config, args)
        if app_server_input(args):
            command = [sys.executable, str(folder / 'image_bridge.py')] + command
        os.execv(command[0], command)
    except (OSError, ValueError, KeyError, TypeError):
        # Avoid logging config, arguments, or remote stderr content locally.
        os.write(2, b'codex remote bridge: invalid configuration or launcher unavailable\n')
        return 127


if __name__ == '__main__':
    sys.exit(main())
