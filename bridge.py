#!/usr/bin/env python3
"""User-local Herdr notifications. Supported APIs only; no terminal keystrokes."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time

CACHE = Path.home() / 'Library/Caches/herdr-native-notify'
APPS = Path.home() / 'Applications/Herdr Native Notify'
CONFIG = Path.home() / '.config/herdr/plugins/config/herdr-native-notify/config.json'
LOCAL_SOCKET = str(Path.home() / '.config/herdr/herdr.sock')

# Cross-machine click navigation (see navigate()). The key must be bound as an
# extra keys.next_agent binding in Herdr's config. A function key with every
# modifier collides with nothing (Ghostty before 1.4 could only send function
# keys by script; 1.4 sends any key, but a reserved key is still the point).
NAV_SOURCE = 'herdr-native-notify'
NAV_TOKEN = 'native_notify_target'
NAV_KEY = ('f12', 'control,option,shift')
NAV_SETTLE = 0.5
NAV_VERIFY = 2.5


def rpc(path, payload, timeout=10):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(path))
        sock.sendall(json.dumps(payload).encode() + b'\n')
        output = b''
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            output += chunk
            if len(output) > 4 * 1024 * 1024:
                raise RuntimeError('oversized response')
            if b'\n' in output:
                output = output.split(b'\n', 1)[0]
                break
        if not output:
            raise ConnectionError('notification/API process disconnected before replying')
        result = json.loads(output)
        if result.get('error'):
            raise RuntimeError(str(result['error']))
        return result


# One helper sends every alert, blocked or done (one sender, so Notification
# Center stacks alerts by Herdr space, not by state). It keeps the original
# blocked app's identity, socket and bundle path; expiry is per alert.
HELPER_SOCKET = 'blocked.sock'
HELPER_APP = 'Herdr Blocked.app'


def helper(payload, timeout=10):
    try:
        return rpc(CACHE / HELPER_SOCKET, payload, timeout)
    except (FileNotFoundError, ConnectionError):
        subprocess.run(['/usr/bin/open', '-g', str(APPS / HELPER_APP)], check=True, timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                return rpc(CACHE / HELPER_SOCKET, payload, timeout)
            except (FileNotFoundError, ConnectionError):
                time.sleep(0.1)
        raise RuntimeError('notification helper did not start')


def thread_id(path, workspace_id):
    """Notification Center's stack: one per Herdr space on one server."""
    return hashlib.sha256((path + '\0' + str(workspace_id)).encode()).hexdigest()[:32]


def api(path, method, params=None):
    return rpc(path, {'id': 'native-notify', 'method': method, 'params': params or {}})['result']


def read_config():
    path = Path(os.environ.get('HERDR_PLUGIN_CONFIG_DIR', str(CONFIG.parent))) / 'config.json'
    config = json.loads(path.read_text()) if path.exists() else {}
    duration = config.get('finished_duration_seconds', 8)
    if isinstance(duration, bool) or not isinstance(duration, (float, int)) or not math.isfinite(duration) or not 1 <= duration <= 3600:
        raise ValueError('finished_duration_seconds must be between 1 and 3600')
    return dict(config, finished_duration_seconds=duration)


def configure_duration(value):
    duration = float(value)
    if not math.isfinite(duration) or not 1 <= duration <= 3600:
        raise ValueError('Duration must be between 1 and 3600 seconds')
    target = Path(os.environ.get('HERDR_PLUGIN_CONFIG_DIR', str(CONFIG.parent))) / 'config.json'
    config = read_config()
    config['finished_duration_seconds'] = duration
    # Respect chezmoi ownership; never overwrite a managed deployed file.
    owner = subprocess.run(['chezmoi', 'source-path', str(target)], capture_output=True, text=True)
    source = Path(owner.stdout.strip()) if owner.returncode == 0 else target
    if source.suffix == '.tmpl':
        raise RuntimeError('Edit the managed config template in source instead')
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps(config, indent=2) + '\n')
    if source != target:
        subprocess.run(['chezmoi', 'apply', '--exclude', 'scripts', str(target)], check=True)
    print('Finished notifications: ' + str(duration) + ' seconds')


def ghostty_surface():
    script = 'tell application "Ghostty" to get id of focused terminal of selected tab of front window'
    return subprocess.run(['/usr/bin/osascript', '-e', script], check=True, capture_output=True, text=True, timeout=10).stdout.strip()


# Herdr renders every window's title on the server (`window_title`, configured
# as "herdr {hostname}: {workspace}"), so the Ghostty terminal showing Herdr, and
# the machine it shows, are read from the screen, not remembered. Ghostty and
# this notifier both run only on the Mac.
HERDR_TITLE = 'herdr '


def front_herdr_title():
    """The focused Ghostty terminal's title when it shows Herdr, else None."""
    title = ghostty_title(ghostty_surface())
    return title if title.startswith(HERDR_TITLE) else None


def herdr_client_ttys():
    """Terminal devices (`ttys000`) running a local Herdr client; servers have none."""
    try:
        listing = subprocess.run(['/bin/ps', '-axo', 'tty=,comm='], check=True, capture_output=True,
                                 text=True, timeout=10).stdout
    except (subprocess.SubprocessError, OSError):
        return set()
    ttys = set()
    for line in listing.splitlines():
        tty, _, command = line.strip().partition(' ')
        if tty not in ('', '??', '-') and Path(command.strip()).name == 'herdr':
            ttys.add(Path(tty).name)
    return ttys


def ghostty_terminals():
    """(id, tty, title) for every Ghostty terminal; tty is '' before Ghostty 1.4."""
    # Inside the tell block `tab` means Ghostty's tab class, so name the separators first.
    script = '''set out to ""
    set sep to ASCII character 9
    set nl to ASCII character 10
    tell application "Ghostty"
        repeat with t in terminals
            set device to ""
            try
                set device to tty of t
            end try
            set out to out & (id of t) & sep & device & sep & (name of t) & nl
        end repeat
    end tell
    return out'''
    listing = subprocess.run(['/usr/bin/osascript', '-e', script], check=True, capture_output=True,
                             text=True, timeout=10).stdout
    rows = []
    for line in listing.splitlines():
        surface, _, rest = line.partition('\t')
        device, _, title = rest.partition('\t')
        if not surface.strip():
            continue
        rows.append((surface, Path(device.strip()).name if device.strip() else '', title))
    return rows


def herdr_surface():
    """A Ghostty terminal showing Herdr: the focused one if it does, else the first.

    Ghostty 1.4 reports each terminal's tty, so the terminal running the local
    Herdr client is found directly; the `herdr ` title prefix is the fallback
    (Ghostty 1.3, or a title not rendered yet).
    """
    try:
        focused = ghostty_surface()
    except (subprocess.SubprocessError, OSError):
        focused = None
    terminals = ghostty_terminals()
    clients = herdr_client_ttys()
    by_tty = [surface for surface, tty, _ in terminals if tty and tty in clients]
    by_title = [surface for surface, _, title in terminals if title.startswith(HERDR_TITLE)]
    for candidates in (by_tty, by_title):
        if focused in candidates:
            return focused
    return (by_tty or by_title or [None])[0]


def showing(binding, space=None):
    """Whether the focused Ghostty terminal shows this binding's Herdr server, on `space`.

    The title is `herdr {hostname}: {workspace}`: the hostname tells Local from a
    saved machine, and the workspace tells apart two sessions on one machine.
    """
    if binding.get('managed_connection'):
        # A dedicated `connect` window: the terminal this notifier opened for it.
        return ghostty_surface() == binding.get('surface')
    title = front_herdr_title()
    if title is None or title_shows_local(title) == bool(binding.get('federated')):
        return False
    label = (space or {}).get('label') or (space or {}).get('name')
    return not label or title.partition(': ')[2].strip() == str(label)


def focus(route):
    federated = 'machine' in route
    snapshot = machine_snapshot(route['machine']) if federated else api(route['socket'], 'session.snapshot')['snapshot']
    agent = next((a for a in snapshot['agents'] if a['pane_id'] == route['pane']), None)
    if not agent or agent['terminal_id'] != route['occupant'] or agent.get('agent_session') != route.get('agent_session'):
        raise RuntimeError('notification target exited or was replaced; refusing stale click')
    route = dict(route, surface=route.get('surface') or herdr_surface())
    if not route['surface']:
        subprocess.run(['/usr/bin/open', '-a', 'Ghostty'], check=True, timeout=15)
        return
    script = '''on run argv
        tell application "Ghostty"
            set targetID to item 1 of argv
            set targetTerminal to first terminal whose id is targetID
            focus targetTerminal
        end tell
    end run'''
    subprocess.run(['/usr/bin/osascript', '-e', script, route['surface']], check=True, timeout=15)
    if not federated and route['socket'] != LOCAL_SOCKET:
        # A dedicated `connect` window shows only its own machine.
        api(route['socket'], 'pane.focus', {'pane_id': route['pane']})
        return
    navigate(route)


class Target:
    """The server owning a clicked pane: the local socket or a saved machine."""

    def __init__(self, route):
        self.machine = route.get('machine')
        self.pane = route['pane']

    def run(self, *args):
        subprocess.run([herdr_binary(), '--machine', self.machine, *args], stdin=subprocess.DEVNULL,
                       capture_output=True, text=True, timeout=15, check=True)

    def focus(self):
        # Same shared-focus semantics as Herdr's own notification click.
        if self.machine:
            self.run('agent', 'focus', self.pane)
        else:
            api(LOCAL_SOCKET, 'pane.focus', {'pane_id': self.pane})

    def tag(self, nonce):
        # Display-only metadata with a TTL, so a crash cannot leave it behind.
        if self.machine:
            if nonce is None:
                self.run('pane', 'report-metadata', self.pane, '--source', NAV_SOURCE, '--clear-token', NAV_TOKEN)
            else:
                self.run('pane', 'report-metadata', self.pane, '--source', NAV_SOURCE,
                         '--token', NAV_TOKEN + '=' + nonce, '--ttl-ms', '15000')
        else:
            api(LOCAL_SOCKET, 'pane.report_metadata', {'pane_id': self.pane, 'source': NAV_SOURCE,
                                                         'tokens': {NAV_TOKEN: nonce}, 'ttl_ms': 15000})

    def focused(self):
        snapshot = machine_snapshot(self.machine) if self.machine else api(LOCAL_SOCKET, 'session.snapshot')['snapshot']
        return snapshot.get('focused_pane_id') == self.pane


REMOTE_RPC = r'''
import json, os, socket, sys
session = sys.argv[1]
base = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'herdr')
path = os.path.join(base, 'herdr.sock') if session == 'default' else os.path.join(base, 'sessions', session, 'herdr.sock')
connection = socket.socket(socket.AF_UNIX)
connection.settimeout(10)
connection.connect(path)
connection.sendall(sys.stdin.buffer.read().strip() + b'\n')
output = b''
while b'\n' not in output:
    chunk = connection.recv(65536)
    if not chunk:
        break
    output += chunk
sys.stdout.buffer.write(output.split(b'\n', 1)[0])
'''


def remote_api(profile, method, params):
    """One socket API request on a saved machine; `herdr --machine` has no CLI for agent views."""
    command = shlex.join(['python3', '-c', REMOTE_RPC, profile['session']])
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', '-o', 'ClearAllForwardings=yes',
                             profile['host'], command],
                            input=json.dumps({'id': 'native-notify', 'method': method, 'params': params}),
                            capture_output=True, text=True, timeout=20)
    if result.returncode:
        detail = (result.stderr.strip().splitlines() or ['exit status ' + str(result.returncode)])[-1]
        raise ConnectionError(profile['label'] + ' API unreachable: ' + detail)
    response = json.loads(result.stdout)
    if response.get('error'):
        raise RuntimeError(str(response['error']))
    return response['result']


def ghostty_title(surface):
    script = '''on run argv
        tell application "Ghostty" to get name of (first terminal whose id is item 1 of argv)
    end run'''
    return subprocess.run(['/usr/bin/osascript', '-e', script, surface], check=True, capture_output=True,
                          text=True, timeout=10).stdout.strip()


def send_navigation_key(surface):
    script = '''on run argv
        tell application "Ghostty" to send key (item 2 of argv) modifiers (item 3 of argv) to (first terminal whose id is item 1 of argv)
    end run'''
    subprocess.run(['/usr/bin/osascript', '-e', script, surface, *NAV_KEY], check=True, timeout=10)


def title_shows_local(title):
    """Whether a Herdr window title shows Local, from its server-rendered {hostname}."""
    return socket.gethostname().casefold() in title.casefold()


def showing_local(surface):
    return title_shows_local(ghostty_title(surface))


def navigate(route):
    """Make the Herdr window show the clicked pane, switching machine if needed.

    Herdr (through 0.9.5) switches the viewed machine only from client input
    (herdr discussion #4396). So: tag the target pane, narrow the Agents list of the
    displayed machine to that tag with an agent view, and press the reserved
    next_agent key in that Ghostty surface. With a one-entry list, next_agent
    always selects the target, through Herdr's own cross-machine activation.
    """
    target = Target(route)
    surface = route['surface']
    target.focus()
    local = showing_local(surface)
    remotes = federated_machines()
    if local and not target.machine:
        return
    if not local and target.machine and [p['id'] for p in remotes] == [target.machine]:
        return  # The only attached machine is the one on screen.
    with (CACHE / 'navigate.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # The displayed machine's server holds the view: Local, or whichever saved
        # machine is on screen. Without knowing which, narrow them all.
        servers = [None] + ([] if local else remotes)
        def call(profile, method, params):
            return remote_api(profile, method, params) if profile else api(LOCAL_SOCKET, method, params)
        nonce = secrets.token_hex(8)
        narrowed = []
        try:
            target.tag(nonce)
            for profile in servers:
                current = call(profile, 'agent.view.clear', {'source': NAV_SOURCE})
                if current.get('active'):
                    raise RuntimeError('another agent view is active; not overriding it')
                narrowed.append(profile)
                call(profile, 'agent.view.set', {'source': NAV_SOURCE, 'filter': {
                    'op': 'eq', 'field': {'token': NAV_TOKEN}, 'value': nonce}})
            for attempt in range(2):
                time.sleep(NAV_SETTLE)  # Let the client receive the tag and view.
                send_navigation_key(surface)
                deadline = time.monotonic() + NAV_VERIFY
                while time.monotonic() < deadline:
                    time.sleep(0.25)
                    if showing_local(surface) != bool(target.machine) and target.focused():
                        return
            raise RuntimeError('Herdr did not switch to the notification target; is the navigation key bound?')
        finally:
            for profile in narrowed:
                try:
                    call(profile, 'agent.view.clear', {'source': NAV_SOURCE})
                except Exception as error:
                    print('agent view cleanup: ' + str(error), file=sys.stderr, flush=True)
            try:
                target.tag(None)
            except Exception as error:
                print('target tag cleanup: ' + str(error), file=sys.stderr, flush=True)


def notification_id(path, pane):
    return hashlib.sha256((path + '\0' + pane).encode()).hexdigest()


def transition(previous, agent):
    if not agent or not agent.get('agent'):
        return None, None
    occupant = [agent['terminal_id'], agent.get('agent_session')]
    status = agent['agent_status']
    if status == 'blocked':
        return 'blocked', ['blocked', occupant, agent.get('state_change_seq', 0)]
    if status in ('done', 'idle') and agent.get('completion_seq') is not None:
        return 'done', ['done', occupant, agent['completion_seq']]
    return None, None


def state_dir(path):
    directory = CACHE / hashlib.sha256(path.encode()).hexdigest()[:16]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def atomic_json(path, data):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data))
    temporary.replace(path)


def baseline(path, directory, snapshot=None):
    if snapshot is None:
        snapshot = api(path, 'session.snapshot')['snapshot']
    state = {a['pane_id']: {'key': None if a['agent_status'] == 'blocked' else transition({}, a)[1]} for a in snapshot['agents']}
    atomic_json(directory / 'state.json', state)


def session_label(agent):
    # display_agent identifies the harness/model, not the human's session.
    name = agent.get('name')
    if isinstance(name, str) and name.strip():
        return ' '.join(name.replace('-', ' ').replace('_', ' ').split()).capitalize()
    for value in [agent.get('title'), (agent.get('tokens') or {}).get('task')]:
        if isinstance(value, str) and value.strip():
            return ' '.join(value.split())
    return 'Agent session'


def summary_label(agent):
    # pi-herdr's `summary` token: what the last turn did, or what a dialog waits
    # on. Cleared as each run starts, so it never describes an earlier turn.
    value = (agent.get('tokens') or {}).get('summary')
    return ' '.join(value.split()) if isinstance(value, str) and value.strip() else None


def space_label(space, agent):
    label = space.get('label') or space.get('name')
    if label and not str(label).isdigit() and label != agent['workspace_id']:
        return str(label)
    cwd = agent.get('foreground_cwd') or agent.get('cwd')
    return Path(cwd).name if cwd and Path(cwd).name else 'Herdr space'


# Herdr's own state glyphs (its "symbols" indicators), leading the title. The
# icon slot is the app's and cannot vary per alert; a thumbnail costs body width.
STATE_GLYPHS = {'blocked': '\u00d7', 'done': '\u2713'}


def notification_text(space, agent, binding, state=None):
    context = [space_label(space, agent)]
    host = binding.get('host')
    machine = host.rsplit('@', 1)[-1] if host else binding.get('label')
    if machine and machine != 'Local' and machine.casefold() != context[0].casefold():
        context.append(machine)
    # The bold title is short and truncates; the regular body wraps. Put the
    # short location (space, plus machine when remote) in the title and the
    # longer session/task title in the body. macOS subtitles are headline-like,
    # so leave them empty. The leading glyph conveys Blocked/Finished; no IDs.
    title = ' · '.join(context)
    if state in STATE_GLYPHS:
        title = STATE_GLYPHS[state] + ' ' + title
    return {'title': title, 'subtitle': '', 'body': summary_label(agent) or session_label(agent)}


def reconcile(path, directory, snapshot=None, binding=None):
    state_file = directory / 'state.json'
    previous = json.loads(state_file.read_text()) if state_file.exists() else {}
    if binding is None:
        binding = json.loads((directory / 'binding.json').read_text())
    if snapshot is None:
        snapshot = api(path, 'session.snapshot')['snapshot']
    duration = read_config()['finished_duration_seconds']
    workspaces = {w.get('workspace_id', w.get('id')): w for w in snapshot['workspaces']}
    current = {}
    for agent in snapshot['agents']:
        pane = agent['pane_id']
        old = previous.get(pane, {})
        kind, key = transition(old, agent)
        identifier = notification_id(path, pane)
        # A pane has one alert: a new state replaces it (same ID), and a pane
        # neither blocked nor done has none. A suppressed or expired alert of
        # the same state is left alone, never reposted or removed again.
        old_kind = (old.get('key') or [None])[0]
        disposition = old.get('disposition', 'delivered')
        should_post = key and (key != old.get('key') or (kind == 'blocked' and disposition == 'suppressed-visible'))
        if kind is None or (kind != old_kind and not should_post):
            helper({'op': 'remove', 'ids': [identifier]})
        if should_post:
            status = helper({'op': 'status'})
            try:
                # Federated agents: each server tracks its own focus, so this can
                # suppress an alert the user is not looking at; the in-app toast
                # still shows it in that frontmost Herdr window.
                shown = status['frontmost'] == 'com.mitchellh.ghostty' and showing(binding, workspaces.get(agent['workspace_id']))
                visible = agent['focused'] and shown
                if not visible:
                    print('alert ' + kind + ' ' + pane + ': focused=' + str(agent['focused']) + ' frontmost=' + str(status['frontmost'])
                          + ' herdr-window-shows-machine=' + str(shown), file=sys.stderr, flush=True)
            except (subprocess.SubprocessError, OSError) as error:
                visible = False  # Never lose an alert because visibility could not be proved.
                detail = getattr(error, 'stderr', None) or error
                print('visibility unknown, alerting: ' + str(detail).strip(), file=sys.stderr, flush=True)
            disposition = 'suppressed-visible' if visible else 'delivered'
            if not visible:
                space = workspaces.get(agent['workspace_id'], {})
                text = notification_text(space, agent, binding, kind)
                route = {'pane': pane, 'occupant': agent['terminal_id']}
                if binding.get('machine'):
                    route['machine'] = binding['machine']
                else:
                    route['socket'] = path
                if binding.get('managed_connection'):
                    route['surface'] = binding['surface']
                if agent.get('agent_session') is not None:
                    route['agent_session'] = agent['agent_session']
                helper({'op': 'notify', 'id': identifier, **text,
                         'thread': thread_id(path, agent['workspace_id']),
                         'duration_seconds': 0 if kind == 'blocked' else duration, 'route': route})
            else:
                # Visible now: drop a replaced alert of the other state.
                if kind != old_kind:
                    helper({'op': 'remove', 'ids': [identifier]})
        current[pane] = {'key': key, 'disposition': disposition}
    for pane in previous.keys() - current.keys():
        helper({'op': 'remove', 'ids': [notification_id(path, pane)]})
    atomic_json(state_file, current)


def locked_reconcile(path, directory):
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reconcile(path, directory)


def connection_alive(binding):
    pid = binding.get('owner_pid')
    if binding.get('managed_connection') is not True or isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def close_connection(binding):
    """Withdraw only this connection's alerts; never touch remote processes."""
    path = binding['socket']
    directory = state_dir(path)
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        target = directory / 'binding.json'
        if not target.exists():
            return
        current = json.loads(target.read_text())
        if current.get('connection_token') != binding.get('connection_token'):
            return
        target.unlink()
        withdraw(path, directory)


def withdraw(path, directory):
    """Remove every alert this state directory posted and forget its state."""
    state_file = directory / 'state.json'
    previous = json.loads(state_file.read_text()) if state_file.exists() else {}
    ids = [identifier for pane in previous for identifier in
           (notification_id(path, pane), 'test-' + notification_id(path, pane))]
    if ids:
        helper({'op': 'remove', 'ids': ids})
    state_file.unlink(missing_ok=True)


def register_connection(binding):
    path = binding['socket']
    directory = state_dir(path)
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        # Prove our API forward works, regardless of unrelated SSH config forwards.
        api(path, 'session.snapshot')
        target = directory / 'binding.json'
        old = json.loads(target.read_text()) if target.exists() else {}
        if old.get('connection_token') != binding['connection_token']:
            baseline(path, directory)
        atomic_json(target, binding)


def herdr_binary():
    # launchd and the helper apps run with a minimal PATH.
    found = shutil.which('herdr')
    if found:
        return found
    for candidate in ('/opt/homebrew/bin/herdr', '/usr/local/bin/herdr', str(Path.home() / '.local/bin/herdr')):
        if os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError('herdr executable not found')


def saved_remotes():
    result = subprocess.run([herdr_binary(), 'machine', 'list'], capture_output=True, text=True, timeout=10)
    if result.returncode:
        return []
    profiles = []
    for line in result.stdout.splitlines():
        fields = line.split('\t')
        if len(fields) == 5 and fields[4] == 'enabled':
            profiles.append({'id': fields[0], 'label': fields[1], 'host': fields[2], 'session': fields[3]})
    return profiles


def machine_snapshot(machine):
    """Read a saved machine's state over Herdr's own non-interactive SSH transport."""
    result = subprocess.run([herdr_binary(), '--machine', machine, 'api', 'snapshot'], stdin=subprocess.DEVNULL,
                            capture_output=True, text=True, timeout=15)
    if result.returncode:
        detail = (result.stderr.strip().splitlines() or ['exit status ' + str(result.returncode)])[-1]
        raise ConnectionError('saved machine ' + machine + ' unreachable: ' + detail)
    response = json.loads(result.stdout)
    if response.get('error'):
        raise RuntimeError(str(response['error']))
    return response['result']['snapshot']


def federation_bridges(ps_output):
    """(host candidates, session) for each live local Herdr client's remote bridge.

    A local Herdr TUI that shows a saved machine keeps an SSH child running
    `herdr [--session S] remote-client-bridge` on that machine.
    """
    processes = {}
    for line in ps_output.splitlines():
        fields = line.split(None, 2)
        if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
            processes[fields[0]] = (fields[1], fields[2].split())
    bridges = []
    for parent_pid, tokens in processes.values():
        if not tokens or Path(tokens[0]).name != 'ssh' or 'remote-client-bridge' not in tokens:
            continue
        parent = processes.get(parent_pid)
        if not parent or not parent[1] or Path(parent[1][0]).name != 'herdr' or 'server' in parent[1][1:]:
            continue
        index = tokens.index('remote-client-bridge')
        session = tokens[index - 1] if index >= 2 and tokens[index - 2] == '--session' else 'default'
        bridges.append((set(tokens[1:index]), session))
    return bridges


def federated_machines():
    """Saved machines the local Herdr TUI is attached to right now."""
    ps = subprocess.run(['/bin/ps', '-axww', '-o', 'pid=,ppid=,command='], capture_output=True,
                        text=True, timeout=10, check=True).stdout
    bridges = federation_bridges(ps)
    if not bridges:
        return []
    return [profile for profile in saved_remotes()
            if any(profile['host'] in hosts and profile['session'] == session for hosts, session in bridges)]


def federated_key(profile):
    return 'herdr-machine:' + profile['id'] + '/' + profile['session']


def sync_federated(errors):
    """Alert for saved machines only while the local Herdr TUI is attached to them.

    No windows are opened. Machines with a live dedicated `connect` window are
    left to that connection. Detaching withdraws the machine's alerts.
    """
    profiles = federated_machines()
    dedicated = set()
    for target in CACHE.glob('*/binding.json'):
        try:
            binding = json.loads(target.read_text())
        except (OSError, ValueError):
            continue
        if binding.get('host') and connection_alive(binding):
            dedicated.add((binding['host'], binding.get('session')))
    attached = set()
    for profile in profiles:
        if (profile['host'], profile['session']) in dedicated:
            continue
        key = federated_key(profile)
        attached.add(key)
        directory = state_dir(key)
        binding = dict(machine=profile['id'], label=profile['label'], host=profile['host'],
                       session=profile['session'], federated=True)
        try:
            snapshot = machine_snapshot(profile['id'])
            with (directory / 'lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                marker = directory / 'federated.json'
                if marker.exists():
                    reconcile(key, directory, snapshot, binding)
                else:
                    baseline(key, directory, snapshot)  # No alerts for history on attach.
                atomic_json(marker, binding)
            errors.pop(key, None)
        except Exception as error:
            # Keep state through transient SSH loss; Herdr reconnects itself.
            message = str(error)
            if errors.get(key) != message:
                print(key + ': ' + message, file=sys.stderr, flush=True)
            errors[key] = message
    for marker in CACHE.glob('*/federated.json'):
        try:
            recorded = json.loads(marker.read_text())
            key = federated_key(dict(recorded, id=recorded['machine']))
        except (OSError, ValueError, KeyError):
            continue
        if key in attached:
            continue
        with (marker.parent / 'lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            withdraw(key, marker.parent)
            marker.unlink(missing_ok=True)


def connect_remote(host=None, session='default'):
    if host is None:
        profiles = saved_remotes()
        if len(profiles) == 1:
            host, session = profiles[0]['host'], profiles[0]['session']
        else:
            for index, profile in enumerate(profiles, 1):
                print(str(index) + '. ' + profile['label'] + ' (' + profile['session'] + ')')
            value = input('Saved machine number or SSH host: ').strip()
            if value.isdecimal() and 1 <= int(value) <= len(profiles):
                profile = profiles[int(value) - 1]
                host, session = profile['host'], profile['session']
            else:
                host = value
                session = input('Herdr session [default]: ').strip() or 'default'
    for target in CACHE.glob('*/binding.json'):
        binding = json.loads(target.read_text())
        if binding.get('host') == host and binding.get('session') == session and connection_alive(binding):
            script = '''on run argv
                tell application "Ghostty"
                    focus (first terminal whose id is item 1 of argv)
                end tell
            end run'''
            try:
                subprocess.run(['/usr/bin/osascript', '-e', script, binding['surface']], check=True, timeout=10)
                print('Opened existing remote connection ' + binding['label'])
                return
            except subprocess.CalledProcessError:
                close_connection(binding)
    from remote_connection import open_connection
    request = open_connection(host, session, Path(__file__).resolve(), CACHE)
    print('Opening dedicated remote Herdr connection ' + host + '/' + session)
    print('SSH and native notification routing are managed automatically.')
    return request


def watch():
    CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (CACHE / 'watcher.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('another notification watcher is already running')
        errors = {}
        stopping = [False]
        def stop(signum, frame):
            stopping[0] = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            # An interrupted click must not leave the Agents list narrowed.
            api(LOCAL_SOCKET, 'agent.view.clear', {'source': NAV_SOURCE})
        except Exception:
            pass
        while not stopping[0]:
            for binding_file in CACHE.glob('*/binding.json'):
                try:
                    binding = json.loads(binding_file.read_text())
                    path = binding.get('socket')
                    if not path:
                        continue
                    if binding.get('host') and not connection_alive(binding):
                        if binding.get('managed_connection'):
                            from remote_connection import reap_abandoned
                            reap_abandoned(binding, CACHE)
                        close_connection(binding)
                        continue
                    locked_reconcile(path, binding_file.parent)
                    errors.pop(str(binding_file), None)
                except Exception as error:
                    message = str(error)
                    if errors.get(str(binding_file)) != message:
                        print(str(binding_file) + ': ' + message, file=sys.stderr, flush=True)
                    errors[str(binding_file)] = message
            try:
                sync_federated(errors)
                errors.pop('federation', None)
            except Exception as error:
                message = str(error)
                if errors.get('federation') != message:
                    print('federation: ' + message, file=sys.stderr, flush=True)
                errors['federation'] = message
            # Dedicated windows own SSH; polling recovers missed events/restarts.
            time.sleep(2)


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else 'event'
    if command == 'focus':
        focus(json.load(sys.stdin))
        return
    if command == 'watch':
        watch()
        return
    if command == 'connect':
        connect_remote(sys.argv[2] if len(sys.argv) > 2 else None,
                       sys.argv[3] if len(sys.argv) > 3 else 'default')
        return
    if command == 'connect-run':
        from remote_connection import run_connection
        code = run_connection(Path(sys.argv[2]), register_connection, close_connection)
        if code:
            raise RuntimeError('Remote Herdr client exited with status ' + str(code))
        return
    if command == 'set-duration':
        configure_duration(sys.argv[2])
        return
    if command == 'configure':
        print('Herdr Native Notify — Finished notification duration')
        print('Current: ' + str(read_config()['finished_duration_seconds']) + ' seconds (default: 8)')
        value = input('New duration in seconds [Enter to keep]: ').strip()
        if value:
            configure_duration(value)
        return
    if command in ('setup', 'status'):
        status = helper({'op': 'status'})
        if command == 'setup' and status['authorization'] == 0:
            status = helper({'op': 'authorize'}, timeout=120)
        print(status, helper({'op': 'list'}), flush=True)
        return
    path = os.environ.get('HERDR_SOCKET_PATH', str(Path.home() / '.config/herdr/herdr.sock'))
    directory = state_dir(path)
    with (directory / 'lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if command in ('bind', 'activate'):
            # Registers this session with the watcher. No Ghostty surface is
            # stored: the window showing Herdr is found from its title.
            # `bind` is kept as an alias of `activate`.
            binding_file = directory / 'binding.json'
            binding = {'socket': path, 'label': 'Local'}
            if binding_file.exists() and json.loads(binding_file.read_text()) == binding:
                print('Native notifications already active for this Herdr session.')
                return
            atomic_json(binding_file, binding)
            baseline(path, directory)
            print('Activated native notifications for this Herdr session; existing completions baselined.')
        elif command == 'event':
            if (directory / 'binding.json').exists():
                reconcile(path, directory)
        elif command in ('test-blocked', 'test-done'):
            kind = command.split('-')[1]
            binding = json.loads((directory / 'binding.json').read_text())
            snapshot = api(path, 'session.snapshot')['snapshot']
            pane = os.environ['HERDR_PANE_ID']
            agent = next(a for a in snapshot['agents'] if a['pane_id'] == pane)
            route = {'socket': path, 'pane': pane, 'occupant': agent['terminal_id']}
            if agent.get('agent_session') is not None:
                route['agent_session'] = agent['agent_session']
            space = next((w for w in snapshot['workspaces'] if w.get('workspace_id', w.get('id')) == agent['workspace_id']), {})
            text = notification_text(space, agent, binding, kind)
            text['body'] += '\nTest notification'
            print(helper({'op': 'notify', 'id': 'test-' + notification_id(path, pane), **text,
                          'thread': thread_id(path, agent['workspace_id']),
                                'duration_seconds': 0 if kind == 'blocked' else read_config()['finished_duration_seconds'], 'route': route}))
        elif command == 'clear-tests':
            pane = os.environ['HERDR_PANE_ID']
            helper({'op': 'remove', 'ids': ['test-' + notification_id(path, pane)]})
        else:
            raise RuntimeError('unknown command: ' + command)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('herdr-native-notify: ' + str(error), file=sys.stderr)
        sys.exit(1)
