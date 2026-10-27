"""Private, window-owned SSH connections; callbacks own notification routing.

on_ready must validate the forwarded public API before returning. Transient
OSError/ConnectionError failures are retried for 20 seconds, while SSH is alive.
No route is considered ready merely because SSH started or a socket exists.
"""
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

SSH = '/usr/bin/ssh'
HANDSHAKE_TIMEOUT = 30
READY_TIMEOUT = 20
DISCOVERY_COMMAND = 'printf "%s\\n" "$HOME"; command -v herdr'
NEW_WINDOW = '''on run argv
    tell application "Ghostty"
        set config to new surface configuration
        set command of config to item 1 of argv
        set wait after command of config to true
        set createdWindow to new window with configuration config
        return id of first terminal of createdWindow
    end tell
end run'''


def _validate(host, session):
    part = r'[A-Za-z0-9_][A-Za-z0-9_.-]*'
    if not isinstance(host, str) or not re.fullmatch(part + r'(?:@' + part + r')?', host):
        raise ValueError('Use an SSH alias or user@host, not flags or a command')
    if not isinstance(session, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]*', session):
        raise ValueError('Use a simple Herdr session name')


def _atomic(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _status(request, status):
    request['status'] = status
    _atomic(request['request_file'], request)


def open_connection(host, session, bridge_script, cache):
    """Launch a NEW window and atomically hand its exact terminal UUID to child."""
    _validate(host, session)
    directory = Path(tempfile.mkdtemp(prefix='connection-', dir=_cache(cache)))
    # Darwin's default TMPDIR can exceed sockaddr_un; /tmp is deliberately short.
    sockets = Path(tempfile.mkdtemp(prefix='hn-', dir='/tmp'))
    request = dict(host=host, session=session, connection_token=uuid.uuid4().hex,
                   request_file=str(directory / 'request.json'), surface=None,
                   socket=str(sockets / 'api'), control_socket=str(sockets / 'ssh'),
                   socket_dir=str(sockets), status='opening')
    try:
        for key in ('socket', 'control_socket'):
            if len(os.fsencode(request[key])) > 103:
                raise ValueError('Unix socket path exceeds Darwin limit')
        _atomic(request['request_file'], request)
        command = shlex.join([sys.executable, str(Path(bridge_script).resolve()),
                              'connect-run', request['request_file']])
        result = subprocess.run(['/usr/bin/osascript', '-e', NEW_WINDOW, command],
                                check=True, stdout=subprocess.PIPE, text=True,
                                timeout=HANDSHAKE_TIMEOUT)
        surface = result.stdout.strip()
        uuid.UUID(surface)  # Validate, but preserve Ghostty's exact identifier text.
        request['surface'] = surface
        _status(request, 'connecting')
        return request
    except BaseException:
        # Never close a guessed/frontmost terminal. A launched child sees error
        # (or times out), exits, and leaves its wait-after-command error visible.
        _status(request, 'error')
        shutil.rmtree(sockets, ignore_errors=True)
        raise


def _cache(cache):
    path = Path(cache).expanduser().resolve()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def _handshake(request_file):
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT
    while True:
        request = json.loads(Path(request_file).read_text())
        if request['status'] in ('error', 'closed'):
            raise RuntimeError('Ghostty window handshake failed')
        if request.get('surface'):
            uuid.UUID(request['surface'])
            _validate(request['host'], request['session'])
            return request
        if time.monotonic() >= deadline:
            _status(request, 'error')
            raise RuntimeError('Timed out waiting for exact Ghostty terminal UUID')
        time.sleep(0.1)


def _ssh(request):
    # No -F override, BatchMode, or ExitOnForwardFailure: normal user config
    # and terminal authentication apply, including unrelated RemoteForwards.
    return [SSH, '-o', 'ConnectTimeout=10', '-o', 'ControlMaster=auto', '-o', 'ControlPersist=60',
            '-S', request['control_socket'], '-o', 'ServerAliveInterval=15',
            '-o', 'ServerAliveCountMax=3']


def _stop(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _exit_master(request):
    try:
        subprocess.run([SSH, '-S', request['control_socket'], '-O', 'exit', request['host']],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _remote_paths(output, session):
    lines = output.splitlines()
    if len(lines) != 2 or any(not p.startswith('/') or any(c in p for c in '\r\n\x00:') for p in lines):
        raise ValueError('Remote HOME and herdr must be absolute paths')
    home, binary = lines
    remote = home.rstrip('/') + '/.config/herdr/'
    remote += 'herdr.sock' if session == 'default' else 'sessions/' + session + '/herdr.sock'
    return remote, binary


def reap_abandoned(binding, cache):
    """Recover terminal SIGKILL cleanup using only a validated owned request."""
    filename = binding.get('request_file')
    if not filename:
        return
    path = Path(filename)
    root = Path(cache).resolve()
    if path.is_symlink() or path.name != 'request.json' or path.parent.parent.resolve() != root or not path.parent.name.startswith('connection-'):
        raise RuntimeError('Refusing cleanup outside owned connection cache')
    if not path.exists():
        return
    request = json.loads(path.read_text())
    if request.get('connection_token') != binding.get('connection_token') or request.get('owner_pid') != binding.get('owner_pid'):
        return
    _validate(request['host'], request['session'])
    sockets = Path(request['socket_dir'])
    if sockets.is_symlink() or sockets.parent.resolve() != Path('/tmp').resolve() or not sockets.name.startswith('hn-'):
        raise RuntimeError('Refusing cleanup outside owned socket directory')
    if sockets.exists() and (sockets.stat().st_uid != os.getuid() or sockets.stat().st_mode & 0o777 != 0o700):
        raise RuntimeError('Refusing cleanup of foreign socket directory')
    for key in ('socket', 'control_socket'):
        if Path(request[key]).parent != sockets:
            raise RuntimeError('Refusing cleanup of foreign SSH transport')
    _status(request, 'closed')
    try:
        _exit_master(request)
    finally:
        shutil.rmtree(sockets, ignore_errors=True)


class _Detached(Exception):
    pass


def run_connection(request_file, on_ready, on_close):
    """Run in the dedicated terminal; return SSH exit code (0 on detach).

    on_close(binding) is called once in finally, including failed API validation.
    Request status is durable; errors never persist authentication output.
    """
    request = None
    binding = None
    process = None
    handlers = {}
    status = 'error'

    def detach(signum, frame):
        raise _Detached()

    try:
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, detach)
        request = json.loads(Path(request_file).read_text())
        request = _handshake(request_file)
        request['owner_pid'] = os.getpid()
        _status(request, 'connecting')
        while True:
            process = subprocess.Popen(_ssh(request) + [request['host'], DISCOVERY_COMMAND],
                                       stdout=subprocess.PIPE, text=True)
            output, _ = process.communicate()
            code = process.returncode
            process = None
            if code == 255:
                _status(request, 'reconnecting')
                _exit_master(request)
                time.sleep(3)
                continue
            if code:
                raise RuntimeError('SSH remote discovery failed')
            remote_socket, binary = _remote_paths(output, request['session'])
            binding = dict(surface=request['surface'], socket=request['socket'],
                           host=request['host'], session=request['session'],
                           remote_socket=remote_socket,
                           label=request['host'] + '/' + request['session'],
                           connection_token=request['connection_token'],
                           request_file=request['request_file'],
                           owner_pid=os.getpid(), managed_connection=True)
            process = subprocess.Popen(_ssh(request) + [
                '-tt', '-o', 'StreamLocalBindMask=0177', '-o', 'StreamLocalBindUnlink=yes',
                '-L', request['socket'] + ':' + remote_socket, request['host'],
                shlex.quote(binary) + ' --session ' + shlex.quote(request['session'])])
            deadline = time.monotonic() + READY_TIMEOUT
            while process.poll() is None:
                try:
                    on_ready(binding)
                    if process.poll() is None:
                        _status(request, 'ready')
                    break
                except (OSError, ConnectionError):
                    if process.poll() is not None:
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError('Forwarded Herdr API did not become ready') from None
                    time.sleep(0.2)
            code = process.wait()
            process = None
            if code != 255:
                status = 'closed' if code == 0 else 'error'
                return code
            _status(request, 'reconnecting')
            _exit_master(request)
            time.sleep(3)
    except (_Detached, KeyboardInterrupt):
        status = 'closed'
        return 0
    finally:
        # Terminal closure may send TERM and HUP together. A second signal must
        # not interrupt cleanup before the durable closed status is recorded.
        for sig in handlers:
            signal.signal(sig, signal.SIG_IGN)
        try:
            try:
                _stop(process)
            finally:
                if request is not None:
                    _exit_master(request)
                    try:
                        _status(request, status)
                    finally:
                        shutil.rmtree(request['socket_dir'], ignore_errors=True)
        finally:
            try:
                if binding is not None:
                    on_close(binding)
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
