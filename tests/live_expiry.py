"""Explicit native expiry test; affects only its own test notification/helper."""
import os
from pathlib import Path
import sys
import time
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
import install

identifier = 'native-notify-expiry-acceptance'
route = {'socket': os.environ['HERDR_SOCKET_PATH'], 'pane': os.environ['HERDR_PANE_ID'], 'occupant': 'test-only', 'surface': 'test-only'}


def send(seconds):
    return bridge.helper({'op': 'notify', 'id': identifier, 'title': 'Notification expiry self-test',
                          'body': 'Automatic test; no click needed.', 'duration_seconds': seconds, 'route': route})


try:
    old = send(2)
    time.sleep(1)
    newer = send(6)
    assert newer['generation'] != old['generation']
    time.sleep(2)
    assert identifier in bridge.helper({'op': 'list'})['ids'], 'old generation removed replacement'
    print('PASS: old timer cannot remove a newer notification with the same ID', flush=True)
    app = bridge.APPS / bridge.HELPER_APP
    executable = install.validate_app(app, 'blocked')
    install.stop_helpers([executable])
    bridge.helper({'op': 'status'})  # on-demand restart of this owned helper
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if identifier not in bridge.helper({'op': 'list'})['ids']:
            break
        time.sleep(0.1)
    else:
        raise AssertionError('expiry did not recover after helper restart')
    print('PASS: expiry recovers across actual helper termination/restart', flush=True)
finally:
    bridge.helper({'op': 'remove', 'ids': [identifier]})
