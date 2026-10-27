"""Explicit acceptance test: a blocked alert in a hidden space stays until unblocked.

Creates its own Herdr space without focusing it (so the user is never on it),
reports the pane blocked, and checks Notification Center every 10 seconds for
PERSIST_SECONDS (default 180). Then unblocks it and checks the alert is withdrawn.
Closes only its own space and removes only its own alert.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge

SECONDS = int(os.environ.get('PERSIST_SECONDS', '180'))


def cli(*args):
    result = subprocess.run(['herdr'] + list(args), check=True, capture_output=True, text=True)
    return json.loads(result.stdout)['result'] if result.stdout.strip() else {}


path = os.environ.get('HERDR_SOCKET_PATH', bridge.LOCAL_SOCKET)
workspace = cli('workspace', 'create', '--label', 'notify-persist-test', '--no-focus')
pane = workspace['root_pane']['pane_id']
workspace_id = workspace['workspace']['workspace_id']
identifier = bridge.notification_id(path, pane)


def report(state, seq):
    cli('pane', 'report-agent', pane, '--source', 'native-notify-persistence', '--agent', 'pi',
        '--state', state, '--seq', str(seq))


def present():
    return identifier in bridge.helper({'op': 'list'})['ids']


try:
    report('working', 1)
    time.sleep(0.5)
    report('blocked', 2)
    deadline = time.monotonic() + 10
    while not present():
        if time.monotonic() > deadline:
            raise AssertionError('blocked alert was never delivered')
        time.sleep(0.2)
    started = time.monotonic()
    print('blocked alert delivered; watching for %d s' % SECONDS, flush=True)
    while time.monotonic() - started < SECONDS:
        time.sleep(10)
        elapsed = time.monotonic() - started
        if not present():
            raise AssertionError('FAIL: blocked alert disappeared after %.0f s while still blocked' % elapsed)
        print('  %3.0f s: still present' % elapsed, flush=True)
    print('PASS: blocked alert in a hidden space stayed %d s while blocked' % SECONDS, flush=True)
    report('working', 3)
    deadline = time.monotonic() + 8
    while present():
        if time.monotonic() > deadline:
            raise AssertionError('FAIL: unblocking did not withdraw the alert')
        time.sleep(0.2)
    print('PASS: unblocking withdrew it', flush=True)
finally:
    cli('workspace', 'close', workspace_id)
    bridge.helper({'op': 'remove', 'ids': [identifier]})
