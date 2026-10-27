# Herdr Native Notify

Native macOS notifications for Herdr 0.9.3+, with Ghostty 1.3+ click navigation.
Developed here; the currently installed plugin is linked locally, not published
or pinned for distribution to other machines.

## Behavior

- One sender, **Herdr**, for every alert, so Notification Center stacks alerts
  by Herdr space (each alert's thread is its space on its server), not by state.
  A pane has at most one alert: a new state replaces it in place.
- **Blocked** (title leads with Herdr's `×`): native Persistent alert, no age-based
  expiry. Click to focus the exact Ghostty surface and Herdr space/tab/pane.
  Unblocking or closing the pane removes its notification. Dismissing it does
  not repost the same state.
- **Finished** (title leads with Herdr's `✓`): native Persistent alert removed
  after **8 seconds** by default; configurable from 1 to 3600 seconds. The timer
  is per alert and uses supported Apple removal APIs, not private preferences.
  It also removes Notification Center history. Its deadline starts at
  submission, not guaranteed visible presentation.
- The bold title is the space name, followed by the remote machine only when
  applicable. The regular, wrapping body is pi-herdr's `summary` pane token (one
  line on what the turn did or what the agent waits on) when set, else the
  session name/task title, which needs the room. Subtitles are empty to avoid
  another headline-like line. The title's state glyph conveys Blocked/Finished; no
  redundant status, “Local” label or click instructions.
- The app icon (the only image macOS allows in an alert's icon slot, the same
  for every alert) is neutral, an SF Symbol rendered at build time by
  `Icon.swift` (`APP_ICON` in `build.py`). State images were dropped: a
  thumbnail takes width from the text.
  Harness/model labels and internal space/pane IDs
  never appear in visible text. Unnamed or numbered spaces fall back to the
  working-directory name, then “Herdr space”. Test cards alone say “Test notification”.
- A notification click validates the current pane occupant before routing, so
  an old alert cannot target a replacement agent.
- An alert is suppressed while you are looking at its pane: Ghostty is
  frontmost, its focused terminal shows Herdr for the agent's machine (read
  from Herdr's server-rendered `herdr {hostname}: {workspace}` window title),
  and Herdr reports the pane focused. No window is remembered, so new windows
  and tabs just work. If the screen cannot be read, alert delivery wins.

In System Settings > Notifications, **Herdr** needs Allow notifications,
Desktop, Notification Center, and **Persistent** (new apps default to
Banners, which would let blocked alerts slide away). Its bundle identifier is
`com.justmytwospence.herdr-notify.blocked`; installing removes the retired Herdr
Finished helper.
Apple controls the presentation style per app; the helper cannot change it.
Apple documents removal from Notification Center, not a cross-version promise of
immediate withdrawal of an already-visible desktop alert. Timing is best effort
under Focus, sleep, lock-screen and Notification Center presentation policies.

## Settings inside Herdr

Invoke the plugin action **Native notifications: finished duration** from the
command palette (`Ctrl-b :`) to edit the duration in an overlay.

Or use:

```sh
herdr-native-notify set-duration 12
```

The setting is `finished_duration_seconds` in:

```text
~/.config/herdr/plugins/config/herdr-native-notify/config.json
```

If chezmoi owns that target, the action edits its source and applies only that
file. It never flattens or rewrites the managed Herdr config. Herdr's built-in
Settings panel does not support plugin settings (through 0.9.5); the plugin action is
the supported in-Herdr control.

## Installation and recovery

```sh
herdr plugin install justmytwospence/herdr-native-notify --ref <commit> --yes
```

The install's build steps compile and ad-hoc sign the helper (`build.py`) and
install it, or update an existing install in place (`install.py --ensure`).
The asset-catalog icon needs full Xcode (`actool`); without it the helper still
works with a plain `.icns`. Then, once per Mac, run `python3 bridge.py setup`
and `activate` as below.

Developing from a checkout (`plugin link` runs no build steps):

```sh
cd ~/Projects/herdr-native-notify
python3 -B -m unittest discover -s tests -v
swiftc -swift-version 5 -D EXPIRY_TESTS Helper.swift -o /tmp/herdr-notify-expiry-tests
/tmp/herdr-notify-expiry-tests
python3 build.py
python3 install.py           # initial install; refuses existing apps
python3 install.py --update  # subsequent scoped, validated atomic replacement
# (install.py --ensure picks whichever applies)
herdr plugin link ~/Projects/herdr-native-notify
python3 bridge.py setup
python3 bridge.py activate   # register this Herdr session with the watcher (`bind` is an alias)
```

The helper lives in `~/Applications/Herdr Native Notify/`; its bundle identifier is
stable across upgrades. They are ad-hoc signed, not
notarized. `install.py` validates signatures/ownership and stops only exact owned
helper executables. No notification-preference writes or permission resets.

Dotfiles owns these reviewed deployment targets:

- `~/.local/bin/herdr-native-notify`: launcher into the installed helper resource.
- `~/Library/LaunchAgents/com.justmytwospence.herdr-native-notify.plist`: Mac login
  watcher; launchd restarts it. It is bootstrapped on the configured Mac.
- The plugin's `config.json` with the 8-second default.

The watcher reconciles registered sessions through authoritative public
JSON API snapshots every two seconds; plugin event hooks also reconcile changes
immediately. Per-session locks serialize both paths. Reconciliation recovers from
missed events, helper restarts, and disconnected servers without reading agent
transcripts. The helper restarts on demand. Finished deadlines are saved atomically
and swept after helper restart/sleep; generation checks prevent an old timer from
removing a newer completion with the same ID.

Bindings and notification state are under `~/Library/Caches/herdr-native-notify/`.
A binding registers a Herdr server with the watcher; local and federated
bindings store no Ghostty surface, since Herdr's window title says which terminal
shows Herdr and which machine it shows. Managed remote connections keep the
surface of the window they open, and clean up on close. A fresh binding
baselines historical completions. The only synthesized input is the reserved
navigation key described under "Saved machines in the local Herdr UI".

Diagnostics:

```sh
herdr-native-notify status
launchctl print gui/$(id -u)/com.justmytwospence.herdr-native-notify
```

Watcher log: `~/Library/Logs/herdr-native-notify.log`. Native click/expiry errors
use macOS unified logging (`Herdr` messages from the helpers). Bounded diagnostic
logs under `~/Library/Caches/herdr-native-notify/{blocked,done}-events.log` record
notification IDs, submission lifetimes, clicks, removal and expiry—no agent text.

## Saved machines in the local Herdr UI

Nothing to do: while your local Herdr TUI is attached to a saved machine
(`herdr machine list`), the watcher also posts that machine's blocked/finished
alerts. It opens no windows and runs no SSH of its own: attachment is detected
from the local client's `remote-client-bridge` SSH child, and state is read with
`herdr --machine <id> api snapshot`, which reuses Herdr's non-interactive
transport. Quitting the TUI or dropping the machine withdraws its alerts;
historical completions are baselined on attach. Transient SSH loss keeps state
while Herdr reconnects. A machine with a live dedicated `connect` window (below)
is left to that window, so alerts are never duplicated.

Clicking brings a Ghostty terminal showing Herdr forward (the focused one if it
shows Herdr, else the first titled `herdr …`) after checking
the agent still occupies its pane, then lands on the exact machine and pane,
whichever machine the window was showing. Local alerts behave the same way when
the window shows a saved machine.

Herdr switches the viewed machine only from client input; there is no API for it
through 0.9.5 or on master (herdr discussion #4396, open). The click therefore:

1. focuses the pane on its own server (`pane.focus`, or `herdr --machine <id>
   agent focus`), the same shared-focus effect as Herdr's own notification click;
2. stops there when the window already shows that machine (judged from the
   Ghostty title, whose `{hostname}` Herdr renders on the displayed server);
3. otherwise tags the pane with a display-only token (15 s TTL), sets an agent
   view (source `herdr-native-notify`) filtering the Agents list to that token on
   the displayed server (Local, or every attached saved machine over plain SSH
   when it is not Local), and sends `ctrl+alt+shift+f12` to that terminal
   with Ghostty's `send key`. Herdr binds that key to `next_agent`; with a
   one-entry list it selects the target through Herdr's own cross-machine
   activation. The click verifies the result, retries the key once, and always
   clears the view and token. It refuses to replace another source's agent view.

Requirements: `ctrl+alt+shift+f12` in `keys.next_agent` (dotfiles adds it), a
`ui.window_title` containing `{hostname}`, and Ghostty's AppleScript enabled.
Ghostty 1.3.1's scripted `send key` encodes only function keys, hence F12. While
the click runs (about 2 s), every client of the narrowed server briefly shows
just the target in its Agents list. Each server tracks its own focus, so a remote
agent focused on its server is treated as visible while a Herdr window showing
that machine is frontmost.

## Direct SSH clients

Use **Native notifications: connect remote** from the Mac's Herdr command
palette, or:

```sh
herdr-native-notify connect              # uses your sole enabled saved machine
herdr-native-notify connect nuc homelab  # explicit target; default session if omitted
```

One action opens a dedicated Ghostty window, authenticates with normal OpenSSH,
runs the remote Herdr client, forwards its public JSON API over the same managed
SSH connection, and automatically registers the exact terminal UUID returned by
Ghostty's new-window API. No separate tunnel or binding command is needed. The
manual `bind-remote` command has been removed; arbitrary SSH windows are not
silently guessed or monitored.

Saved targets come from `herdr machine list`; multiple targets are selectable.
Opening the same live target again focuses its existing managed window. Standard
`~/.ssh/config` remains authoritative. A private ControlMaster socket reuses
ordinary SSH authentication; host-key/password prompts appear in the new window.
No passwords or keys are stored. Unrelated configured forwarding failures do not
falsely disable native support: registration requires an actual forwarded API
probe, not merely a socket file or successful SSH launch.

Network disconnects reconnect automatically while the dedicated window is open.
Detaching or closing it withdraws its alerts and cleans up only its owned SSH
transport and routing. The watcher also recovers cleanup after abrupt process
termination. It never stops the remote Herdr server or remote agents. Nothing
needs to be installed in the remote server's plugins.

Real NUC `homelab` acceptance passed: automatic native blocked persistence
beyond 8 seconds, clearing on unblock, finished expiry, exact Ghostty/remote-pane
navigation-handler routing, automatic reconnection after private SSH transport
termination, and window-close cleanup. Existing remote agents were preserved;
only owned temporary test workspaces were created and removed. The notification
click handler was exercised directly; these checks do not simulate an OS click.

**Important limits:** public pane focus affects all attached shell clients in
that server session. Switching the machine shown in a local
federated Herdr UI uses the reserved navigation key above until Herdr has an
activation API; keep remote TUI coverage. Herdr also has one client-wide
toast mode: disabling only local TUI duplicates while retaining remote toasts in
one federated window is not supported. The current setup deliberately leaves
existing TUI toasts unchanged rather than losing remote notifications.

## Acceptance tests

```sh
python3 tests/live_lifecycle.py
```

This explicit live test creates/closes only its own no-focus test workspace. It
verifies automatic blocked persistence beyond 8 seconds, clearing on unblock,
and automatic completion expiry through the linked plugin/watcher. These API
checks establish Notification Center behavior; they are not screenshot proof of
desktop presentation. If a test notification is clicked during a timed assertion,
the audit log identifies acknowledgement and that assertion is skipped rather
than incorrectly reporting an expiry failure. Exact local click navigation was
also user-confirmed, including updated-helper acknowledgement in the live tests.

## Rollback

Uninstall or unlink `herdr-native-notify`, boot out its named LaunchAgent, and quit only its
helper. Remove the three managed deployment sources if retiring the setup.
No Herdr server restart or termination is needed. Existing TUI configuration is
unchanged.

## Sources

- https://herdr.dev/llms.txt (version-pinned docs)
- https://herdr.dev/docs/plugins/
- https://herdr.dev/docs/socket-api/
- https://ghostty.org/docs/features/applescript
- https://support.apple.com/guide/mac-help/mh40583/26/mac/26
- https://developer.apple.com/documentation/usernotifications/unnotificationsettings/alertstyle
- https://developer.apple.com/documentation/usernotifications/unusernotificationcenter/removedeliverednotifications(withidentifiers:)
- https://github.com/herdrdev/herdr/blob/v0.9.5/src/server/headless/client_views.rs
- https://github.com/herdrdev/herdr/blob/v0.9.5/src/client/shell_runtime.rs
- https://man.openbsd.org/ssh (stream-local `-L` forwarding)
