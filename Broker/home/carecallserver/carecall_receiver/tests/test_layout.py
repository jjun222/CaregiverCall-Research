"""Regression for the actual Netplan preflight that blocked the user's install."""
import contextlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import yaml

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
import router


RUNTIME = '[Match]\nName=wlan0\n\n[Network]\nDHCP=ipv4\nLinkLocalAddressing=ipv6\n'


def document(country='KR'):
    # Invented SSID/password; the real private configuration is never collected.
    wifi = {'dhcp4': True, 'optional': True, 'access-points': {
        'fixture-router': {'auth': {'key-management': 'psk', 'password': 'fixture-secret'}}}}
    if country is not None:
        wifi['regulatory-domain'] = country
    return {'network': {'version': 2, 'ethernets': {
        'eth0': {'dhcp4': True, 'dhcp6': True, 'optional': True}}, 'wifis': {'wlan0': wifi}}}


class LayoutTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)

        def mapped(value):
            path = Path(value)
            return self.root / str(path).lstrip('/') if path.is_absolute() else path

        self.netplan = mapped('/etc/netplan/50-cloud-init.yaml')
        self.runtime = mapped('/run/systemd/network/10-netplan-wlan0.network')
        self.etc = mapped('/etc/carecall-wifi-aptrial')
        for directory in ('/lib/netplan', '/etc/netplan', '/run/netplan',
                          '/etc/cloud/cloud.cfg.d', '/etc/carecall-wifi-aptrial',
                          '/usr/sbin', '/run/systemd/network'):
            mapped(directory).mkdir(parents=True, exist_ok=True)
        self.netplan.write_text(yaml.safe_dump(document()))
        self.runtime.write_text(RUNTIME)
        (self.etc / 'settings.json').write_text('{"private":"fixture-only"}')
        for name in ('wpa_cli', 'wpa_supplicant'):
            mapped('/usr/sbin/' + name).write_text('fixture-executable')
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.multiple(router, NETPLAN=self.netplan, NETWORK=self.runtime))
        self.stack.enter_context(mock.patch.object(router, 'Path', side_effect=mapped))
        self.stack.enter_context(mock.patch.multiple(c, ETC=self.etc, RUN=mapped('/run/carecall-wifi-aptrial')))
        self.command = self.stack.enter_context(mock.patch.object(c, 'command', return_value=mock.Mock(
            stdout=json.dumps([{'ifname': 'wlan0', 'addr_info': [
                {'family': 'inet', 'scope': 'global', 'local': '192.168.0.7', 'prefixlen': 24}]}]))))

    def tearDown(self):
        self.stack.close()
        self.folder.cleanup()

    def test_real_preflight_accepts_reported_layout_without_mutating_sources(self):
        before = self.netplan.read_bytes()
        old_allowed = {'dhcp4', 'dhcp6', 'optional', 'access-points', 'renderer'}
        self.assertEqual(set(document()['network']['wifis']['wlan0']) - old_allowed, {'regulatory-domain'})
        result = router.preflight()
        self.assertEqual(result, router.source_hashes())
        self.assertEqual(self.netplan.read_bytes(), before)
        self.assertEqual(router.read_layout(), 'KR')
        self.command.assert_called_once_with('ip', '-j', '-4', 'address', 'show')

    def test_country_is_optional_and_never_hardcoded(self):
        candidate = router.credentials({'token': 'a' * 48, 'ssid': 'fixture',
                                        'password': 'fixture-password', 'hidden': False})
        for country in ('KR', 'GB', 'US', '00', None):
            with self.subTest(country=country):
                actual = router.validate_layout(document(country), RUNTIME)
                self.assertEqual(actual, country)
                output = router.supplicant_config(candidate, 'a' * 24, actual)
                if country is None:
                    self.assertNotIn('country=', output)
                else:
                    self.assertIn('\ncountry=' + country + '\nnetwork={', output)

    def test_prepare_carries_country_read_from_netplan_into_state(self):
        self.netplan.write_text(yaml.safe_dump(document('GB')))
        directory = mock.MagicMock()
        directory.is_symlink.return_value = False
        directory.stat.return_value.st_uid = 0
        with mock.patch.object(router, 'private', return_value=directory), mock.patch.object(router.os, 'chmod'):
            state = router.prepare(c.new_state())
        self.assertEqual(state['regulatory_domain'], 'GB')
        self.assertEqual(state['source_hashes'], router.source_hashes())
        self.assertTrue(all(call.args == ('ip', '-j', '-4', 'address', 'show')
                            for call in self.command.call_args_list))

    def test_invalid_country_cannot_inject_supplicant_directives(self):
        for country in ('', 'KOR', 'KR\nupdate_config=1', 'KR#', 123, {}, []):
            with self.subTest(country=repr(country)), self.assertRaisesRegex(
                    c.TrialError, '^NETPLAN_REGULATORY_DOMAIN_INVALID$'):
                router.validate_layout(document(country), RUNTIME)

    def test_unknown_options_and_non_dhcp_remain_blocked(self):
        for key, value in (('addresses', ['10.0.0.2/24']), ('routes', []),
                           ('unknown-option', True)):
            source = document()
            source['network']['wifis']['wlan0'][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(
                    c.TrialError, '^NETPLAN_WLAN0_OPTIONS_REQUIRE_REVIEW$'):
                router.validate_layout(source, RUNTIME)
        source = document()
        source['network']['wifis']['wlan0']['dhcp4'] = False
        with self.assertRaisesRegex(c.TrialError, '^NETPLAN_WLAN0_DHCP4_REQUIRED$'):
            router.validate_layout(source, RUNTIME)

    def test_runtime_mismatch_and_static_configuration_remain_blocked(self):
        cases = ((RUNTIME.replace('Name=wlan0', 'Name=eth0'), 'RUNTIME_WLAN0_MATCH_REQUIRED'),
                 (RUNTIME.replace('DHCP=ipv4', 'DHCP=no'), 'RUNTIME_IPV4_DHCP_REQUIRED'),
                 (RUNTIME + 'Address=10.0.0.2/24\n', 'RUNTIME_STATIC_ADDRESS_REQUIRES_REVIEW'),
                 (RUNTIME + '\n[Address]\nAddress=10.0.0.2/24\n', 'RUNTIME_STATIC_ADDRESS_REQUIRES_REVIEW'),
                 (RUNTIME + '\n[Route]\nGateway=10.0.0.1\n', 'RUNTIME_ROUTE_REQUIRES_REVIEW'))
        for runtime, code in cases:
            with self.subTest(code=code), self.assertRaisesRegex(c.TrialError, '^' + code + '$'):
                router.validate_layout(document(), runtime)

    def test_yaml_parse_error_does_not_echo_password(self):
        self.netplan.write_text('network: [secret-router-password: broken')
        with self.assertRaisesRegex(c.TrialError, '^NETPLAN_READ_OR_PARSE_FAILED$') as error:
            router.read_layout()
        self.assertNotIn('secret-router-password', str(error.exception))


if __name__ == '__main__':
    unittest.main()
