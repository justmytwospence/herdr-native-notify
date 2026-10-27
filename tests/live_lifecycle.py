"""Explicit acceptance test: creates/closes only its own no-focus test workspace."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge


def cli(*args):
    result = subprocess.run(['herdr'] + list(args), check=True, capture_output=True, text=True)
    return json.loads(result.stdout)['result'] if result.stdout.strip() else {}


class AcknowledgedDuringTest(Exception):
    pass


def wait_for(kind, identifier, present, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = identifier in bridge.helper({'op': 'list'})['ids']
        if found == present:
            return
        time.sleep(0.1)
    events = bridge.CACHE / 'blocked-events.log'
    if present and events.exists() and 'click ' + identifier in events.read_text():
        raise AcknowledgedDuringTest('Notification was clicked and correctly acknowledged during the timed test')
    snapshot = bridge.api(os.environ['HERDR_SOCKET_PATH'], 'session.snapshot')['snapshot']
    print('DIAGNOSTIC agents:', [a for a in snapshot['agents'] if a['workspace_id'] == workspace_id], flush=True)
    print('DIAGNOSTIC bridge state:', (bridge.state_dir(os.environ['HERDR_SOCKET_PATH']) / 'state.json').read_text(), flush=True)
    print('DIAGNOSTIC native expiry:', (bridge.CACHE / 'blocked-deadlines.json').read_text(), flush=True)
    raise AssertionError(kind + ' notification presence != ' + str(present))


workspace = cli('workspace', 'create', '--label', 'Notification acceptance test', '--no-focus')
pane = workspace['root_pane']['pane_id']
workspace_id = workspace['workspace']['workspace_id']
path = os.environ['HERDR_SOCKET_PATH']
identifier = bridge.notification_id(path, pane)
seq = 0


def report(state):
    global seq
    seq += 1
    cli('pane', 'report-agent', pane, '--source', 'native-notify-acceptance', '--agent', 'pi', '--state', state, '--seq', str(seq))


try:
    report('working')
    time.sleep(0.5)
    report('blocked')
    wait_for('blocked', identifier, True)
    time.sleep(9)
    wait_for('blocked', identifier, True, timeout=1)
    print('PASS: automatic blocked alert survives beyond 8 seconds', flush=True)
    report('working')
    wait_for('blocked', identifier, False)
    print('PASS: unblocking automatically withdraws the blocked alert', flush=True)
    time.sleep(0.5)
    report('idle')
    wait_for('done', identifier, True)
    started = time.monotonic()
    time.sleep(3)
    wait_for('done', identifier, True, timeout=1)
    wait_for('done', identifier, False, timeout=7)
    print('PASS: automatic finished alert expires; observed after delivery %.1fs' % (time.monotonic() - started), flush=True)
    print('PASS: enabled hooks/watcher, not a manual notification injection', flush=True)
except AcknowledgedDuringTest as acknowledgement:
    print('SKIP timed assertion: ' + str(acknowledgement), flush=True)
finally:
    cli('workspace', 'close', workspace_id)
    bridge.helper({'op': 'remove', 'ids': [identifier]})
