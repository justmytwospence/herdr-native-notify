import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('bridge', Path(__file__).resolve().parents[1] / 'bridge.py')
b = importlib.util.module_from_spec(spec)
spec.loader.exec_module(b)


def agent(status='blocked', seq=1):
    return dict(pane_id='w1:p1', workspace_id='w1', terminal_id='terminal1', agent='pi',
                title='Fix login flow', display_agent='pi model-example:high', agent_status=status, state_change_seq=seq,
                completion_seq=seq if status in ('idle', 'done') else None, focused=False)


class SurfaceTests(unittest.TestCase):
    def lookup(self, focused, terminals, ttys):
        with patch.object(b, 'ghostty_surface', return_value=focused), \
                patch.object(b, 'ghostty_terminals', return_value=terminals), \
                patch.object(b, 'herdr_client_ttys', return_value=ttys):
            return b.herdr_surface()

    def test_tty_beats_title(self):
        terminals = [('A', 'ttys001', 'herdr lookalike'), ('B', 'ttys000', 'untitled')]
        self.assertEqual(self.lookup('Z', terminals, {'ttys000'}), 'B')

    def test_focused_herdr_terminal_wins(self):
        terminals = [('A', 'ttys000', 'herdr mac: x'), ('B', 'ttys002', 'herdr NUC: y')]
        self.assertEqual(self.lookup('B', terminals, {'ttys000', 'ttys002'}), 'B')

    def test_title_fallback_without_tty(self):
        terminals = [('A', '', 'zsh'), ('B', '', 'herdr mac: x')]
        self.assertEqual(self.lookup('A', terminals, {'ttys000'}), 'B')
        self.assertIsNone(self.lookup('A', [('A', '', 'zsh')], set()))

    def test_client_ttys_skip_servers(self):
        listing = '?? herdr\nttys000 herdr\nttys003 /opt/homebrew/bin/herdr\nttys001 -zsh\n'
        with patch.object(b.subprocess, 'run', return_value=b.subprocess.CompletedProcess([], 0, listing, '')):
            self.assertEqual(b.herdr_client_ttys(), {'ttys000', 'ttys003'})


class BridgeTests(unittest.TestCase):
    def test_helper_recovers_disconnect_during_restart(self):
        with patch.object(b, 'rpc', side_effect=[ConnectionError('restart'), {'ok': True}]) as rpc, patch.object(b.subprocess, 'run') as launch:
            self.assertEqual(b.helper({'op': 'status'}), {'ok': True})
        launch.assert_called_once()
        self.assertEqual(rpc.call_count, 2)

    def test_ids_scope_sessions(self):
        self.assertNotEqual(b.notification_id('/a.sock', 'w1:p1'), b.notification_id('/b.sock', 'w1:p1'))

    def test_seen_idle_completion_is_done(self):
        self.assertEqual(b.transition({}, agent('idle'))[0], 'done')

    def test_idle_without_completion_is_not_done(self):
        a = agent('idle')
        a['completion_seq'] = None
        self.assertIsNone(b.transition({}, a)[0])

    def test_working_and_unknown_do_not_notify(self):
        for state in ['working', 'unknown']:
            self.assertIsNone(b.transition({}, agent(state))[0])

    def test_replacement_has_new_identity(self):
        a = agent()
        replacement = dict(a, agent_session={'value': 'new'})
        self.assertNotEqual(b.transition({}, a)[1], b.transition({}, replacement)[1])

    def reconcile(self, agents, previous=None):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'binding.json').write_text(json.dumps({'surface': 'surface1'}))
            if previous is not None:
                (path / 'state.json').write_text(json.dumps(previous))
            snapshot = {'agents': agents, 'workspaces': [{'workspace_id': 'w1', 'label': 'test-space'}]}
            def helper(payload, timeout=10):
                calls.append((payload.get('state'), payload))
                return {'frontmost': 'browser'} if payload['op'] == 'status' else {'ok': True}
            with patch.object(b, 'api', return_value={'snapshot': snapshot}), patch.object(b, 'helper', side_effect=helper):
                b.reconcile('/test.sock', path)
            return calls, json.loads((path / 'state.json').read_text())

    def test_blocked_identity_labels_and_route(self):
        calls, state = self.reconcile([agent()])
        notification = next(payload for kind, payload in calls if payload['op'] == 'notify')
        self.assertEqual(notification['title'], '× test-space')
        self.assertEqual(notification['subtitle'], '')
        self.assertNotIn('model-example', notification['body'])
        self.assertEqual(notification['body'], 'Fix login flow')
        for field in ['title', 'subtitle', 'body']:
            self.assertNotIn('w1:p1', notification[field])
            self.assertNotIn('w1', notification[field])
        self.assertEqual(notification['route']['pane'], 'w1:p1')
        self.assertEqual(notification['route']['socket'], '/test.sock')

    def test_notification_format_is_location_title_and_session_body(self):
        space = {'label': 'api-server'}
        local = b.notification_text(space, agent(), {'label': 'Local'})
        self.assertEqual(local, {'title': 'api-server', 'subtitle': '', 'body': 'Fix login flow'})
        remote = b.notification_text(space, agent(), {'host': 'spencer@nuc', 'label': 'spencer@nuc/homelab'})
        self.assertEqual(remote['subtitle'], '')
        self.assertEqual(remote['title'], 'api-server · nuc')
        self.assertNotIn('spencer', remote['title'])
        self.assertNotIn('homelab', remote['title'])
        self.assertEqual(b.notification_text({'label': 'nuc'}, agent(), {'host': 'nuc'})['title'], 'nuc')
        self.assertEqual(b.session_label(dict(agent(), title='  Fix\n login   flow  ')), 'Fix login flow')

    def test_summary_token_is_the_body_when_present(self):
        space = {'label': 'api-server'}
        a = dict(agent(), tokens={'task': 'Fix login flow', 'summary': '  Fixed the\n login redirect  '})
        self.assertEqual(b.notification_text(space, a, {'label': 'Local'})['body'], 'Fixed the login redirect')
        blank = dict(agent(), tokens={'summary': '   '})
        self.assertEqual(b.notification_text(space, blank, {'label': 'Local'})['body'], 'Fix login flow')
        self.assertEqual(b.notification_text(space, agent(), {'label': 'Local'})['body'], 'Fix login flow')

    def test_session_label_uses_human_identity_not_model(self):
        self.assertEqual(b.session_label(dict(agent(), name='login-reviewer')), 'Login reviewer')
        self.assertEqual(b.session_label(dict(agent(), name='herdr-notifications')), 'Herdr notifications')
        self.assertEqual(b.session_label(agent()), 'Fix login flow')
        self.assertEqual(b.session_label(dict(agent(), title=None, tokens={'task': 'Investigate cache misses'})), 'Investigate cache misses')
        self.assertEqual(b.session_label(dict(agent(), title=None)), 'Agent session')

    def test_space_label_never_falls_back_to_internal_id(self):
        a = dict(agent(), cwd='/Users/example/Projects/my-project')
        self.assertEqual(b.space_label({}, a), 'my-project')
        self.assertEqual(b.space_label({'label': '1'}, a), 'my-project')
        self.assertEqual(b.space_label({'label': a['workspace_id']}, a), 'my-project')
        self.assertEqual(b.space_label({}, agent()), 'Herdr space')

    def test_dismissed_blocked_not_reposted(self):
        a = agent()
        calls, _ = self.reconcile([a], {'w1:p1': {'key': b.transition({}, a)[1]}})
        self.assertFalse(any(p['op'] == 'notify' for _, p in calls))

    def test_visible_suppressed_blocked_posts_when_backgrounded(self):
        a = agent()
        previous = {'w1:p1': {'key': b.transition({}, a)[1], 'disposition': 'suppressed-visible'}}
        calls, state = self.reconcile([a], previous)
        self.assertTrue(any(p['op'] == 'notify' for _, p in calls))
        self.assertEqual(state['w1:p1']['disposition'], 'delivered')

    def test_unblock_removes_blocked_notification(self):
        calls, _ = self.reconcile([agent('working')], {'w1:p1': {'key': ['blocked']}})
        self.assertTrue(any(p['op'] == 'remove' for _, p in calls))
        self.assertFalse(any(p['op'] == 'notify' for _, p in calls))

    def test_closed_pane_clears_its_alert(self):
        calls, state = self.reconcile([], {'w1:p1': {'key': ['blocked']}})
        self.assertEqual([p['ids'] for _, p in calls if p['op'] == 'remove'], [[b.notification_id('/test.sock', 'w1:p1')]])
        self.assertEqual(state, {})

    def test_one_alert_per_pane_stacked_by_space_with_its_state(self):
        calls, state = self.reconcile([agent()])
        blocked = next(p for _, p in calls if p['op'] == 'notify')
        self.assertTrue(blocked['title'].startswith('\u00d7 '))
        self.assertEqual(blocked['thread'], b.thread_id('/test.sock', 'w1'))
        self.assertNotEqual(b.thread_id('/test.sock', 'w1'), b.thread_id('/test.sock', 'w2'))
        self.assertNotEqual(b.thread_id('/test.sock', 'w1'), b.thread_id('/other.sock', 'w1'))
        # Blocked -> done replaces the same alert; no removal first.
        calls, state = self.reconcile([agent('done')], state)
        self.assertFalse(any(p['op'] == 'remove' for _, p in calls))
        done = next(p for _, p in calls if p['op'] == 'notify')
        self.assertEqual((done['title'], done['id']), ('\u2713 test-space', blocked['id']))
        # A delivered done alert is left to expire, never withdrawn by later passes.
        calls, _ = self.reconcile([agent('done')], state)
        self.assertEqual(calls, [])

    def test_completion_is_not_repeated_after_seen(self):
        done = agent('done')
        calls, _ = self.reconcile([agent('idle')], {'w1:p1': {'key': b.transition({}, done)[1]}})
        self.assertFalse(any(p['op'] == 'notify' for _, p in calls))

    def test_stale_click_refuses_to_focus(self):
        route = dict(socket='/a', pane='w1:p1', occupant='old', surface='surface1')
        with patch.object(b, 'api', return_value={'snapshot': {'agents': [agent()]}}), patch.object(b.subprocess, 'run') as run:
            with self.assertRaises(RuntimeError):
                b.focus(route)
            run.assert_not_called()

    def test_blocked_never_expires(self):
        calls, _ = self.reconcile([agent()])
        payload = next(p for _, p in calls if p['op'] == 'notify')
        self.assertEqual(payload['duration_seconds'], 0)
        self.assertNotIn('agent_session', payload['route'])

    def test_finished_uses_configured_duration(self):
        with patch.object(b, 'read_config', return_value={'finished_duration_seconds': 12}):
            calls, _ = self.reconcile([agent('done')])
        payload = next(p for _, p in calls if p['op'] == 'notify')
        self.assertEqual(payload['duration_seconds'], 12)

    def test_config_rejects_invalid_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            with patch.dict(b.os.environ, {'HERDR_PLUGIN_CONFIG_DIR': directory}):
                for duration in [-1, 0, 3601, True, '8', None]:
                    config.write_text(json.dumps({'finished_duration_seconds': duration}))
                    with self.assertRaises(ValueError):
                        b.read_config()
                config.write_text(json.dumps({'finished_duration_seconds': 8}))
                self.assertEqual(b.read_config()['finished_duration_seconds'], 8)

    def test_successful_click_targets_exact_surface_and_pane(self):
        a = agent()
        route = dict(socket='/a', pane=a['pane_id'], occupant=a['terminal_id'], surface='surface1')
        with patch.object(b, 'api', side_effect=[{'snapshot': {'agents': [a]}}, {}]) as api, patch.object(b.subprocess, 'run') as run:
            b.focus(route)
        self.assertEqual(run.call_args.args[0][-1], 'surface1')
        self.assertEqual(api.call_args.args, ('/a', 'pane.focus', {'pane_id': a['pane_id']}))

    def foreground(self, title, binding=None, focused=True, frontmost='com.mitchellh.ghostty'):
        """Notify ops for a blocked agent while Ghostty's focused terminal has this title."""
        a = agent()
        a['focused'] = focused
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'binding.json').write_text(json.dumps(binding or {'socket': '/a', 'label': 'Local'}))
            def helper(payload, timeout=10):
                calls.append(payload)
                return {'frontmost': frontmost}
            with patch.object(b, 'api', return_value={'snapshot': {'agents': [a], 'workspaces': [{'workspace_id': 'w1', 'label': 'notifications'}]}}), \
                    patch.object(b, 'helper', side_effect=helper), patch.object(b.socket, 'gethostname', return_value='MacBook-Pro.local'), \
                    patch.object(b, 'ghostty_surface', return_value='any-surface'), patch.object(b, 'ghostty_title', return_value=title):
                b.reconcile('/a', path)
        return [p for p in calls if p['op'] == 'notify']

    def test_foreground_pane_does_not_notify_in_any_herdr_window(self):
        # No stored surface: a new window or tab showing this machine counts.
        self.assertEqual(self.foreground('herdr MacBook-Pro.local: notifications'), [])

    def test_alerts_unless_this_machine_is_on_screen_and_focused(self):
        self.assertTrue(self.foreground('herdr nuc: homelab'))  # Herdr window shows another machine.
        self.assertTrue(self.foreground('zsh: ~/Projects'))  # Ghostty, but not Herdr.
        self.assertTrue(self.foreground('herdr MacBook-Pro.local: x', focused=False))
        # Same machine, another space: e.g. a second local Herdr session focused elsewhere.
        self.assertTrue(self.foreground('herdr MacBook-Pro.local: bitwarden'))
        self.assertTrue(self.foreground('herdr MacBook-Pro.local: x', frontmost='com.apple.Safari'))

    def test_federated_agent_is_seen_when_its_machine_is_on_screen(self):
        federated = {'machine': 'id1', 'label': 'nuc', 'host': 'nuc', 'session': 'default', 'federated': True}
        self.assertEqual(self.foreground('herdr nuc: notifications', federated), [])
        self.assertTrue(self.foreground('herdr MacBook-Pro.local: notifications', federated))
        self.assertTrue(self.foreground('herdr nuc: homelab', federated))

    def test_routes_carry_no_surface_except_dedicated_windows(self):
        notify = self.foreground('zsh', {'socket': '/a', 'label': 'Local'})[0]
        self.assertNotIn('surface', notify['route'])
        dedicated = {'socket': '/a', 'label': 'nuc', 'host': 'nuc', 'surface': 'S1', 'managed_connection': True}
        self.assertEqual(self.foreground('herdr nuc: homelab', dedicated)[0]['route']['surface'], 'S1')

    def test_click_without_surface_focuses_a_herdr_window(self):
        a = agent()
        route = dict(socket='/a', pane=a['pane_id'], occupant=a['terminal_id'])
        with patch.object(b, 'api', side_effect=[{'snapshot': {'agents': [a]}}, {}]), \
                patch.object(b, 'herdr_surface', return_value='S9'), patch.object(b.subprocess, 'run') as run:
            b.focus(route)
        self.assertEqual(run.call_args.args[0][-1], 'S9')


class ConnectionTests(unittest.TestCase):
    def binding(self, directory):
        return dict(socket=str(Path(directory) / 'remote.sock'), surface='remote-surface',
                    host='nuc', session='homelab', connection_token='owned-token',
                    managed_connection=True, owner_pid=b.os.getpid())

    def test_connection_liveness_requires_owned_process(self):
        self.assertFalse(b.connection_alive({'host': 'nuc'}))
        for pid in (True, 0, -1, '123'):
            self.assertFalse(b.connection_alive({'managed_connection': True, 'owner_pid': pid}))
        with patch.object(b.os, 'kill', side_effect=ProcessLookupError):
            self.assertFalse(b.connection_alive({'managed_connection': True, 'owner_pid': 123}))
        self.assertTrue(b.connection_alive({'managed_connection': True, 'owner_pid': b.os.getpid()}))

    def test_reconnect_preserves_acknowledged_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)):
            binding = self.binding(directory)
            snapshot = {'snapshot': {'agents': [agent('done')]}}
            with patch.object(b, 'api', return_value=snapshot):
                b.register_connection(binding)
                state_dir = b.state_dir(binding['socket'])
                remembered = {'w1:p1': {'key': ['acknowledged'], 'disposition': 'delivered'}}
                b.atomic_json(state_dir / 'state.json', remembered)
                b.register_connection(binding)
            self.assertEqual(json.loads((state_dir / 'state.json').read_text()), remembered)

    def test_registration_requires_successful_api_probe(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)):
            binding = self.binding(directory)
            with patch.object(b, 'api', side_effect=ConnectionError('not ready')):
                with self.assertRaises(ConnectionError):
                    b.register_connection(binding)
            self.assertFalse((b.state_dir(binding['socket']) / 'binding.json').exists())

    def test_disconnect_clears_own_alerts_and_binding(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)):
            binding = self.binding(directory)
            state_dir = b.state_dir(binding['socket'])
            b.atomic_json(state_dir / 'binding.json', binding)
            b.atomic_json(state_dir / 'state.json', {'w1:p1': {'key': ['blocked']}})
            with patch.object(b, 'helper') as helper:
                b.close_connection(binding)
            self.assertFalse((state_dir / 'binding.json').exists())
            self.assertFalse((state_dir / 'state.json').exists())
            self.assertEqual(helper.call_count, 1)
            expected = b.notification_id(binding['socket'], 'w1:p1')
            self.assertEqual(helper.call_args.args[0]['ids'], [expected, 'test-' + expected])

    def test_old_disconnect_cannot_remove_new_binding(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)):
            binding = self.binding(directory)
            state_dir = b.state_dir(binding['socket'])
            b.atomic_json(state_dir / 'binding.json', dict(binding, connection_token='new-token'))
            with patch.object(b, 'helper') as helper:
                b.close_connection(binding)
            self.assertTrue((state_dir / 'binding.json').exists())
            helper.assert_not_called()

    def test_saved_machines_use_existing_config(self):
        output = 'id1\tNUC\tnuc\thomelab\tenabled\nid2\tOther\tother\tdefault\tdisabled\n'
        with patch.object(b.subprocess, 'run', return_value=type('Result', (), {'returncode': 0, 'stdout': output})()):
            self.assertEqual(b.saved_remotes(), [{'id': 'id1', 'label': 'NUC', 'host': 'nuc', 'session': 'homelab'}])


PS = '''  100     1 /opt/homebrew/bin/herdr server
  200    50 herdr
  201   200 ssh -C -F /tmp/cfg -S /tmp/hssh-501/x-%C -o BatchMode=yes -T nuc printf '\\012%s\\012' 'ready:1'\\012exec /home/s/.local/bin/herdr --session homelab remote-client-bridge --idle-timeout-v1
  300    60 /usr/bin/python3 bridge.py connect-run req
  301   300 /usr/bin/ssh -tt nuc /home/s/.local/bin/herdr --session homelab
  400   100 ssh -T other herdr remote-client-bridge
'''
NUC = {'id': 'id1', 'label': 'NUC', 'host': 'nuc', 'session': 'homelab'}


class FederationTests(unittest.TestCase):
    def test_only_local_client_bridges_count(self):
        bridges = b.federation_bridges(PS)
        self.assertEqual(len(bridges), 1)  # Not dedicated windows, not the server.
        hosts, session = bridges[0]
        self.assertIn('nuc', hosts)
        self.assertEqual(session, 'homelab')

    def test_default_session_bridge(self):
        bridges = b.federation_bridges('  1 0 herdr --session work\n  2 1 ssh -T box herdr remote-client-bridge\n')
        self.assertEqual(bridges, [({'-T', 'box', 'herdr'}, 'default')])

    def test_no_bridge_skips_machine_lookup(self):
        run = type('R', (), {'stdout': '  1 0 herdr\n', 'returncode': 0})()
        with patch.object(b.subprocess, 'run', return_value=run) as call, patch.object(b, 'saved_remotes') as saved:
            self.assertEqual(b.federated_machines(), [])
        saved.assert_not_called()
        call.assert_called_once()

    def test_session_must_match(self):
        run = type('R', (), {'stdout': PS, 'returncode': 0})()
        other = dict(NUC, id='id2', session='default')
        with patch.object(b.subprocess, 'run', return_value=run), patch.object(b, 'saved_remotes', return_value=[NUC, other]):
            self.assertEqual(b.federated_machines(), [NUC])

    def sync(self, directory, profiles, snapshots, dedicated=None):
        calls = []
        def helper(payload, timeout=10):
            calls.append((payload.get('state'), payload))
            return {'frontmost': 'browser'} if payload['op'] == 'status' else {'ok': True}
        if dedicated:
            b.atomic_json(b.state_dir('/tmp/x.sock') / 'binding.json', dedicated)
        with patch.object(b, 'federated_machines', return_value=profiles), \
                patch.object(b, 'machine_snapshot', side_effect=snapshots), patch.object(b, 'helper', side_effect=helper):
            errors = {}
            b.sync_federated(errors)
        return calls, errors

    def test_attach_baselines_then_alerts_without_windows(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)), \
                patch.object(b.subprocess, 'run') as run:
            snap = lambda *agents: {'agents': list(agents), 'workspaces': [{'workspace_id': 'w1', 'label': 'homelab'}]}
            calls, _ = self.sync(directory, [NUC], [snap(agent('done', 1))])
            self.assertFalse(any(p['op'] == 'notify' for _, p in calls))  # History is baselined.
            calls, _ = self.sync(directory, [NUC], [snap(agent('done', 2))])
            run.assert_not_called()  # No Ghostty windows, no SSH of our own.
        notify = next(p for k, p in calls if p['op'] == 'notify')
        self.assertEqual(notify['route']['machine'], 'id1')
        self.assertNotIn('socket', notify['route'])
        self.assertEqual(notify['title'], '✓ homelab · nuc')

    def test_detach_withdraws_alerts(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)):
            snap = {'agents': [agent()], 'workspaces': []}
            self.sync(directory, [NUC], [snap])
            calls, _ = self.sync(directory, [NUC], [snap])
            self.assertTrue(any(p['op'] == 'notify' for _, p in calls))
            calls, _ = self.sync(directory, [], [])
            key = b.federated_key(NUC)
            removed = [p['ids'] for _, p in calls if p['op'] == 'remove']
            self.assertIn(b.notification_id(key, 'w1:p1'), removed[0])
            self.assertFalse(list(Path(directory).glob('*/federated.json')))

    def test_unreachable_keeps_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)):
            self.sync(directory, [NUC], [{'agents': [agent()], 'workspaces': []}])
            calls, errors = self.sync(directory, [NUC], ConnectionError('down'))
            self.assertIn(b.federated_key(NUC), errors)
            self.assertFalse(calls)
            self.assertTrue(list(Path(directory).glob('*/federated.json')))

    def test_dedicated_connection_wins(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)):
            owned = dict(host='nuc', session='homelab', managed_connection=True, owner_pid=b.os.getpid(), socket='/tmp/x.sock')
            with patch.object(b, 'machine_snapshot') as snapshot:
                self.sync(directory, [NUC], snapshot, dedicated=owned)
            snapshot.assert_not_called()

    def test_federated_click_checks_occupant_then_navigates(self):
        a = agent()
        route = dict(machine='id1', pane=a['pane_id'], occupant=a['terminal_id'], surface='surface1')
        with patch.object(b, 'machine_snapshot', return_value={'agents': [a]}), patch.object(b, 'navigate') as navigate, \
                patch.object(b.subprocess, 'run') as run:
            b.focus(route)
            navigate.assert_called_once_with(route)
            self.assertEqual(run.call_args.args[0][-1], 'surface1')
            navigate.reset_mock()
            with self.assertRaises(RuntimeError):
                b.focus(dict(route, occupant='old'))
            navigate.assert_not_called()

    def test_local_click_navigates_but_dedicated_window_only_focuses(self):
        a = agent()
        route = dict(socket=b.LOCAL_SOCKET, pane=a['pane_id'], occupant=a['terminal_id'], surface='surface1')
        with patch.object(b, 'api', return_value={'snapshot': {'agents': [a]}}), patch.object(b, 'navigate') as navigate, \
                patch.object(b.subprocess, 'run'):
            b.focus(route)
        navigate.assert_called_once_with(route)


class NavigationTests(unittest.TestCase):
    """Cross-machine click navigation through a narrowed agent view and the reserved key."""

    def run_navigate(self, route, showing, remotes, focused_after=1, views=None):
        events = self.events = []
        state = {'keys': 0}
        views = views or {}
        def api(path, method, params=None):
            events.append(('local', method, params))
            if method == 'agent.view.clear':
                return views.get('local', {'active': False})
            return {}
        def remote_api(profile, method, params):
            events.append((profile['id'], method, params))
            if method == 'agent.view.clear':
                return views.get(profile['id'], {'active': False})
            return {}
        def key(surface):
            state['keys'] += 1
            events.append(('key', surface))
        def showing_local(surface):
            return showing if state['keys'] < focused_after else not showing
        with tempfile.TemporaryDirectory() as directory, patch.object(b, 'CACHE', Path(directory)), \
                patch.object(b, 'api', side_effect=api), patch.object(b, 'remote_api', side_effect=remote_api), \
                patch.object(b, 'send_navigation_key', side_effect=key), \
                patch.object(b, 'showing_local', side_effect=showing_local), \
                patch.object(b, 'federated_machines', return_value=remotes), \
                patch.object(b.Target, 'run', lambda self, *args: events.append((self.machine, 'cli') + args)), \
                patch.object(b.Target, 'focused', lambda self: state['keys'] >= focused_after), \
                patch.object(b, 'NAV_SETTLE', 0), patch.object(b, 'NAV_VERIFY', 0.05), patch.object(b.time, 'sleep'):
            b.navigate(route)
        return events

    NUC = dict(id='id1', label='NUC', host='nuc', session='homelab')
    MINI = dict(id='id2', label='Mini', host='mini', session='default')

    def test_remote_target_from_local_window_narrows_local_and_presses_key(self):
        route = dict(machine='id1', pane='w1S:p1', surface='surface1')
        events = self.run_navigate(route, showing=True, remotes=[self.NUC])
        self.assertIn(('id1', 'cli', 'agent', 'focus', 'w1S:p1'), events)
        tag = next(e for e in events if e[:3] == ('id1', 'cli', 'pane') and '--token' in e)
        nonce = tag[tag.index('--token') + 1].split('=', 1)[1]
        view = next(e for e in events if e[1] == 'agent.view.set')
        self.assertEqual(view[0], 'local')
        self.assertEqual(view[2]['filter'], {'op': 'eq', 'field': {'token': b.NAV_TOKEN}, 'value': nonce})
        self.assertEqual([e for e in events if e[0] == 'key'], [('key', 'surface1')])
        # The view and tag are cleared after the key.
        key_at = events.index(('key', 'surface1'))
        self.assertIn(('local', 'agent.view.clear', {'source': b.NAV_SOURCE}), events[key_at:])
        self.assertTrue(any(e[:3] == ('id1', 'cli', 'pane') and '--clear-token' in e for e in events[key_at:]))
        self.assertFalse(any(e[0] == 'id1' and e[1] in ('agent.view.set',) for e in events))

    def test_local_target_in_local_window_only_focuses(self):
        route = dict(socket=b.LOCAL_SOCKET, pane='w6A:p1', surface='surface1')
        events = self.run_navigate(route, showing=True, remotes=[self.NUC])
        self.assertEqual(events, [('local', 'pane.focus', {'pane_id': 'w6A:p1'})])

    def test_remote_target_shown_alone_only_focuses(self):
        route = dict(machine='id1', pane='w1S:p1', surface='surface1')
        events = self.run_navigate(route, showing=False, remotes=[self.NUC])
        self.assertEqual(events, [('id1', 'cli', 'agent', 'focus', 'w1S:p1')])

    def test_local_target_from_remote_window_narrows_every_attached_machine(self):
        route = dict(socket=b.LOCAL_SOCKET, pane='w6A:p1', surface='surface1')
        events = self.run_navigate(route, showing=False, remotes=[self.NUC, self.MINI])
        narrowed = {e[0] for e in events if e[1] == 'agent.view.set'}
        self.assertEqual(narrowed, {'local', 'id1', 'id2'})
        cleared = {e[0] for e in events[events.index(('key', 'surface1')):] if e[1] == 'agent.view.clear'}
        self.assertEqual(cleared, {'local', 'id1', 'id2'})
        self.assertIn(('local', 'pane.report_metadata', {'pane_id': 'w6A:p1', 'source': b.NAV_SOURCE,
                                                         'tokens': {b.NAV_TOKEN: None}, 'ttl_ms': 15000}), events)

    def test_retries_key_once_then_reports(self):
        route = dict(machine='id1', pane='w1S:p1', surface='surface1')
        events = self.run_navigate(route, showing=True, remotes=[self.NUC], focused_after=2)
        self.assertEqual(len([e for e in events if e[0] == 'key']), 2)
        with self.assertRaises(RuntimeError):
            self.run_navigate(route, showing=True, remotes=[self.NUC], focused_after=99)

    def test_foreign_view_is_never_overridden(self):
        route = dict(machine='id1', pane='w1S:p1', surface='surface1')
        with self.assertRaises(RuntimeError):
            self.run_navigate(route, showing=True, remotes=[self.NUC],
                              views={'local': {'active': True, 'source': 'someone-else'}})

    def test_foreign_view_skips_key_and_still_clears_tag(self):
        route = dict(machine='id1', pane='w1S:p1', surface='surface1')
        with self.assertRaises(RuntimeError):
            self.run_navigate(route, showing=True, remotes=[self.NUC],
                              views={'local': {'active': True, 'source': 'someone-else'}})
        self.assertFalse(any(e[0] == 'key' or e[1] == 'agent.view.set' for e in self.events))
        self.assertTrue(any(e[:3] == ('id1', 'cli', 'pane') and '--clear-token' in e for e in self.events))
        # Only our own source is ever cleared.
        self.assertTrue(all(e[2] == {'source': b.NAV_SOURCE} for e in self.events if e[1] == 'agent.view.clear'))


if __name__ == '__main__':
    unittest.main()
