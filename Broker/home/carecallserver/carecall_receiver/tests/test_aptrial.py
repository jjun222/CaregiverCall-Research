import contextlib
import http.client
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import common as c
import controller
import web


class LogicTests(unittest.TestCase):
    def test_deadline_uses_monotonic_time(self):
        self.assertFalse(c.deadline_due({'deadline': 180}, 179.99))
        self.assertTrue(c.deadline_due({'deadline': 180}, 180))

    def test_configs_are_ap_scoped_and_dhcp_only(self):
        network, hostapd, dhcp = c.configs({'ssid': 'CareCall-Pi-abc', 'password': '1234567890abcdef'})
        self.assertIn('Name=wlan0', network)
        self.assertIn('DHCP=no', network)
        self.assertIn('wpa=2', hostapd)
        self.assertIn('rsn_pairwise=CCMP', hostapd)
        self.assertIn('port=0', dhcp)
        self.assertIn('interface=wlan0', dhcp)
        self.assertNotIn('/etc/netplan', network + hostapd + dhcp)

    def test_report_contains_no_setup_secrets(self):
        state = dict(c.new_state(), restored=True, confirmed=True, password='PASSWORD_SECRET')
        report = c.final_report(state)
        self.assertEqual(report['result'], 'PASS')
        text = json.dumps(report)
        self.assertNotIn(state['token'], text)
        self.assertNotIn('PASSWORD_SECRET', text)
        state['restored'] = False
        self.assertEqual(c.final_report(state)['result'], 'NOT_PASSED')

    def test_setup_failure_still_attempts_restore_without_secret_log(self):
        state = c.new_state()
        stream = io.StringIO()
        with (mock.patch.object(controller, 'setup_ap', side_effect=ValueError('secret-router-password')),
             mock.patch.object(controller, 'restore_original', return_value=True) as restore,
             mock.patch.object(c, 'read_state', return_value=state),
             mock.patch.object(c, 'save_state'), mock.patch.object(controller.signal, 'signal'),
             contextlib.redirect_stdout(stream)):
            controller.run_trial()
        restore.assert_called_once()
        self.assertNotIn('secret-router-password', stream.getvalue())
        self.assertEqual(state['failure'], 'ValueError')

    def test_guard_stops_stuck_worker_before_restore(self):
        state = dict(c.new_state(), deadline=0)
        events = []
        with (mock.patch.object(c, 'read_state', return_value=state),
             mock.patch.object(c, 'command', side_effect=lambda *a, **k: events.append(a)),
             mock.patch.object(controller, 'restore_original', side_effect=lambda: events.append(('restore',)) or True)):
            controller.guard()
        self.assertEqual(events[0][:3], ('systemctl', 'stop', c.PREFIX + 'test.service'))
        self.assertEqual(events[1], ('restore',))

    def test_guard_retries_after_stop_timeout(self):
        state = dict(c.new_state(), deadline=0)
        with (mock.patch.object(c, 'read_state', return_value=state),
              mock.patch.object(c, 'command', side_effect=[TimeoutError(), None]) as command,
              mock.patch.object(controller, 'restore_original', return_value=True) as restore,
              mock.patch.object(controller.time, 'sleep'), contextlib.redirect_stdout(io.StringIO())):
            controller.guard()
        self.assertEqual(command.call_count, 2)
        restore.assert_called_once()

    def test_restore_keeps_original_files_and_stops_only_trial_units(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ap_file = root / '00-carecall-wifi-aptrial.network'
            original = root / '10-netplan-wlan0.network'
            original.write_text('original-settings-must-remain')
            ap_file.write_text(c.MARKER + 'temporary')
            state = dict(c.new_state(), confirmed=True)
            calls = []
            with (mock.patch.multiple(c, RUN=root, AP_NETWORK=ap_file, RESULT=root / 'result.json'),
                 mock.patch.object(c, 'read_state', return_value=state),
                 mock.patch.object(c, 'save_state', side_effect=lambda s: state.update(s)),
                 mock.patch.object(c, 'command', side_effect=lambda *a, **k: calls.append(a)),
                 mock.patch.object(c, 'station_ready', return_value=True)):
                self.assertTrue(controller.restore_original())
            self.assertFalse(ap_file.exists())
            self.assertEqual(original.read_text(), 'original-settings-must-remain')
            stops = [a[2] for a in calls if a[:2] == ('systemctl', 'stop')]
            self.assertTrue(all(name.startswith(c.PREFIX) for name in stops))
            self.assertTrue(state['restored'])

    def test_unrecognized_runtime_file_is_not_removed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ap_file = root / 'network'
            ap_file.write_text('someone-elses-config')
            state = c.new_state()
            with (mock.patch.multiple(c, RUN=root, AP_NETWORK=ap_file),
                 mock.patch.object(c, 'read_state', return_value=state),
                 mock.patch.object(c, 'save_state'), mock.patch.object(c, 'command')):
                with self.assertRaises(c.TrialError):
                    controller.restore_original()
            self.assertEqual(ap_file.read_text(), 'someone-elses-config')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.public = Path(self.temp.name)
        self.token = 'a' * 48
        (self.public / 'session.json').write_text(json.dumps({'token': self.token}))
        self.server = web.Server(('127.0.0.1', 0), self.public)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = self.server.expected_host

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=3)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        data = response.read()
        result = response.status, data
        connection.close()
        return result

    def test_real_http_page_and_same_origin_confirmation(self):
        status, data = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn('CareCall'.encode(), data)
        status, data = self.request('GET', '/session')
        self.assertEqual(json.loads(data)['token'], self.token)
        body = json.dumps({'token': self.token})
        headers = {'Content-Type': 'application/json', 'Origin': 'http://' + self.host,
                   'X-CareCall-Token': self.token}
        self.assertEqual(self.request('POST', '/confirm', body, headers)[0], 200)
        self.assertEqual((self.public / 'confirmed').read_text(), self.token)

    def test_cross_origin_wrong_token_and_wrong_host_are_rejected(self):
        body = json.dumps({'token': self.token})
        headers = {'Content-Type': 'application/json', 'Origin': 'http://attacker.invalid',
                   'X-CareCall-Token': self.token}
        self.assertEqual(self.request('POST', '/confirm', body, headers)[0], 403)
        headers['Origin'] = 'http://' + self.host
        headers['X-CareCall-Token'] = 'wrong'
        self.assertEqual(self.request('POST', '/confirm', body, headers)[0], 403)
        self.assertEqual(self.request('GET', '/', headers={'Host': 'attacker.invalid'})[0], 403)
        self.assertFalse((self.public / 'confirmed').exists())


if __name__ == '__main__':
    unittest.main()
