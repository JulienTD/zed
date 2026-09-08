"""Inspect Codex's kernel writer lock; stop only the owner the user confirmed."""
import contextlib
import json
import os
from pathlib import Path
import pwd
import signal
import socket
import sys
import time
import uuid


def kernel_locks():
    result = {}
    for line in Path('/proc/locks').read_text().splitlines():
        fields = line.split()
        if len(fields) < 8 or fields[1] not in ('FLOCK', 'POSIX') or fields[3] != 'WRITE':
            continue
        major, minor, inode = fields[5].split(':')
        result[(int(major, 16), int(minor, 16), int(inode))] = int(fields[4])
    return result


def file_identity(path):
    stat = path.stat()
    return (os.major(stat.st_dev), os.minor(stat.st_dev), stat.st_ino)


def inspect(session_id):
    session_id = str(uuid.UUID(session_id))
    result = dict(host=socket.gethostname(), user=pwd.getpwuid(os.getuid()).pw_name,
                  session_id=session_id, owner=None, reason=None)
    if sys.platform != 'linux':
        result['reason'] = 'Automatic takeover currently requires Linux. Close this conversation in its original Codex window, then retry.'
        return result
    directory = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()
    directory = directory.resolve() / 'thread-writer-locks'
    target = directory / (session_id + '.lock')
    if not target.exists():
        result['reason'] = 'No writer lock was found. The session may have closed; retry to resume it.'
        return result
    locks = kernel_locks()
    identity = file_identity(target)
    pid = locks.get(identity)
    if pid is None or pid <= 0:
        result['reason'] = 'No verifiable writer owns this conversation now. Retry to resume it.'
        return result
    process = Path('/proc') / str(pid)
    executable = os.readlink(process / 'exe')
    start_time = (process / 'stat').read_text().rsplit(')', 1)[1].split()[19]
    owner_uid = process.stat().st_uid
    owned_sessions = sorted(path.stem for path in directory.glob('*.lock')
                            if locks.get(file_identity(path)) == pid)
    result['owner'] = dict(pid=pid, uid=owner_uid, user=pwd.getpwuid(owner_uid).pw_name,
                           executable=executable, start_time=start_time,
                           boot_id=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
                           lock_identity=list(identity), directory=str(directory),
                           sessions=owned_sessions)
    if owner_uid != os.getuid():
        result['reason'] = 'The writer belongs to another user. Ask that user to close the session.'
    elif Path(executable).name != 'codex':
        result['reason'] = 'The lock owner could not be verified as a Codex executable.'
    elif owned_sessions != [session_id]:
        result['reason'] = ('This process owns %d conversations. Close this session in its original '
                            'Codex window so other conversations are not interrupted.' % len(owned_sessions))
    elif not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        result['reason'] = 'This host cannot safely target the existing process. Close the original session, then retry.'
    return result


def stop(session_id, expected):
    current = inspect(session_id)
    if current['reason'] or not expected or current['owner'] != expected:
        raise RuntimeError('The session owner changed or cannot be safely stopped. Retry to inspect it again.')
    # A pidfd cannot accidentally signal a replacement process if the PID is reused.
    with contextlib.closing(os.fdopen(os.pidfd_open(expected['pid']), 'rb', buffering=0)) as process:
        current = inspect(session_id)
        if current['reason'] or current['owner'] != expected:
            raise RuntimeError('The session owner changed. Nothing was stopped; retry to inspect it again.')
        signal.pidfd_send_signal(process.fileno(), signal.SIGTERM)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if kernel_locks().get(tuple(expected['lock_identity'])) != expected['pid']:
                return current
            time.sleep(0.1)
    raise RuntimeError('Codex was asked to stop but still holds the session. Wait for it to finish, then retry.')


def main():
    request = json.loads(sys.argv[1])
    if request['action'] == 'inspect':
        result = inspect(request['session_id'])
    elif request['action'] == 'stop':
        result = stop(request['session_id'], request['owner'])
    else:
        raise ValueError('Unknown writer operation')
    print(json.dumps(result))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
