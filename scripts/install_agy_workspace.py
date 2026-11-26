#!/usr/bin/env python3
"""Generate a Mac Paseo launcher and the Linux helper to copy to your SSH host."""
import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import sys
import tempfile

sys.dont_write_bytecode = True
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'src'))
from paseo_codex_remote_bridge.launcher import validate_host

SOURCE = REPO / 'src/paseo_codex_remote_bridge/agy_workspace'
RUNTIME = ('agy-workspace', 'agy_workspace.py', 'remote_workspace.py', 'workspace.sb')
INSTANCE = ('bridge.json', 'provider.json')


def executable(value):
    if not value or '\0' in value or value.startswith('-'):
        raise ValueError('invalid executable')
    if '/' in value and not value.startswith(('/', '~/')):
        raise ValueError('use an absolute path, ~/ path or command name')
    return value


def write_files(folder, files):
    for name, (data, mode) in files.items():
        with tempfile.NamedTemporaryFile(dir=folder, delete=False) as f:
            temp = Path(f.name)
            try:
                f.write(data)
                f.flush()
                os.fchmod(f.fileno(), mode)
                os.replace(temp, folder / name)
            finally:
                temp.unlink(missing_ok=True)


def install(options):
    destination = Path(options.install_dir).expanduser()
    if destination.is_symlink():
        raise ValueError('install directory cannot be a symlink')
    folder = destination.resolve()
    if folder == REPO or REPO in folder.parents:
        raise ValueError('choose an installation directory outside the checkout')

    # Use the same interpreter used for installation even when launchd has a short PATH.
    launcher = ('#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' ' +
                shlex.quote(str(folder / 'agy_workspace.py')) + ' "$@"\n')
    files = {name: ((SOURCE / name).read_bytes(), 0o644) for name in RUNTIME[1:]}
    files['agy-workspace'] = (launcher.encode(), 0o755)
    if options.update:
        if any((options.ssh_host, options.remote_agy_bin, options.remote_sshfs_bin, options.local_agy_bin)):
            raise ValueError('update accepts only the installation directory')
        for name in RUNTIME + INSTANCE:
            path = folder / name
            if path.is_symlink() or not path.is_file():
                raise ValueError('expected an existing complete workspace installation')
        if json.loads((folder / 'bridge.json').read_text())['agent'] != 'antigravity-workspace':
            raise ValueError('different installation type')
        write_files(folder, files)
        return

    config = {'version': 1, 'agent': 'antigravity-workspace', 'ssh_host': validate_host(options.ssh_host),
              'remote_agy_bin': executable(options.remote_agy_bin or '~/.local/bin/agy'),
              'remote_sshfs_bin': executable(options.remote_sshfs_bin or 'sshfs'),
              'local_agy_bin': os.path.expanduser(executable(options.local_agy_bin or 'agy'))}
    provider = {'agents': {'providers': {'antigravity': {'command': [str(folder / 'agy-workspace')]}}}}
    for name, data in [('bridge.json', config), ('provider.json', provider)]:
        files[name] = ((json.dumps(data, indent=2) + '\n').encode(), 0o600)
    folder.mkdir(parents=True, mode=0o700)
    try:
        write_files(folder, files)
    except BaseException:
        shutil.rmtree(folder)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-dir', required=True)
    parser.add_argument('--ssh-host', help='SSH alias of the Linux host')
    parser.add_argument('--remote-agy-bin', help='default: ~/.local/bin/agy on Linux')
    parser.add_argument('--remote-sshfs-bin', help='default: sshfs on Linux PATH')
    parser.add_argument('--local-agy-bin', help='default: agy on Mac PATH for existing local sessions')
    parser.add_argument('--update', action='store_true')
    opts = parser.parse_args()
    try:
        install(opts)
    except (OSError, ValueError, KeyError, TypeError):
        parser.exit(2, 'Installation refused: check arguments and installation directory.\n')
    print('Workspace files ready. Copy remote_workspace.py to the Linux host before using the launcher.')


if __name__ == '__main__':
    main()
