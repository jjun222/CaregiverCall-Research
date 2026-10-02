"""No real Wi-Fi changes: crash recovery uses real temporary files; networking is mocked."""
import ast
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch

import yaml

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
import manager as m
import transaction as tx
import router
import web

spec = importlib.util.spec_from_file_location('installer', PACKAGE / 'install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

CANDIDATE = {'ssid': 'Fixture 한글: "x" \\ net', 'psk_hex': 'a' * 64,
             'hidden': False, 'regulatory_domain': 'KR'}


def document():
    return {'network': {'version': 2, 'ethernets': {'eth0': {'dhcp4': True, 'dhcp6': True, 'optional': True}},
                        'wifis': {'wlan0': {'dhcp4': True, 'optional': True, 'regulatory-domain': 'KR',
                                           'access-points': {'old-fixture': {'password': 'OldFakePassword!'}}}}}}


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.multiple(tx, NETPLAN=self.root / 'netplan.yaml',
                                              CLOUD=self.root / 'cloud.cfg', PROFILE=self.root / 'profile.json',
                                              JOURNAL=self.root / 'transaction.json'))
        self.stack.enter_context(patch.object(tx, 'cloud_policy_check'))
        self.original = {'netplan': b'old-netplan\n', 'profile': b'{"ssid":"old-fixture"}\n', 'cloud': None}
        for key, path in tx.targets().items():
            if self.original[key] is not None:
                c.atomic(path, self.original[key].decode())

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def assert_original(self):
        for key, path in tx.targets().items():
            self.assertEqual(path.read_bytes() if path.exists() else None, self.original[key])

    def test_power_loss_after_each_file_write_restores_all_originals(self):
        atomic = c.atomic
        for crash_after in range(1, 5):
            with self.subTest(crash_after=crash_after):
                if tx.JOURNAL.exists():
                    tx.JOURNAL.unlink()
                count = 0
                def crash(path, data, mode=0o600):
                    nonlocal count
                    atomic(path, data, mode)
                    count += 1
                    if count == crash_after:
                        raise SystemExit('simulated power loss')
                with patch.object(c, 'atomic', side_effect=crash), self.assertRaises(SystemExit):
                    tx.begin(CANDIDATE, 'new-netplan\n', 'boot-fixture')
                self.assertTrue(tx.recover())
                self.assert_original()
                self.assertFalse(tx.recover())

    def test_committed_transaction_survives_restart(self):
        tx.begin(CANDIDATE, 'new-netplan\n', 'boot-fixture')
        tx.commit()
        self.assertFalse(tx.recover())
        self.assertEqual(tx.NETPLAN.read_text(), 'new-netplan\n')
        self.assertEqual(tx.obj(tx.PROFILE)['ssid'], CANDIDATE['ssid'])
        self.assertEqual(tx.obj(tx.PROFILE)['committed_boot_id'], 'boot-fixture')

    def test_external_change_is_not_overwritten_during_recovery(self):
        tx.begin(CANDIDATE, 'new-netplan\n', 'boot-fixture')
        c.atomic(tx.NETPLAN, 'administrator-change')
        with self.assertRaisesRegex(c.TrialError, 'TRANSACTION_EXTERNAL_FILE_CHANGE'):
            tx.recover()
        self.assertEqual(tx.NETPLAN.read_text(), 'administrator-change')

    def test_pending_transaction_blocks_another_writer(self):
        tx.begin(CANDIDATE, 'new-netplan\n', 'boot-fixture')
        with self.assertRaisesRegex(c.TrialError, 'PENDING_TRANSACTION'):
            tx.begin(CANDIDATE, 'other', 'other-boot')

    def test_commit_rejects_changed_profile(self):
        tx.begin(CANDIDATE, 'new-netplan\n', 'boot-fixture')
        c.atomic(tx.PROFILE, '{}')
        with self.assertRaisesRegex(c.TrialError, 'FILES_CHANGED_DURING_COMMIT'):
            tx.commit()

    def test_root_private_permissions(self):
        tx.begin(CANDIDATE, 'new-netplan\n', 'boot-fixture')
        for path in list(tx.targets().values()) + [tx.JOURNAL]:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.real_path = Path
        def mapped(value):
            value = Path(value)
            return self.root / str(value).lstrip('/') if value.is_absolute() else value
        self.mapped = mapped
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(tx, 'Path', side_effect=mapped))
        self.stack.enter_context(patch.multiple(tx, NETPLAN=mapped('/etc/netplan/50-cloud-init.yaml'),
                                              CLOUD=mapped('/etc/cloud/cloud.cfg.d/99-carecall-disable-network-config.cfg')))
        c.atomic(tx.NETPLAN, yaml.safe_dump(document()))
        c.atomic(mapped('/proc/cmdline'), 'console=fixture')
        c.atomic(mapped('/etc/cloud/cloud.cfg'), 'users: [default]\n')
        tx.CLOUD.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_only_wifi_access_point_changes(self):
        old = yaml.safe_load(tx.NETPLAN.read_text())
        updated = yaml.safe_load(tx.document(CANDIDATE))
        self.assertEqual(updated['network']['ethernets'], old['network']['ethernets'])
        self.assertEqual(updated['network']['wifis']['wlan0']['regulatory-domain'], 'KR')
        self.assertEqual(set(updated['network']['wifis']['wlan0']['access-points']), {CANDIDATE['ssid']})
        self.assertEqual(yaml.safe_load(tx.NETPLAN.read_text()), old)

    def test_country_change_rejected(self):
        with self.assertRaisesRegex(c.TrialError, 'COUNTRY_CHANGED'):
            tx.document(dict(CANDIDATE, regulatory_domain='US'))

    def test_duplicate_netplan_file_rejected(self):
        c.atomic(tx.NETPLAN.parent / '99-other.yaml', 'network: {version: 2}')
        with self.assertRaisesRegex(c.TrialError, 'NETPLAN_LAYOUT_CHANGED'):
            tx.document(CANDIDATE)

    def test_unsafe_candidate_strings_rejected(self):
        for change in ({'ssid': 'x\nctrl_interface=evil'}, {'psk_hex': 'not-a-key'}, {'hidden': 'yes'}):
            with self.subTest(change=change), self.assertRaises(c.TrialError):
                tx.validate_candidate(dict(CANDIDATE, **change))

    def test_cloud_policy_can_be_added_without_disabling_other_cloud_modules(self):
        tx.cloud_policy_check()
        value = yaml.safe_load(tx.CLOUD_TEXT)
        self.assertEqual(value, {'network': {'config': 'disabled'}})

    def test_kernel_override_is_rejected(self):
        c.atomic(self.mapped('/proc/cmdline'), 'network-config=ENCODED')
        with self.assertRaisesRegex(c.TrialError, 'KERNEL_NETWORK_POLICY_CONFLICT'):
            tx.cloud_policy_check()

    def test_later_cloud_override_and_custom_merge_are_rejected(self):
        later = tx.CLOUD.parent / 'zz-other.cfg'
        for value, expected in (('network: {config: enabled}', 'LATER_CLOUD_NETWORK_POLICY_CONFLICT'),
                                ('merge_how: []', 'CUSTOM_CLOUD_MERGE_REQUIRES_REVIEW')):
            c.atomic(later, value)
            with self.subTest(value=value), self.assertRaisesRegex(c.TrialError, expected):
                tx.cloud_policy_check()


class IdentityTests(unittest.TestCase):
    def test_hex_quoted_escapes_unicode_and_literal_hex_ssid(self):
        self.assertEqual(m.decode_ssid('414243'), b'ABC')
        self.assertEqual(m.decode_ssid('"414243"'), b'414243')
        self.assertEqual(m.decode_ssid('"A \\"quote\\" \\\\ 끝"'), 'A "quote" \\ 끝'.encode())
        self.assertEqual(m.decode_ssid('"\\xed\\x95\\x9c"'), '한'.encode())
        self.assertIsNone(m.decode_ssid('FAIL'))
        self.assertIsNone(m.decode_ssid('"bad\\q"'))

    def test_saved_readiness_checks_ssid_address_and_wlan0_default(self):
        address = json.dumps([{'addr_info': [{'family': 'inet', 'scope': 'global',
                                            'local': '192.168.0.7', 'prefixlen': 24}]}])
        routes = json.dumps([{'dev': 'wlan0', 'gateway': '192.168.0.1'}])
        def active(unit):
            return unit == c.STATION
        with patch.object(c, 'active', side_effect=active), \
                patch.object(m, 'wpa_query', side_effect=['wpa_state=COMPLETED\nid=0', CANDIDATE['ssid'].encode().hex()]), \
                patch.object(c, 'command', side_effect=[types.SimpleNamespace(stdout=address), types.SimpleNamespace(stdout=routes)]) as cmd:
            self.assertTrue(m.saved_ready(CANDIDATE))
        self.assertEqual(cmd.call_args.args, ('ip', '-j', '-4', 'route', 'show', 'default'))

    def test_same_ip_on_wrong_wifi_not_accepted(self):
        with patch.object(c, 'active', side_effect=lambda unit: unit == c.STATION), \
                patch.object(m, 'wpa_query', side_effect=['wpa_state=COMPLETED\nid=0', '"other"']), \
                patch.object(c, 'command') as cmd:
            self.assertFalse(m.saved_ready(CANDIDATE))
        cmd.assert_not_called()

    def test_default_on_other_interface_not_accepted(self):
        address = json.dumps([{'addr_info': [{'family': 'inet', 'scope': 'global',
                                            'local': '192.168.0.7', 'prefixlen': 24}]}])
        with patch.object(c, 'active', side_effect=lambda unit: unit == c.STATION), \
                patch.object(m, 'wpa_query', side_effect=['wpa_state=COMPLETED\nid=0', CANDIDATE['ssid'].encode().hex()]), \
                patch.object(c, 'command', side_effect=[types.SimpleNamespace(stdout=address),
                    types.SimpleNamespace(stdout='[{"dev":"eth0","gateway":"192.168.0.1"}]')]):
            self.assertFalse(m.saved_ready(CANDIDATE))


class ApplyTests(unittest.TestCase):
    def test_failed_persistent_connection_rolls_back_before_reopening_ap(self):
        order = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(m, 'state_update'))
            stack.enter_context(patch.object(m, 'validate_render', return_value='rendered'))
            stack.enter_context(patch.object(m, 'boot_id', return_value='boot'))
            stack.enter_context(patch.object(tx, 'begin', return_value=CANDIDATE))
            stack.enter_context(patch.object(m, 'start_saved', side_effect=lambda **_: order.append('start')))
            stack.enter_context(patch.object(m, 'wait_saved', return_value=False))
            stack.enter_context(patch.object(tx, 'recover', side_effect=lambda: order.append('recover')))
            stack.enter_context(patch.object(c, 'atomic'))
            stack.enter_context(patch.object(m, 'open_ap', side_effect=lambda _: order.append('ap')))
            self.assertFalse(m.apply_candidate(CANDIDATE))
        self.assertEqual(order, ['start', 'recover', 'start', 'ap'])

    def test_commit_occurs_only_after_saved_connection_verified(self):
        order = []
        with ExitStack() as stack:
            for name in ('state_update', 'start_saved'):
                stack.enter_context(patch.object(m, name))
            stack.enter_context(patch.object(m, 'validate_render', return_value='rendered'))
            stack.enter_context(patch.object(m, 'boot_id', return_value='boot'))
            stack.enter_context(patch.object(tx, 'begin', return_value=CANDIDATE))
            stack.enter_context(patch.object(m, 'wait_saved', side_effect=lambda _: order.append('verified') or True))
            stack.enter_context(patch.object(tx, 'commit', side_effect=lambda: order.append('commit')))
            stack.enter_context(patch.object(c, 'atomic'))
            self.assertTrue(m.apply_candidate(CANDIDATE))
        self.assertEqual(order, ['verified', 'commit'])

    def test_post_commit_error_is_not_reported_as_rollback(self):
        with ExitStack() as stack:
            for name in ('state_update', 'start_saved'):
                stack.enter_context(patch.object(m, name))
            stack.enter_context(patch.object(m, 'validate_render', return_value='rendered'))
            stack.enter_context(patch.object(m, 'boot_id', return_value='boot'))
            stack.enter_context(patch.object(tx, 'begin', return_value=CANDIDATE))
            stack.enter_context(patch.object(m, 'wait_saved', return_value=True))
            stack.enter_context(patch.object(tx, 'commit'))
            rollback = stack.enter_context(patch.object(tx, 'recover'))
            stack.enter_context(patch.object(c, 'atomic', side_effect=OSError('disk-full-fixture')))
            with self.assertRaises(OSError):
                m.apply_candidate(CANDIDATE)
            rollback.assert_not_called()


class UnitsAndUiTests(unittest.TestCase):
    def test_boot_recovery_does_not_wait_for_network_online(self):
        unit = installer.units()[m.UNIT]
        self.assertNotIn('network-online.target', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertIn('WatchdogSec=120s', unit)
        self.assertIn('Restart=always', unit)
        self.assertIn('NotifyAccess=main', unit)

    def test_child_units_stop_with_manager_and_preserve_private_tmp(self):
        for name, unit in installer.units().items():
            self.assertIn('PrivateTmp=yes', unit)
            if name != m.UNIT:
                self.assertIn('BindsTo=' + m.UNIT, unit)
        self.assertIn('User=carecall-wifi-web', installer.units()['carecall-wifi-manager-web.service'])

    def test_old_trial_mutations_are_gated_but_status_is_available(self):
        source = (PACKAGE / 'tests/fixtures/r4_controller.py').read_text()
        changed = installer.guard_legacy(source)
        ast.parse(changed)
        self.assertIn("action not in ('status', 'show-setup')", changed)
        self.assertIn('WIFI_MANAGER_OWNS_NETWORK', changed)

    def test_phone_ui_requires_explicit_save_after_test(self):
        self.assertIn('이 Wi-Fi로 저장하고 연결', web.ROUTER_PAGE)
        self.assertIn('저장된 Wi-Fi로 다시 연결', web.ROUTER_PAGE)
        self.assertNotIn('아직 변경하지 않습니다', web.ROUTER_PAGE)
        self.assertNotIn('15분', web.ROUTER_PAGE)

    def test_cross_origin_and_wrong_token_still_rejected(self):
        host, token = '192.168.77.1:8080', 'a' * 48
        self.assertTrue(web.valid_confirmation(host, 'http://' + host, token, {'token': token}, token, host))
        self.assertFalse(web.valid_confirmation(host, 'https://other.invalid', token, {'token': token}, token, host))
        self.assertFalse(web.valid_confirmation(host, 'http://' + host, 'b' * 48, {'token': token}, token, host))


class LoopDone(Exception):
    pass


class BootAndPhoneFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch.multiple(m, REQUEST=self.root / 'request.json',
                                              SETUP_REQUEST=self.root / 'setup-request', stopping=False))
        self.stack.enter_context(patch.object(m.signal, 'signal'))
        self.stack.enter_context(patch.object(m, 'notify'))
        self.stack.enter_context(patch.object(m, 'boot_id', return_value='new-boot'))
        self.state = {'phase': 'starting', 'attempts': 0}
        self.stack.enter_context(patch.object(m, 'setup_runtime'))
        self.stack.enter_context(patch.object(c, 'read_state', side_effect=lambda: dict(self.state)))
        self.stack.enter_context(patch.object(m, 'state_update', side_effect=self.update))
        self.recover = self.stack.enter_context(patch.object(tx, 'recover', return_value=False))
        self.stack.enter_context(patch.object(tx, 'obj', return_value=copy.deepcopy(CANDIDATE)))
        self.start = self.stack.enter_context(patch.object(m, 'start_saved'))
        self.wait = self.stack.enter_context(patch.object(m, 'wait_saved', return_value=False))
        self.stack.enter_context(patch.object(m, 'pause'))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def update(self, **values):
        self.state.update(values)
        return dict(self.state)

    def test_missing_router_at_boot_opens_ap_without_requiring_carecall_or_internet(self):
        with patch.object(m, 'open_ap', side_effect=LoopDone) as ap, self.assertRaises(LoopDone):
            m.main_loop()
        ap.assert_called_once_with()
        self.start.assert_called_once_with(regenerate=False)

    def test_boot_after_interrupted_write_regenerates_restored_configuration(self):
        self.recover.return_value = True
        with patch.object(c, 'atomic'), patch.object(tx, 'sync_directory'), \
                patch.object(m, 'open_ap', side_effect=LoopDone), self.assertRaises(LoopDone):
            m.main_loop()
        self.start.assert_called_once_with(regenerate=True)

    def test_failed_input_retry_and_explicit_save_flow(self):
        order = []
        def open_ap(notice='READY', pending=False):
            order.append((notice, pending))
            self.update(phase='setup_ap')
        with patch.object(m, 'open_ap', side_effect=open_ap), \
                patch.object(m, 'confirmed', side_effect=[False, False, True]), \
                patch.object(router, 'take_request', side_effect=[dict(CANDIDATE), dict(CANDIDATE)]), \
                patch.object(m, 'trial', side_effect=['CONNECT_TIMEOUT', 'CONNECTED']), \
                patch.object(c, 'active', return_value=True), \
                patch.object(m, 'apply_candidate', side_effect=LoopDone) as apply, self.assertRaises(LoopDone):
            m.main_loop()
        self.assertEqual(order, [('READY', False), ('CONNECT_TIMEOUT', False), ('CONNECTED', True)])
        apply.assert_called_once_with(CANDIDATE)

    def test_connected_candidate_is_not_saved_without_phone_confirmation(self):
        rounds = 0
        def pause(_):
            nonlocal rounds
            rounds += 1
            if rounds > 3:
                raise LoopDone()
        def open_ap(notice='READY', pending=False):
            self.update(phase='setup_ap')
        with patch.object(m, 'open_ap', side_effect=open_ap), \
                patch.object(m, 'confirmed', return_value=False), \
                patch.object(router, 'take_request', side_effect=[dict(CANDIDATE), None, None, None]), \
                patch.object(m, 'trial', return_value='CONNECTED'), \
                patch.object(c, 'active', return_value=True), patch.object(m, 'pause', side_effect=pause), \
                patch.object(m, 'apply_candidate') as apply, self.assertRaises(LoopDone):
            m.main_loop()
        apply.assert_not_called()

    def test_power_loss_after_commit_does_not_reapply_old_initial_request(self):
        c.atomic(m.REQUEST, '{}')
        committed = dict(CANDIDATE, committed_boot_id='old-boot')
        with patch.object(tx, 'obj', return_value=committed), \
                patch.object(m, 'open_ap', side_effect=LoopDone), \
                patch.object(m, 'apply_candidate') as apply, self.assertRaises(LoopDone):
            m.main_loop()
        self.start.assert_called_once_with()
        apply.assert_not_called()

    def test_reconnect_saved_button_does_not_overwrite_credentials(self):
        def open_ap(*args, **kwargs):
            self.update(phase='setup_ap')
        self.wait.side_effect = [False, False]
        calls = 0
        def confirm():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise LoopDone()
            return True
        with patch.object(m, 'open_ap', side_effect=open_ap) as ap, \
                patch.object(m, 'confirmed', side_effect=confirm), \
                patch.object(m, 'apply_candidate') as apply, self.assertRaises(LoopDone):
            m.main_loop()
        self.assertEqual(ap.call_args.args, ('SAVED_UNAVAILABLE',))
        apply.assert_not_called()


if __name__ == '__main__':
    unittest.main()
