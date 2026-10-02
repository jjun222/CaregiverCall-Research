import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
import controller
import router
import web


def request(ssid='IEEE', password='password', **values):
    return dict(token='a' * 48, ssid=ssid, password=password, hidden=False, **values)


class CredentialTests(unittest.TestCase):
    def test_known_wpa_psk_vector(self):
        candidate = router.credentials(request())
        self.assertEqual(candidate['psk_hex'], 'f42c6fc52df0ebef9ebb4b90b38a5f902e83fe1b135a70e23aed762e9710a12e')

    def test_unicode_ssid_spaces_and_symbols_are_not_trimmed_or_injected(self):
        candidate = router.credentials(request(' 연구실 "Wi-Fi" ', ' !a"\\$()9 '))
        output = router.supplicant_config(candidate, 'a' * 24)
        self.assertIn('ssid=' + ' 연구실 "Wi-Fi" '.encode().hex(), output)
        self.assertNotIn(' !a"\\$()9 ', output)
        self.assertNotIn('연구실', output)
        self.assertEqual(output.count('network={'), 1)

    def test_ssid_limit_is_utf8_bytes_and_controls_are_rejected(self):
        router.credentials(request('가' * 10 + 'ab'))
        for ssid in ('', 'a' * 33, '가' * 11, 'name\nnetwork={', 'name\x00', '\ud800'):
            with self.subTest(ssid=repr(ssid)), self.assertRaises(c.TrialError):
                router.credentials(request(ssid))

    def test_router_password_format_is_separate_from_setup_ap_password(self):
        for password in ('abcd!234', 'a' * 63, '0' * 64):
            router.credentials(request(password=password))
        for password in ('short', 'g' * 64, 'a' * 65, 'abcd\n1234', '비밀번호12345'):
            with self.subTest(password=repr(password)), self.assertRaises(c.TrialError):
                router.credentials(request(password=password))
        with self.assertRaises(c.TrialError):
            router.credentials(request(extra='unsupported'))

    def test_saved_psk_can_be_used_without_rehashing(self):
        candidate = router.credentials(request(password='AB' * 32))
        self.assertEqual(candidate['psk_hex'], 'ab' * 32)

    def test_report_does_not_pass_on_ap_connectivity_alone_or_expose_secrets(self):
        state = dict(c.new_state(), mode='router', confirmed=True, restored=True,
                     router_connected=False, candidate_saved=False, sources_unchanged=True,
                     password='RAW_SECRET', ssid='SSID_SECRET', psk_hex='PSK_SECRET')
        self.assertEqual(c.final_report(state)['result'], 'NOT_PASSED')
        state.update(router_connected=True, candidate_saved=True)
        report = c.final_report(state)
        self.assertEqual(report['result'], 'PASS')
        self.assertFalse(report['persistent_wifi_changed'])
        for secret in ('RAW_SECRET', 'SSID_SECRET', 'PSK_SECRET', state['token']):
            self.assertNotIn(secret, json.dumps(report))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.run = self.root / 'run'
        self.etc = self.root / 'etc'
        (self.run / 'private').mkdir(parents=True)
        (self.run / 'public').mkdir()
        self.etc.mkdir()
        self.state = dict(c.new_state(), mode='router', attempts=0, router_connected=False,
                          candidate_saved=False, source_hashes={'baseline': 'hash'},
                          deadline=c.now() + 900)
        self.path_patch = mock.patch.multiple(c, RUN=self.run, ETC=self.etc,
                         STATE=self.run / 'state.json', RESULT=self.etc / 'result.json',
                         AP_NETWORK=self.run / 'ap.network')
        self.path_patch.start()
        c.save_state(self.state)
        c.AP_NETWORK.write_text(c.MARKER + '[Network]\nDHCP=no\n')

    def tearDown(self):
        self.path_patch.stop()
        self.folder.cleanup()

    def fake_command(self, *args, **kwargs):
        self.calls.append(args)
        if args[:4] == ('ip', '-j', '-4', 'address'):
            return mock.Mock(stdout=json.dumps([{'addr_info': [{'family': 'inet', 'scope': 'global',
                         'local': '192.168.77.1', 'prefixlen': 24}]}]))
        return mock.Mock(stdout='', returncode=0)

    def test_success_uses_only_candidate_unit_saves_private_candidate_and_stops(self):
        self.calls = []
        candidate = router.credentials(request('PrivateRouter', 'Password!234'))
        with mock.patch.object(c, 'command', side_effect=self.fake_command), \
             mock.patch.object(c, 'active', return_value=False), \
             mock.patch.object(router, 'candidate_ready', return_value='192.168.0.0/24'):
            result = router.attempt(candidate, lambda: False)
        self.assertEqual(result, 'CONNECTED')
        saved = self.etc / 'tested-router-candidate.json'
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)
        self.assertEqual(json.loads(saved.read_text())['ssid'], 'PrivateRouter')
        self.assertNotIn('Password!234', saved.read_text())
        self.assertFalse((self.run / 'private' / 'candidate.conf').exists())
        self.assertIn(('systemctl', 'start', router.UNIT), self.calls)
        self.assertNotIn(('systemctl', 'start', c.STATION), self.calls)
        self.assertTrue(c.read_state()['candidate_saved'])
        self.assertFalse(any(a[0] in ('netplan', 'ufw') for a in self.calls))

    def test_timeout_does_not_save_candidate_and_stops_supplicant(self):
        self.calls = []
        with mock.patch.object(c, 'command', side_effect=self.fake_command), \
             mock.patch.object(c, 'active', return_value=False), mock.patch.object(router, 'CONNECT_SECONDS', 0):
            result = router.attempt(router.credentials(request()), lambda: False)
        self.assertEqual(result, 'CONNECT_TIMEOUT')
        self.assertFalse((self.etc / 'tested-router-candidate.json').exists())
        self.assertFalse(c.read_state()['router_connected'])
        self.assertIn(('systemctl', 'stop', router.UNIT), self.calls)

    def test_subnet_conflict_returns_specific_result_without_saving(self):
        self.calls = []
        with mock.patch.object(c, 'command', side_effect=self.fake_command), \
             mock.patch.object(c, 'active', return_value=False), \
             mock.patch.object(router, 'candidate_ready', side_effect=c.TrialError('ROUTER_SUBNET_CONFLICT')):
            self.assertEqual(router.attempt(router.credentials(request()), lambda: False), 'ROUTER_SUBNET_CONFLICT')
        self.assertFalse((self.etc / 'tested-router-candidate.json').exists())

    def test_wrong_identity_or_no_route_cannot_pass_candidate_readiness(self):
        def output(*args, **kwargs):
            if args[0] == 'wpa_cli':
                return mock.Mock(returncode=0, stdout='wpa_state=COMPLETED\nid_str=somebody-else\n')
            self.fail('Address checks must wait for verified candidate identity')
        with mock.patch.object(c, 'active', return_value=True), mock.patch.object(c, 'command', side_effect=output):
            self.assertIsNone(router.candidate_ready(self.state))

    def test_request_reader_rejects_symlinks_and_stale_tokens(self):
        outside = self.root / 'outside'
        outside.write_text('do-not-delete')
        path = self.run / 'public' / 'request.json'
        path.symlink_to(outside)
        with self.assertRaises(c.TrialError):
            router.take_request(self.state)
        self.assertEqual(outside.read_text(), 'do-not-delete')
        path.unlink()
        path.write_text(json.dumps(request()))
        account = mock.Mock(pw_uid=os.getuid())
        with mock.patch.object(router.pwd, 'getpwnam', return_value=account), self.assertRaises(c.TrialError):
            router.take_request(self.state)
        self.assertFalse(path.exists())

    def test_request_is_consumed_without_retaining_plaintext_password(self):
        body = request()
        body['token'] = self.state['token']
        path = self.run / 'public' / 'request.json'
        path.write_text(json.dumps(body))
        with mock.patch.object(router.pwd, 'getpwnam', return_value=mock.Mock(pw_uid=os.getuid())):
            candidate = router.take_request(self.state)
        self.assertFalse(path.exists())
        self.assertNotIn('password', candidate)

    def test_controller_exception_restores_and_never_logs_credentials(self):
        stream = io.StringIO()
        with mock.patch.object(router, 'run', side_effect=ValueError('super-secret-password')), \
             mock.patch.object(controller, 'restore_original', return_value=True) as restore, \
             mock.patch.object(controller.signal, 'signal'), contextlib.redirect_stdout(stream):
            controller.run_trial()
        restore.assert_called_once()
        self.assertNotIn('super-secret-password', stream.getvalue())
        self.assertEqual(c.read_state()['failure'], 'ValueError')

    def test_restore_stops_candidate_before_starting_original_station(self):
        calls = []
        with mock.patch.object(c, 'command', side_effect=lambda *a, **k: calls.append(a)), \
             mock.patch.object(c, 'active', return_value=False), \
             mock.patch.object(router, 'source_hashes', return_value={'baseline': 'hash'}), \
             mock.patch.object(c, 'station_ready', return_value=True):
            self.assertTrue(controller.restore_original())
        self.assertLess(calls.index(('systemctl', 'stop', router.UNIT)), calls.index(('systemctl', 'start', c.STATION)))
        self.assertTrue(c.read_state()['sources_unchanged'])

    def test_retry_returns_to_ap_then_success_finishes(self):
        candidate = router.credentials(request())
        events = []
        count = 0
        def attempt(*_):
            nonlocal count
            count += 1
            events.append('attempt')
            state = c.read_state()
            if count == 2:
                state['router_connected'] = True
                c.save_state(state)
            return 'CONNECT_TIMEOUT' if count == 1 else 'CONNECTED'
        with mock.patch.object(router, 'publish', side_effect=lambda state, result: events.append(result)), \
             mock.patch.object(router, 'take_request', return_value=candidate), \
             mock.patch.object(router, 'attempt', side_effect=attempt), \
             mock.patch.object(router.time, 'sleep'):
            router.run(lambda: events.append('ap'), lambda: False, lambda state: state['router_connected'])
        self.assertEqual(events.count('ap'), 3)
        self.assertEqual(events.count('attempt'), 2)
        self.assertTrue(c.read_state()['confirmed'])


class RouterHTTPTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.public = Path(self.folder.name)
        self.token = 'a' * 48
        self.session = {'token': self.token, 'mode': 'router', 'notice': 'READY', 'can_submit': True}
        (self.public / 'session.json').write_text(json.dumps(self.session))
        self.server = web.Server(('127.0.0.1', 0), self.public)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.headers = {'Origin': 'http://' + self.server.expected_host,
                        'Content-Type': 'application/json', 'X-CareCall-Token': self.token}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.folder.cleanup()

    def send(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, body, headers or self.headers)
        response = connection.getresponse()
        result = response.status, response.read()
        connection.close()
        return result

    def test_page_submission_private_file_and_duplicate_block(self):
        status, page = self.send('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('공유기 Wi-Fi 연결 시험'.encode(), page)
        body = json.dumps(request(password='password!23'))
        self.assertEqual(self.send('POST', '/submit', body)[0], 200)
        path = self.public / 'request.json'
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(path.read_text())['password'], 'password!23')
        self.assertEqual(self.send('POST', '/submit', body)[0], 409)
        self.assertEqual(self.send('GET', '/request.json')[0], 404)
        self.assertNotIn(b'password!23', self.send('GET', '/session')[1])
        self.assertEqual(list(self.public.glob('.request-*')), [])

    def test_invalid_and_cross_origin_requests_never_create_secret_file(self):
        self.assertEqual(self.send('POST', '/submit', json.dumps(request(password='short')))[0], 400)
        headers = dict(self.headers, Origin='http://attacker.invalid')
        self.assertEqual(self.send('POST', '/submit', json.dumps(request()), headers)[0], 403)
        self.assertFalse((self.public / 'request.json').exists())

    def test_old_ap_mode_and_completed_trial_cannot_submit_credentials(self):
        for session in ({'token': self.token}, dict(self.session, can_submit=False)):
            (self.public / 'session.json').write_text(json.dumps(session))
            self.assertEqual(self.send('POST', '/submit', json.dumps(request()))[0], 409)


if __name__ == '__main__':
    unittest.main()
