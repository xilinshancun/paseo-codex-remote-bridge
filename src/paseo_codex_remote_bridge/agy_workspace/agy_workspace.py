"""Mac Paseo launcher for a Linux agy with a per-session Mac project mount."""
import asyncio
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import uuid


HERE = Path(__file__).resolve().parent
SFTP = '/usr/libexec/sftp-server'
SANDBOX = '/usr/bin/sandbox-exec'
REMOTE = 'exec python3 "$HOME/.local/share/paseo-agy-workspace/remote_workspace.py" '


def remote(config, *args):
    host = config['ssh_host']
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@:\-]*', host):
        raise ValueError('invalid SSH host')
    return ['/usr/bin/ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15',
            '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
            host, REMOTE + shlex.join(args)]


def local_conversation(args):
    if '--conversation' not in args:
        return False
    ident = value_after(args, '--conversation')
    if not re.fullmatch(r'[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}', ident):
        return False
    history = Path.home() / '.gemini/antigravity-cli/conversations'
    return any((history / (ident + ext)).is_file() for ext in ('.db', '.pb'))


def value_after(args, key):
    if args.count(key) != 1 or args.index(key) + 1 >= len(args):
        raise ValueError('expected one ' + key + ' argument')
    return args[args.index(key) + 1]


def workspace(args):
    cwd = Path.cwd().resolve()
    if (Path(value_after(args, '--add-dir')).resolve() != cwd
            or cwd == Path.home() or cwd in Path.home().parents):
        raise ValueError('select a Mac project directory matching --add-dir')
    args[args.index('--add-dir') + 1] = str(cwd)
    return cwd


async def stop(child):
    if child.returncode is None:
        try:
            child.terminate()
            await asyncio.wait_for(child.wait(), 3)
        except asyncio.TimeoutError:
            child.kill()
            await child.wait()
        except ProcessLookupError:
            await child.wait()


async def transfer_images(text, session, temp, config):
    if not isinstance(text, str):
        return
    for line in text.splitlines():
        prefix = 'Attached image: '
        if not line.startswith(prefix):
            continue
        image = Path(line[len(prefix):])
        folder = image.parent
        if (folder.parent != temp or not re.fullmatch(r'paseo-antigravity-images-[A-Za-z0-9]+', folder.name)
                or not re.fullmatch(r'[0-9a-f-]{36}\.[A-Za-z0-9.+-]+', image.name)):
            continue
        directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('attachment directory must be private')
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(image.name, flags, dir_fd=directory)
            with os.fdopen(fd, 'rb') as src:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('expected a private attachment file')
                proc = await asyncio.create_subprocess_exec(
                    *remote(config, 'image', session, folder.name, image.name),
                    stdin=src, stdout=subprocess.DEVNULL)
                try:
                    if await proc.wait():
                        raise RuntimeError('could not transfer attachment')
                finally:
                    await stop(proc)
        finally:
            os.close(directory)


async def forward(child, session, temp, config):
    reader = asyncio.StreamReader(limit=32 * 1024 * 1024)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(os.dup(0), 'rb'))
    try:
        while True:
            line = await reader.readline()
            if not line:
                break
            frame = json.loads(line)
            message = frame.get('message') if isinstance(frame, dict) else None
            if isinstance(message, dict):
                await transfer_images(message.get('content'), session, temp, config)
            child.stdin.write(line)
            await child.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        transport.close()
        child.stdin.close()


def sftp_command(project):
    return [SANDBOX, '-D', 'PROJECT=' + str(project), '-f', str(HERE / 'workspace.sb'), SFTP]


async def run(config, args):
    project = workspace(args)
    session = uuid.uuid4().hex
    temp = Path(tempfile.gettempdir())
    children = []
    tasks = []
    owner = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, owner.cancel)
    try:
        left, right = socket.socketpair()
        with left, right:
            sftp = await asyncio.create_subprocess_exec(
                *sftp_command(project), '-d', str(project), '-P',
                'hardlink,home-directory,expand-path,users-groups-by-id', stdin=left, stdout=left)
            children.append(sftp)
            mount = await asyncio.create_subprocess_exec(
                *remote(config, 'mount', session, str(project), config['remote_sshfs_bin']),
                stdin=right, stdout=right)
            children.append(mount)
        process = await asyncio.create_subprocess_exec(
            *remote(config, 'run', session, str(project), str(temp), config['remote_agy_bin'], *args),
            stdin=asyncio.subprocess.PIPE)
        children.append(process)
        input_task = asyncio.create_task(forward(process, session, temp, config))
        exit_task = asyncio.create_task(process.wait())
        tasks.extend([input_task, exit_task])
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if input_task in done:
            input_task.result()
        status = await exit_task
        return status if status >= 0 else 128 - status
    except asyncio.CancelledError:
        return 143
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # End SFTP first so the passive sshfs sees EOF and can unmount on Linux.
        if children:
            await stop(children[0])
        if len(children) > 1:
            try:
                await asyncio.wait_for(children[1].wait(), 3)
            except asyncio.TimeoutError:
                pass
        for child in reversed(children[1:]):
            await stop(child)
        loop.remove_signal_handler(signal.SIGTERM)


def main(args):
    config = json.loads((HERE / 'bridge.json').read_text(encoding='utf-8'))
    if config['agent'] != 'antigravity-workspace':
        raise ValueError('expected an Antigravity workspace installation')
    if args == ['--bridge-check']:
        subprocess.run([*sftp_command(Path.cwd().resolve()), '-Q', 'requests'],
                       stdout=subprocess.DEVNULL, check=True)
        command = remote(config, 'check', config['remote_agy_bin'], config['remote_sshfs_bin'])
    elif '--input-format' not in args:
        command = remote(config, 'probe', config['remote_agy_bin'], *args)
    elif local_conversation(args):
        binary = shutil.which(config['local_agy_bin'])
        if not binary or Path(binary).resolve() == (HERE / 'agy-workspace').resolve():
            raise ValueError('local agy is unavailable for this Mac conversation')
        command = [binary, *args]
    else:
        return asyncio.run(run(config, args))
    os.execv(command[0], command)


if __name__ == '__main__':
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        print('Antigravity workspace:', error, file=sys.stderr)
        sys.exit(1)
