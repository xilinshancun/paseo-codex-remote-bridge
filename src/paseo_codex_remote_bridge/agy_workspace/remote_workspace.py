#!/usr/bin/env python3
"""Linux side of the session-scoped Mac workspace mount. Run over SSH stdio."""
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time


RUNTIME = Path.home() / '.cache/paseo-agy-workspace'
IMAGE_FOLDER = re.compile(r'paseo-antigravity-images-[A-Za-z0-9]+')
IMAGE_FILE = re.compile(r'[0-9a-f-]{36}\.[A-Za-z0-9.+-]+')


def binary(name):
    path = shutil.which(os.path.expanduser(name))
    if not path:
        raise ValueError('program unavailable: ' + name)
    return path


def sshfs_stdio_mode(sshfs):
    help_text = subprocess.run([sshfs, '-h'], stdout=subprocess.PIPE, stderr=subprocess.STDOUT).stdout
    for option in ('passive', 'slave'):
        if re.search(rb'\b' + option.encode() + rb'\b', help_text):
            return option
    raise RuntimeError('sshfs needs stdio support (-o passive or -o slave)')


def check(agy, sshfs):
    if not sys.platform.startswith('linux') or sys.version_info < (3, 9):
        raise RuntimeError('Linux with Python 3.9+ is required')
    if not os.access('/dev/fuse', os.R_OK | os.W_OK):
        raise RuntimeError('the SSH user needs access to /dev/fuse')
    binary(agy)
    binary('fusermount3')
    sshfs_stdio_mode(binary(sshfs))
    subprocess.run([binary('bwrap'), '--die-with-parent', '--unshare-pid',
                    '--ro-bind', '/', '/', '--proc', '/proc', '--', '/bin/true'], check=True)
    print('Linux workspace dependencies ready')


def mount_workspace(session, project, sshfs):
    session.mkdir(mode=0o700)
    mountpoint = session / 'project'
    images = session / 'images'
    try:
        mountpoint.mkdir(mode=0o700)
        images.mkdir(mode=0o700)
        sshfs = binary(sshfs)
        mode = sshfs_stdio_mode(sshfs)
        return subprocess.call([sshfs, ':' + project, str(mountpoint), '-f', '-o',
                                mode + ',auto_unmount,idmap=user'])
    finally:
        if os.path.ismount(mountpoint):
            subprocess.run(['fusermount3', '-uz', str(mountpoint)], check=False)
        if not os.path.ismount(mountpoint):
            # Never descend into the mountpoint, including on an unmount failure.
            if mountpoint.exists():
                mountpoint.rmdir()
            if images.exists():
                shutil.rmtree(images)
            session.rmdir()


def wait_until(ready, description):
    deadline = time.monotonic() + 20
    while not ready():
        if time.monotonic() >= deadline:
            raise RuntimeError('timed out waiting for ' + description)
        time.sleep(.1)


def run_session(session, project, mac_temp, agy, args):
    mountpoint = session / 'project'
    wait_until(lambda: os.path.ismount(mountpoint), 'Mac project mount')
    cmd = [binary('bwrap'), '--die-with-parent', '--unshare-pid']
    for name in ('usr', 'etc', 'bin', 'sbin', 'lib', 'lib64', 'sys', 'opt'):
        path = '/' + name
        if os.path.islink(path):
            cmd.extend(['--symlink', os.readlink(path), path])
        elif os.path.isdir(path):
            cmd.extend(['--ro-bind', path, path])
    home = str(Path.home())
    cmd.extend(['--dev', '/dev', '--proc', '/proc', '--bind', '/run', '/run',
                '--bind', '/tmp', '/tmp', '--bind', home, home,
                '--bind', str(session / 'images'), mac_temp, '--bind', str(mountpoint), project,
                '--tmpfs', str(RUNTIME), '--chdir', project, '--', binary(agy), *args])
    os.execv(cmd[0], cmd)


def receive_image(session, folder, filename):
    if not IMAGE_FOLDER.fullmatch(folder) or not IMAGE_FILE.fullmatch(filename):
        raise ValueError('invalid Paseo attachment name')
    images = session / 'images'
    wait_until(images.is_dir, 'session attachment directory')
    destination = images / folder
    destination.mkdir(mode=0o700, exist_ok=True)
    # The agent can write to its attachment directory. Open it without following links.
    directory = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = '.upload-' + os.urandom(16).hex()
    try:
        fd = os.open(name, os.O_WRONLY | os.O_EXCL | os.O_CREAT, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'wb') as output:
            shutil.copyfileobj(sys.stdin.buffer, output)
        os.replace(name, filename, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def main(args):
    action, *params = args
    if action == 'check':
        return check(*params)
    if action == 'probe':
        agy, *options = params
        os.execv(binary(agy), [agy, *options])
    ident, *params = params
    if not re.fullmatch('[a-f0-9]{32}', ident):
        raise ValueError('invalid workspace session id')
    RUNTIME.mkdir(parents=True, exist_ok=True, mode=0o700)
    entry = RUNTIME.lstat()
    if not stat.S_ISDIR(entry.st_mode) or entry.st_uid != os.getuid() or entry.st_mode & 0o077:
        raise ValueError('workspace cache must be a private directory owned by the SSH user')
    session = RUNTIME / ident
    if action == 'mount':
        return mount_workspace(session, *params)
    if action == 'image':
        return receive_image(session, *params)
    if action == 'run':
        project, mac_temp, agy, *options = params
        return run_session(session, project, mac_temp, agy, options)
    raise ValueError('unknown workspace operation')


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except (OSError, TypeError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('Antigravity workspace:', error, file=sys.stderr)
        sys.exit(1)
