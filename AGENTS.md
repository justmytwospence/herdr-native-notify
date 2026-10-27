# Herdr Native Notify

Maintained here (github.com/justmytwospence/herdr-native-notify), never in installed plugin copies. Dotfiles pins a commit in `.chezmoidata/herdr_plugins.json` (Macs); `herdr plugin install` runs the `[[build]]` steps (`build.py`, `install.py --ensure`). Publish first, then bump the pin (dotfiles `docs/plugins.md`).

- macOS UserNotifications only; no private notification APIs, preference edits, or reverse SSH callbacks.
- One exception to "no simulated keystrokes": cross-machine click navigation sends the single reserved `next_agent` key (`ctrl+alt+shift+f12`) to the Ghostty terminal showing Herdr through Ghostty's scripting API, only while an agent view narrows the Agents list to the target. Never System Events, never global keystrokes, never typed text. Remove it once Herdr can switch an attached client's machine (herdr discussion #4396).
- One helper sends every alert (one sender, so alerts stack per Herdr space); its bundle identifier is `com.justmytwospence.herdr-notify.blocked` (a fresh identity: Notification Center kept a stale blank icon for the old `herdr-native-notify` one). Keep it and the bundle path stable; a new identity means re-allowing notifications and re-selecting Persistent. Blocked/finished differ per alert (expiry, title glyph), never per app. Presentation styles are user-selected per app.
- Ghostty and this notifier run only on the Mac. Find the terminal showing Herdr, and the machine it shows, from Herdr's server-rendered window title (`herdr {hostname}: {workspace}`); never store a surface for local or federated sessions. Only managed `connect` windows keep the surface they opened. Never treat a non-Herdr frontmost application or terminal as Herdr.
- Events are invalidations: reconcile authoritative state, not potentially stale event payloads. Scope IDs by socket/session and pane occupant.
- Keep TUI notifications enabled until native routing and permissions have passed live acceptance tests.
- Test with `python3 -B -m unittest discover -s tests`; build with `python3 build.py`. Do not push without authorization.
