"""Control-path, readiness and current-versus-previous result regressions."""
import contextlib
import errno
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import common as c
import controller
import router


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.state = dict(c.new_state(), mode='router')
        self.reply = 'wpa_state=COMPLETED\nid_str=carecall-' + self.state['test_id'] + '\n'
        self.addresses = [{'addr_info': [{'family': 'inet', 'scope': 'global',
                                         'local': '*가림*', 'prefixlen': 24}]}]
        self.routes = [{'dev': 'wlan0', 'gateway': '*가림*'}]

    def command(self, *args, **kwargs):
        if args[0] == 'wpa_cli':
            # Model the PrivateTmp failure: the daemon cannot resolve an
            # unspecified/default client reply path from another service.
            if '-s' not in args or args[args.index('-s') + 1] != str(router.private()):
                return mock.Mock(returncode=1, stdout="'STATUS' command timed out.\n")
            return mock.Mock(returncode=0, stdout=self.reply)
        if args[:4] == ('ip', '-j', '-4', 'address'):
            return mock.Mock(returncode=0, stdout=json.dumps(self.addresses))
        if args[:4] == ('ip', '-j', '-4', 'route'):
            routes = self.routes
            if 'dev' in args:
                selected = args[args.index('dev') + 1]
                # iproute2 print_route suppresses RTA_OIF/"dev" for oifmask=-1.
                # Realistic filtered output: gateway is present, dev is absent.
                routes = [{key:value for key,value in route.items() if key != 'dev'}
                          for route in routes if route.get('dev') == selected]
            return mock.Mock(returncode=0, stdout=json.dumps(routes))
        raise AssertionError('Unexpected command')

    def readiness(self):
        with mock.patch.object(c, 'active', return_value=True), \
             mock.patch.object(c, 'command', side_effect=self.command):
            return router.candidate_ready(self.state)

    def test_shared_reply_path_allows_completed_candidate_to_pass(self):
        old_query = self.command('wpa_cli', '-p', str(router.private() / 'ctrl'),
                                 '-i', 'wlan0', 'status')
        self.assertNotEqual(old_query.returncode, 0)
        self.assertEqual(self.readiness(), '192.168.0.0/24')
        self.assertEqual(self.state['last_control_result'], 'OK')
        self.assertTrue(self.state['candidate_identity_match'])
        self.assertEqual(self.state['last_readiness'], 'READY')

    def test_connected_other_network_cannot_pass(self):
        self.reply = 'wpa_state=COMPLETED\nid_str=another-network\n'
        self.assertIsNone(self.readiness())
        self.assertFalse(self.state['candidate_identity_match'])
        self.assertEqual(self.state['last_readiness'], 'CANDIDATE_ID_MISMATCH')

    def test_filtered_route_json_omits_dev_but_unfiltered_readiness_succeeds(self):
        filtered = json.loads(self.command('ip', '-j', '-4', 'route', 'show',
                                          'default', 'dev', 'wlan0').stdout)
        self.assertEqual(filtered, [{'gateway': '192.168.0.1'}])
        self.assertFalse(any(route.get('dev') == 'wlan0' for route in filtered))
        self.assertEqual(self.readiness(), '192.168.0.0/24')
        self.assertEqual(self.state['last_readiness'], 'READY')

    def test_default_route_on_another_interface_cannot_pass(self):
        self.routes = [{'dev': 'eth0', 'gateway': '192.168.0.1'}]
        self.assertIsNone(self.readiness())
        self.assertEqual(self.state['last_readiness'], 'DEFAULT_ROUTE_MISSING')

    def test_missing_device_or_off_subnet_gateway_stays_rejected(self):
        for route in ({'gateway': '192.168.0.1'},
                      {'dev': 'wlan0', 'gateway': '192.168.9.1'},
                      {'dev': 'wlan0'}):
            with self.subTest(route=route):
                self.routes = [route]
                self.assertIsNone(self.readiness())
                self.assertEqual(self.state['last_readiness'], 'DEFAULT_ROUTE_MISSING')

    def test_multiple_default_routes_accept_only_matching_wlan0(self):
        self.routes = [{'dev': 'eth0', 'gateway': '10.1.0.1'},
                       {'dev': 'wlan0', 'gateway': '192.168.0.1'}]
        self.assertEqual(self.readiness(), '192.168.0.0/24')

    def test_completed_authentication_still_requires_address_and_route(self):
        self.addresses = []
        self.assertIsNone(self.readiness())
        self.assertEqual(self.state['last_readiness'], 'IPV4_MISSING')
        self.addresses = [{'addr_info': [{'family': 'inet', 'scope': 'global',
                                         'local': '192.168.0.7', 'prefixlen': 24}]}]
        self.routes = []
        self.assertIsNone(self.readiness())
        self.assertEqual(self.state['last_readiness'], 'DEFAULT_ROUTE_MISSING')

    def test_control_timeout_is_not_authentication_failure_or_success(self):
        with mock.patch.object(c, 'active', return_value=True), \
             mock.patch.object(c, 'command', side_effect=subprocess.TimeoutExpired('wpa_cli', 5)):
            self.assertIsNone(router.candidate_ready(self.state))
        self.assertEqual(self.state['last_control_result'], 'QUERY_TIMEOUT')
        self.assertEqual(self.state['last_wpa_state'], 'UNAVAILABLE')
        self.assertIsNone(self.state['candidate_identity_match'])

    def test_unrecognized_output_cannot_pass_or_leak_into_state(self):
        self.reply = 'ssid=secret-router-name\nwpa_state=UNTRUSTED_SECRET_VALUE\n'
        self.assertIsNone(self.readiness())
        self.assertEqual(self.state['last_control_result'], 'INVALID_STATUS')
        for secret in ('secret-router-name', 'UNTRUSTED_SECRET_VALUE'):
            self.assertNotIn(secret, json.dumps(self.state))

    def test_status_distinguishes_current_trial_from_previous_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            result = Path(folder) / 'last-result.json'
            result.write_text(json.dumps(dict(c.final_report(dict(c.new_state(),
                mode='router', restored=True, confirmed=True)), result='NOT_PASSED')))
            self.state.update(phase='router_connecting', last_wpa_state='COMPLETED',
                              last_control_result='OK', ssid='secret-name', password='secret-key')
            stream = io.StringIO()
            with mock.patch.object(c, 'RESULT', result), \
                 mock.patch.object(c, 'read_state', return_value=self.state), \
                 mock.patch.object(c, 'active', return_value=True), \
                 mock.patch.object(controller, 'check_firewall'), contextlib.redirect_stdout(stream):
                controller.status()
            output = stream.getvalue()
            self.assertIn('CURRENT_WPA_STATE=COMPLETED', output)
            self.assertIn('CURRENT_CONTROL_RESULT=OK', output)
            self.assertIn('LAST_REPORT_IS_CURRENT_TRIAL=False', output)
            self.assertIn('LAST_RESULT=NOT_PASSED', output)
            self.assertNotIn('secret-name', output)
            self.assertNotIn('secret-key', output)
            self.assertNotIn(self.state['token'], output)


class DatagramVisibilityTests(unittest.TestCase):
    def test_request_can_arrive_even_when_reply_path_is_unreachable(self):
        # Exercise real Linux pathname datagram sockets. Renaming the client
        # endpoint models an unreachable pathname, not systemd mount namespaces.
        # It proves why receiving STATUS alone does not guarantee a reply.
        with tempfile.TemporaryDirectory(prefix='ccipc-') as folder:
            root = Path(folder)
            with contextlib.ExitStack() as resources:
                try:
                    server = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
                    client = resources.enter_context(socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM))
                except OSError as exc:
                    if exc.errno in (errno.EPERM, errno.EACCES, errno.EAFNOSUPPORT):
                        self.skipTest('Runtime does not permit AF_UNIX datagram sockets')
                    raise
                server.settimeout(1)
                client.settimeout(1)
                server.bind(str(root / 'server'))
                endpoint = root / 'client'
                client.bind(str(endpoint))
                client.sendto(b'STATUS', str(root / 'server'))
                request, reply_path = server.recvfrom(128)
                self.assertEqual(request, b'STATUS')
                endpoint.rename(root / 'hidden-client')
                with self.assertRaises(FileNotFoundError):
                    server.sendto(b'wpa_state=COMPLETED', reply_path)
                (root / 'hidden-client').rename(endpoint)
                server.sendto(b'wpa_state=COMPLETED', reply_path)
                self.assertEqual(client.recv(128), b'wpa_state=COMPLETED')


if __name__ == '__main__':
    unittest.main()
