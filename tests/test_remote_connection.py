import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import remote_connection as rc


class ConnectionTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        self.cache_path = self.cache.name
        self.surface = str(uuid.uuid4())
        self.run = patch.object(rc.subprocess, 'run').start()
        self.addCleanup(patch.stopall)
        self.run.return_value = subprocess.CompletedProcess([], 0, self.surface + '\n')
        self.request = None

    def open(self, host='user@nuc', session='default'):
        self.request = rc.open_connection(host, session, '/bridge with spaces.py', self.cache_path)
        self.addCleanup(rc.shutil.rmtree, self.request['socket_dir'], True)
        return self.request

    def read(self):
        return json.loads(Path(self.request['request_file']).read_text())

    def process(self, code=0, discovery=False):
        process = Mock()
        process.returncode = code
        process.communicate.return_value = ('/home/alice\n/opt/bin/herdr\n', None)
        process.poll.return_value = None
        process.wait.return_value = code
        if discovery:
            process.poll.return_value = code
        return process

    def exercise(self, processes, ready=None, close=None):
        ready = ready or Mock()
        close = close or Mock()
        with patch.object(rc.subprocess, 'Popen', side_effect=processes) as popen, \
                patch.object(rc.time, 'sleep') as sleep:
            code = rc.run_connection(self.request['request_file'], ready, close)
        return code, popen, sleep, ready, close

    def test_validation_rejects_flags_injection_and_complex_sessions(self):
        for host in ['-v', 'a b', 'a;id', 'a$(id)', 'a@b@c', 'a\nb', 'a/b', 'a:22', '', '@host']:
            with self.subTest(host=host), self.assertRaises(ValueError):
                rc.open_connection(host, 'default', 'bridge', self.cache.name)
        for session in ['-x', '../x', 'a b', 'a;id', '', 'a\nb', 'a/b']:
            with self.subTest(session=session), self.assertRaises(ValueError):
                rc.open_connection('nuc', session, 'bridge', self.cache.name)
        self.run.assert_not_called()

    def test_valid_aliases(self):
        for host in ['nuc', 'user@nuc', 'host.example', '192.168.1.2', 'my-host']:
            rc._validate(host, 'my_session-2')

    def test_exact_new_terminal_and_launch_command(self):
        request = self.open()
        args = self.run.call_args.args[0]
        self.assertEqual(args[:3], ['/usr/bin/osascript', '-e', rc.NEW_WINDOW])
        self.assertIn('new surface configuration', args[2])
        self.assertIn('wait after command of config to true', args[2])
        self.assertIn('id of first terminal of createdWindow', args[2])
        self.assertNotIn('front', args[2])
        self.assertEqual(shlex.split(args[3]), [rc.sys.executable, '/bridge with spaces.py',
                                               'connect-run', request['request_file']])
        self.assertEqual(request['surface'], self.surface)
        self.assertEqual(self.read()['status'], 'connecting')

    def test_surface_uuid_case_is_preserved(self):
        self.run.return_value.stdout = self.surface.upper() + '\n'
        self.assertEqual(self.open()['surface'], self.surface.upper())

    def test_abandoned_transport_cleanup_is_token_scoped(self):
        request = self.open()
        request['owner_pid'] = 123
        rc._atomic(request['request_file'], request)
        binding = {'request_file': request['request_file'], 'connection_token': request['connection_token'], 'owner_pid': 123}
        rc.reap_abandoned(dict(binding, connection_token='wrong'), self.cache_path)
        self.assertTrue(Path(request['socket_dir']).exists())
        rc.reap_abandoned(binding, self.cache_path)
        self.assertEqual(self.read()['status'], 'closed')
        self.assertFalse(Path(request['socket_dir']).exists())
        self.assertEqual(self.run.call_args.args[0][:5], [rc.SSH, '-S', request['control_socket'], '-O', 'exit'])

    def test_abandoned_cleanup_refuses_foreign_paths(self):
        request = self.open()
        with self.assertRaises(RuntimeError):
            rc.reap_abandoned({'request_file': '/tmp/foreign/request.json'}, self.cache_path)
        request['owner_pid'] = 123
        request['socket_dir'] = self.cache_path
        rc._atomic(request['request_file'], request)
        with self.assertRaises(RuntimeError):
            rc.reap_abandoned({'request_file': request['request_file'], 'connection_token': request['connection_token'], 'owner_pid': 123}, self.cache_path)
        self.assertTrue(Path(self.cache_path).exists())

    def test_child_waits_for_atomic_exact_surface(self):
        request = self.open()
        pending = dict(request, surface=None, status='opening')
        rc._atomic(request['request_file'], pending)
        with patch.object(rc.time, 'sleep', side_effect=lambda _: rc._atomic(request['request_file'], request)) as sleep:
            self.assertEqual(rc._handshake(request['request_file'])['surface'], self.surface)
        sleep.assert_called_once()

    def test_private_unique_short_paths_even_with_long_cache(self):
        self.cache_path = str(Path(self.cache.name) / ('long-cache-' * 12))
        first = self.open()
        second = self.open()
        self.assertNotEqual(first['socket'], second['socket'])
        self.assertNotEqual(first['control_socket'], second['control_socket'])
        for request in (first, second):
            for key in ('socket', 'control_socket'):
                self.assertLessEqual(len(os.fsencode(request[key])), 103)
            self.assertEqual(os.stat(request['socket_dir']).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(Path(request['request_file']).parent).st_mode & 0o777, 0o700)
            self.assertEqual(os.stat(request['request_file']).st_mode & 0o777, 0o600)

    def test_failed_window_handshake_records_error_without_guessing(self):
        self.run.return_value.stdout = 'not-a-uuid'
        with self.assertRaises(ValueError):
            self.open()
        request_file = next(Path(self.cache.name).glob('connection-*/request.json'))
        request = json.loads(request_file.read_text())
        self.assertEqual(request['status'], 'error')
        self.assertFalse(Path(request['socket_dir']).exists())
        self.assertEqual(self.run.call_count, 1)

    def test_window_timeout_cleanup(self):
        self.run.side_effect = subprocess.TimeoutExpired('osascript', 30)
        with self.assertRaises(subprocess.TimeoutExpired):
            self.open()
        request = json.loads(next(Path(self.cache.name).glob('connection-*/request.json')).read_text())
        self.assertEqual(request['status'], 'error')
        self.assertFalse(Path(request['socket_dir']).exists())

    def test_child_handshake_timeout_cleanup(self):
        self.open()
        rc._atomic(self.request['request_file'], dict(self.request, surface=None, status='opening'))
        with patch.object(rc.time, 'monotonic', side_effect=[0, 31]), self.assertRaises(RuntimeError):
            rc.run_connection(self.request['request_file'], Mock(), Mock())
        self.assertEqual(self.read()['status'], 'error')
        self.assertFalse(Path(self.request['socket_dir']).exists())

    def test_ssh_arguments_binding_validation_and_normal_cleanup(self):
        self.open(session='work')
        statuses = []
        ready = Mock(side_effect=lambda binding: statuses.append(self.read()['status']))
        code, popen, _, _, close = self.exercise([self.process(discovery=True), self.process()], ready)
        self.assertEqual(code, 0)
        discovery, foreground = [c.args[0] for c in popen.call_args_list]
        self.assertEqual(discovery[-1], rc.DISCOVERY_COMMAND)
        self.assertIs(popen.call_args_list[0].kwargs['stdout'], subprocess.PIPE)
        self.assertNotIn('stderr', popen.call_args_list[0].kwargs)
        for args in (discovery, foreground):
            self.assertIn('ControlMaster=auto', args)
            self.assertIn('ControlPersist=60', args)
            self.assertIn(self.request['control_socket'], args)
            self.assertNotIn('-F', args)
            self.assertFalse(any('BatchMode' in a or 'ExitOnForwardFailure' in a for a in args))
        self.assertIn('-tt', foreground)
        self.assertIn('StreamLocalBindMask=0177', foreground)
        self.assertIn('StreamLocalBindUnlink=yes', foreground)
        self.assertIn(self.request['socket'] + ':/home/alice/.config/herdr/sessions/work/herdr.sock', foreground)
        self.assertEqual(foreground[-1], '/opt/bin/herdr --session work')
        binding = ready.call_args.args[0]
        self.assertEqual(binding, dict(surface=self.surface, socket=self.request['socket'], host='user@nuc',
                                      session='work', remote_socket='/home/alice/.config/herdr/sessions/work/herdr.sock',
                                      label='user@nuc/work', connection_token=self.request['connection_token'],
                                      request_file=self.request['request_file'], owner_pid=os.getpid(), managed_connection=True))
        self.assertEqual(statuses, ['connecting'])
        close.assert_called_once_with(binding)
        self.assertEqual(self.read()['status'], 'closed')
        self.assertFalse(Path(self.request['socket_dir']).exists())
        self.assertEqual(self.run.call_args.args[0], [rc.SSH, '-S', self.request['control_socket'], '-O', 'exit', 'user@nuc'])

    def test_remote_paths_absolute_and_quoted_binary(self):
        for output in ['/home/a\nherdr\n', '/home/a:x\n/bin/herdr\n', '/home/a\n/bin/herdr\nextra\n']:
            with self.assertRaises(ValueError):
                rc._remote_paths(output, 'default')
        self.open()
        discovery = self.process(discovery=True)
        discovery.communicate.return_value = ('/home/alice\n/opt/herdr dir/herdr\n', None)
        _, popen, _, _, _ = self.exercise([discovery, self.process()])
        self.assertEqual(popen.call_args.args[0][-1], "'/opt/herdr dir/herdr' --session default")

    def test_reconnect_preserves_token_and_persists_lifecycle(self):
        self.open()
        seen = []
        ready = Mock(side_effect=lambda b: seen.append((b['connection_token'], self.read()['status'])))
        code, _, sleep, _, close = self.exercise([
            self.process(discovery=True), self.process(255),
            self.process(discovery=True), self.process()], ready)
        self.assertEqual(code, 0)
        self.assertEqual([x[0] for x in seen], [self.request['connection_token']] * 2)
        self.assertEqual(seen[1][1], 'reconnecting')
        sleep.assert_called_once_with(3)
        close.assert_called_once()

    def test_discovery_network_failure_reconnects(self):
        self.open()
        _, popen, sleep, ready, _ = self.exercise([
            self.process(255, True), self.process(discovery=True), self.process()])
        self.assertEqual(popen.call_count, 3)
        sleep.assert_called_once_with(3)
        ready.assert_called_once()

    def test_retry_api_startup_and_check_process_alive(self):
        self.open()
        ready = Mock(side_effect=[ConnectionError('not listening'), OSError('startup'), None])
        _, _, sleep, _, _ = self.exercise([self.process(discovery=True), self.process()], ready)
        self.assertEqual(ready.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_on_ready_failure_stops_only_owned_process_and_closes(self):
        self.open()
        foreground = self.process()
        close = Mock()
        with patch.object(rc.subprocess, 'Popen', side_effect=[self.process(discovery=True), foreground]), \
                self.assertRaises(ValueError):
            rc.run_connection(self.request['request_file'], Mock(side_effect=ValueError('invalid API')), close)
        foreground.terminate.assert_called_once()
        close.assert_called_once()
        self.assertEqual(self.read()['status'], 'error')

    def test_api_timeout_is_error_and_never_ready(self):
        self.open()
        foreground = self.process()
        close = Mock()
        with patch.object(rc.subprocess, 'Popen', side_effect=[self.process(discovery=True), foreground]), \
                patch.object(rc.time, 'monotonic', side_effect=[0, 0, 21]), self.assertRaises(RuntimeError):
            rc.run_connection(self.request['request_file'], Mock(side_effect=OSError('secret')), close)
        foreground.terminate.assert_called_once()
        self.assertEqual(self.read()['status'], 'error')
        self.assertNotIn('secret', Path(self.request['request_file']).read_text())
        close.assert_called_once()

    def test_signal_detach_cleanup_and_handler_restore(self):
        for sig in (signal.SIGINT, signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=sig):
                self.open()
                old = signal.getsignal(sig)
                foreground = self.process()
                ready = Mock(side_effect=lambda b: signal.getsignal(sig)(sig, None))
                code, _, _, _, close = self.exercise([self.process(discovery=True), foreground], ready)
                self.assertEqual(code, 0)
                foreground.terminate.assert_called_once()
                close.assert_called_once()
                self.assertEqual(self.read()['status'], 'closed')
                self.assertEqual(signal.getsignal(sig), old)

    def test_cleanup_ignores_additional_terminal_shutdown_signals(self):
        self.open()
        foreground = self.process()
        def stop_again():
            self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        foreground.terminate.side_effect = stop_again
        ready = Mock(side_effect=lambda b: signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None))
        self.exercise([self.process(discovery=True), foreground], ready)
        self.assertEqual(self.read()['status'], 'closed')
        self.assertFalse(Path(self.request['socket_dir']).exists())

    def test_ready_status_is_persisted_only_after_callback_validation(self):
        self.open()
        foreground = self.process()
        seen = []
        foreground.wait.side_effect = lambda: (seen.append(self.read()['status']), 0)[1]
        self.exercise([self.process(discovery=True), foreground])
        self.assertEqual(seen, ['ready'])

    def test_ready_callback_not_called_for_dead_client(self):
        self.open()
        foreground = self.process()
        foreground.poll.return_value = 0
        _, _, _, ready, close = self.exercise([self.process(discovery=True), foreground])
        ready.assert_not_called()
        close.assert_called_once()

    def test_atomic_writes_unique_temps_and_no_secrets(self):
        self.open()
        names = []
        original = rc.os.replace
        with patch.object(rc.os, 'replace', side_effect=lambda src, dst: (names.append(src), original(src, dst))):
            rc._status(self.request, 'ready')
            rc._status(self.request, 'closed')
        self.assertNotEqual(*names)
        data = self.read()
        self.assertFalse({'password', 'credentials', 'private_key', 'stdout', 'stderr'} & data.keys())
        self.assertEqual(list(Path(self.request['request_file']).parent.iterdir()), [Path(self.request['request_file'])])


if __name__ == '__main__':
    unittest.main()
