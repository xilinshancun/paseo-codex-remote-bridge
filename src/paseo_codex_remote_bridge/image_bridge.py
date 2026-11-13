#!/usr/bin/python3
"""LF-framed app-server stdin bridge. Child stdout/stderr remain inherited."""
import base64
import json
import os
import signal
import subprocess
import sys
import threading


def image_url(path):
    with open(path, 'rb') as stream:
        data = stream.read()
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        mime = 'image/png'
    elif data.startswith(b'\xff\xd8\xff'):
        mime = 'image/jpeg'
    elif data.startswith((b'GIF87a', b'GIF89a')):
        mime = 'image/gif'
    elif data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        mime = 'image/webp'
    else:
        raise ValueError('unsupported image bytes')
    return 'data:' + mime + ';base64,' + base64.b64encode(data).decode('ascii')


def transform(line, warn):
    try:
        message = json.loads(line)
    except (ValueError, UnicodeError):
        return line
    if not isinstance(message, dict) or message.get('method') not in ('turn/start', 'turn/steer'):
        return line
    params = message.get('params')
    if not isinstance(params, dict) or not isinstance(params.get('input'), list):
        return line
    changed = False
    for index, item in enumerate(params['input']):
        if not isinstance(item, dict) or item.get('type') != 'localImage' or not isinstance(item.get('path'), str):
            continue
        try:
            url = image_url(item['path'])
        except (OSError, ValueError):
            # Preserve native handling, including paths that exist only remotely.
            warn()
            continue
        replacement = dict(item)
        del replacement['path']
        replacement.update(type='image', url=url)
        params['input'][index] = replacement
        changed = True
    if not changed:
        return line
    ending = b'\r\n' if line.endswith(b'\r\n') else b'\n' if line.endswith(b'\n') else b''
    return json.dumps(message, ensure_ascii=True, separators=(',', ':')).encode('utf-8') + ending


def relay(command):
    try:
        child = subprocess.Popen(command, stdin=subprocess.PIPE, bufsize=0)
    except OSError:
        os.write(2, b'codex-e image bridge: child launch failed\n')
        return 127

    def forward(signum, _frame):
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)

    def pump():
        def warn():
            os.write(2, b'codex-e image bridge: image unavailable locally; preserving native input\n')

        def send(frame):
            data = memoryview(transform(frame, warn))
            while data:
                count = os.write(child.stdin.fileno(), data)
                data = data[count:]

        pending = b''
        try:
            while True:
                chunk = os.read(0, 65536)
                if not chunk:
                    if pending:
                        send(pending)
                    break
                pending += chunk
                while b'\n' in pending:
                    frame, pending = pending.split(b'\n', 1)
                    send(frame + b'\n')
        except (BrokenPipeError, OSError):
            # Child may close stdin before it exits; main still waits for its status.
            pass
        finally:
            child.stdin.close()

    threading.Thread(target=pump, daemon=True).start()
    code = child.wait()
    return code if code >= 0 else 128 - code


if __name__ == '__main__':
    if len(sys.argv) < 2:
        sys.exit(127)
    sys.exit(relay(sys.argv[1:]))
