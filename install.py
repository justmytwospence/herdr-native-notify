#!/usr/bin/env python3
"""Install once, or explicitly update our signed helpers without changing prefs."""
import argparse
import ctypes
import os
from pathlib import Path
import plistlib
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
DESTINATION = Path.home() / 'Applications/Herdr Native Notify'
# One helper sends every alert (one sender, so alerts stack per Herdr space); it
# keeps the original blocked identity, so its permission and Persistent style
# carry over. Expiry is per alert, not per app.
APPS = (('blocked', 'Herdr Blocked'),)
# The former finished-alert helper: validated, stopped and removed on install.
RETIRED = (('done', 'Herdr Finished'),)
# A new identity (was com.justmytwospence.herdr-native-notify.): Notification
# Center kept a stale, blank icon for the old one that no cache reset cleared.
PREFIX = 'com.justmytwospence.herdr-notify.'
LEGACY = 'Herdr Needs Input.app'


def validate_app(app, kind):
    """Fail closed on foreign, unsigned, damaged, or symlinked helper bundles."""
    app = Path(app)
    if kind not in dict(APPS + RETIRED):
        raise ValueError('Unknown helper kind: ' + kind)
    if app.is_symlink() or not app.is_dir():
        raise RuntimeError('Not a real helper bundle: ' + str(app))
    for directory, dirs, files in os.walk(app, followlinks=False):
        for path in [Path(directory)] + [Path(directory) / p for p in dirs + files]:
            metadata = path.lstat()
            if metadata.st_uid != os.getuid() or not (
                    stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
                raise RuntimeError('Unowned or unsafe bundle entry: ' + str(path))
    with (app / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    expected = {'CFBundleIdentifier': PREFIX + kind,
                'CFBundleExecutable': 'Helper', 'CFBundlePackageType': 'APPL',
                'HerdrNotificationKind': kind}
    if any(info.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Unexpected helper identity: ' + str(app))
    executable = app / 'Contents/MacOS/Helper'
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError('Missing executable helper: ' + str(app))
    if not (app / 'Contents/Resources/bridge.py').is_file():
        raise RuntimeError('Missing focus bridge: ' + str(app))
    subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)], check=True)
    signature = subprocess.run(['/usr/bin/codesign', '--display', '--verbose=4', str(app)],
                               check=True, capture_output=True, text=True)
    identifiers = [line.partition('=')[2] for line in
                   (signature.stdout + '\n' + signature.stderr).splitlines()
                   if line.startswith('Identifier=')]
    if identifiers != [PREFIX + kind]:
        raise RuntimeError('Unexpected signing identity: ' + str(app))
    return executable.resolve()


def bundle_stamp(app):
    metadata = Path(app).lstat()
    return metadata.st_dev, metadata.st_ino


def rename_bundle(source, destination, exchange=False):
    """Same-filesystem atomic swap, or exclusive rename (never clobber a race)."""
    libc = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    rename = libc.renamex_np
    rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    # Darwin: RENAME_SWAP=2, RENAME_EXCL=4. No non-atomic replacement fallback.
    if rename(os.fsencode(source), os.fsencode(destination), 2 if exchange else 4):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(destination))


def pid_path_reader():
    """Load libproc lazily: importing this module is safe on any test host."""
    libproc = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    proc_pidpath = libproc.proc_pidpath
    proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc_pidpath.restype = ctypes.c_int

    def path(pid):
        buffer = ctypes.create_string_buffer(4096)
        if proc_pidpath(pid, buffer, len(buffer)) <= 0:
            return None
        return os.fsdecode(buffer.value)
    return path


def stop_helpers(executables, timeout=5):
    """Never use argv/command substrings to decide which processes to signal."""
    read_path = pid_path_reader()  # Fail before signaling if libproc is unavailable.
    owned = {str(Path(path).resolve()) for path in executables}
    pids = subprocess.check_output(['/bin/ps', '-axo', 'pid='], text=True)
    stopped = {}
    for token in pids.split():
        pid = int(token)
        path = read_path(pid)
        if pid == os.getpid() or path not in owned:
            continue
        # Recheck immediately before signaling to reduce PID-reuse races.
        if read_path(pid) != path:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped[pid] = path
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + timeout
    while stopped:
        stopped = {pid: path for pid, path in stopped.items() if read_path(pid) == path}
        if not stopped:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError('Owned helpers did not stop; refusing to replace bundles')
        time.sleep(0.05)


def launch_helpers(apps):
    errors = []
    for app in apps:
        try:
            subprocess.run(['/usr/bin/open', '-g', str(app)], check=True)
        except Exception as error:
            errors.append(str(app) + ': ' + str(error))
    if errors:
        raise RuntimeError('Helper launch failed: ' + '; '.join(errors))


def install(update=False, rename_blocked=False, source_root=None, destination=None):
    source_root = Path(source_root) if source_root is not None else ROOT / 'build'
    destination = Path(destination) if destination is not None else DESTINATION
    if update and rename_blocked:
        raise ValueError('Choose --update or --rename-blocked, not both')
    specs = APPS[:1] if rename_blocked else APPS
    plans = []
    # Validate every source and existing target before staging, signaling, or renaming.
    if destination.is_symlink() or (destination.exists() and not destination.is_dir()):
        raise RuntimeError('Unsafe installation directory: ' + str(destination))
    if destination.exists() and destination.stat().st_uid != os.getuid():
        raise RuntimeError('Unowned installation directory: ' + str(destination))
    for kind, name in specs:
        source = source_root / (name + '.app')
        target = destination / (name + '.app')
        current = destination / LEGACY if rename_blocked else target
        validate_app(source, kind)
        if update or rename_blocked:
            validate_app(current, kind)
        elif os.path.lexists(target):
            raise RuntimeError('Refusing to overwrite: ' + str(target))
        if rename_blocked and os.path.lexists(target):
            raise RuntimeError('Refusing to overwrite: ' + str(target))
        plans.append((kind, source, target, current))
    # Do not allow a second blocked app with the same notification identity.
    if not rename_blocked and os.path.lexists(destination / LEGACY):
        raise RuntimeError('Run --rename-blocked first; legacy blocked app still exists')

    destination.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.herdr-stage-', dir=destination))
    safe_cleanup = True
    try:
        stamps = {}
        for kind, source, target, current in plans:
            staged = stage / target.name
            shutil.copytree(source, staged, symlinks=True)
            validate_app(staged, kind)
            stamps[staged] = bundle_stamp(staged)
            if update or rename_blocked:
                stamps[current] = bundle_stamp(current)
        completed = []
        renamed = False
        try:
            if update or rename_blocked:
                executables = [validate_app(current, kind) for kind, _, _, current in plans]
                stop_helpers(executables)
                for kind, _, _, current in plans:
                    validate_app(current, kind)
                    if bundle_stamp(current) != stamps[current]:
                        raise RuntimeError('Helper changed during update: ' + str(current))
            if rename_blocked:
                rename_bundle(plans[0][3], plans[0][2])
                renamed = True
                safe_cleanup = False
            for kind, _, target, current in plans:
                staged = stage / target.name
                replacing = update or rename_blocked
                if replacing:
                    validate_app(target, kind)
                    if bundle_stamp(target) != stamps[current]:
                        raise RuntimeError('Helper changed during update: ' + str(target))
                rename_bundle(staged, target, exchange=replacing)
                safe_cleanup = False
                completed.append((kind, staged, target, replacing))
                validate_app(target, kind)
            for kind, staged, _, replacing in completed:
                if replacing:
                    validate_app(staged, kind)
        except Exception:
            # Keep backups if rollback fails. Never delete the only surviving old app.
            for kind, staged, target, replacing in reversed(completed):
                if bundle_stamp(target) != stamps[staged]:
                    raise RuntimeError('Helper changed before rollback; backups retained in ' + str(stage))
                if replacing:
                    validate_app(staged, kind)
                rename_bundle(target, staged, exchange=replacing)
            if renamed:
                rename_bundle(plans[0][2], plans[0][3])
            safe_cleanup = True
            if update or rename_blocked:
                for kind, _, _, current in plans:
                    validate_app(current, kind)
                launch_helpers([current for _, _, _, current in plans])
            raise
        safe_cleanup = True
        # Initial setup and explicit updates keep both long-lived helpers running.
        # Authorization/presentation remain user-selected, never preference edits.
        launch_helpers([target for _, _, target, _ in plans])
        for _, _, target, _ in plans:
            print(target)
        retire(destination)
    finally:
        if safe_cleanup:
            shutil.rmtree(stage)
        else:
            print('Recovery bundles retained in ' + str(stage), file=sys.stderr)


def retire(destination):
    """Stop and remove retired helpers we own; leave anything unexpected alone."""
    for kind, name in RETIRED:
        app = Path(destination) / (name + '.app')
        if not os.path.lexists(app):
            continue
        executable = validate_app(app, kind)
        stop_helpers([executable])
        validate_app(app, kind)
        shutil.rmtree(app)
        print('Removed retired ' + str(app))


def pause_watcher():
    """Stop only our validated launchd watcher while replacing its helper resources."""
    label = 'com.justmytwospence.herdr-native-notify'
    service = 'gui/' + str(os.getuid()) + '/' + label
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    if not plist.exists():
        return None
    with plist.open('rb') as stream:
        settings = plistlib.load(stream)
    expected = ['/usr/bin/python3', str(DESTINATION / 'Herdr Blocked.app/Contents/Resources/bridge.py'), 'watch']
    if settings.get('Label') != label or settings.get('ProgramArguments') != expected:
        raise RuntimeError('Refusing to stop an unexpected watcher configuration')
    def registered():
        return subprocess.run(['launchctl', 'print', service], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if not registered():
        return None
    subprocess.run(['launchctl', 'bootout', service], check=True)
    deadline = time.monotonic() + 30
    while registered():
        if time.monotonic() >= deadline:
            raise RuntimeError('Watcher did not stop; no app update attempted')
        time.sleep(0.1)
    return plist


def installed(destination=None):
    """Whether every current helper is already installed (so --ensure updates it)."""
    destination = Path(destination) if destination is not None else DESTINATION
    return all(os.path.lexists(destination / (name + '.app')) for _, name in APPS)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--update', action='store_true', help='atomically replace owned helpers and restart both')
    mode.add_argument('--rename-blocked', action='store_true', help='migrate the old blocked app name, preserving its ID')
    mode.add_argument('--ensure', action='store_true',
                      help='install, or update an existing install (the herdr plugin build step)')
    args = parser.parse_args(argv)
    if args.ensure:
        args.update = installed()
    watcher = pause_watcher() if args.update or args.rename_blocked else None
    try:
        install(update=args.update, rename_blocked=args.rename_blocked)
    finally:
        if watcher is not None:
            subprocess.run(['launchctl', 'bootstrap', 'gui/' + str(os.getuid()), str(watcher)], check=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('herdr-native-notify: ' + str(error), file=sys.stderr)
        sys.exit(1)
