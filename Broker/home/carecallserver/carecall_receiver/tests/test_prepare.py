"""Synthetic fixtures only; no real Wi-Fi, Netplan, or systemd is accessed."""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import yaml

PACKAGE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('persist_prepare', PACKAGE / 'prepare.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def evidence():
    candidate = {'ssid': 'Lab: 한글 "quote" \\ test', 'psk_hex': '', 'hidden': False,
                 'version': p.R4, 'test_id': 'a' * 24, 'regulatory_domain': 'KR',
                 'purpose': 'candidate-only-not-boot-config'}
    candidate['psk_hex'] = hashlib.pbkdf2_hmac(
        'sha1', b'FakeSecret2026!', candidate['ssid'].encode(), 4096, 32).hex()
    shared = {'mode': 'router', 'test_id': 'a' * 24, 'restored': True, 'failure': None,
              'router_connected': True, 'candidate_saved': True, 'sources_unchanged': True,
              'candidate_identity_match': True, 'last_attempt': 'CONNECTED',
              'last_control_result': 'OK', 'last_wpa_state': 'COMPLETED', 'last_readiness': 'READY'}
    state = dict(shared, phase='restored', confirmed=True)
    report = dict(shared, version=p.R4, result='PASS', phone_confirmed=True,
                  persistent_wifi_changed=False)
    document = {'network': {'version': 2, 'ethernets': {'eth0': {
        'dhcp4': True, 'dhcp6': True, 'optional': True}}, 'wifis': {'wlan0': {
            'dhcp4': True, 'optional': True, 'regulatory-domain': 'KR',
            'access-points': {candidate['ssid']: {
                'auth': {'key-management': 'psk', 'password': 'FakeSecret2026!'}}}}}}}
    return state, report, candidate, document


class EvidenceTests(unittest.TestCase):
    def test_two_attempt_success_accepted_without_inventing_first_attempt_history(self):
        state, report, candidate, _ = evidence()
        state['attempts'] = report['attempts'] = 2
        p.validate_evidence(state, report, candidate)

    def test_stale_candidate_and_unfinished_trial_rejected(self):
        for target, field, value in (
            (0, 'phase', 'router_connecting'), (0, 'confirmed', False),
            (1, 'result', 'NOT_PASSED'), (1, 'version', 'old-version'),
            (1, 'persistent_wifi_changed', True), (1, 'sources_unchanged', False),
            (2, 'test_id', 'b' * 24), (2, 'purpose', 'boot-config')):
            with self.subTest(field=field):
                records = list(evidence()[:3])
                records[target][field] = value
                with self.assertRaises(p.PrepareError):
                    p.validate_evidence(*records)

    def test_identity_and_readiness_checks_cannot_be_bypassed(self):
        for field, value in (('candidate_identity_match', False), ('last_readiness', 'DEFAULT_ROUTE_MISSING'),
                             ('candidate_saved', 'True'), ('last_control_result', 'QUERY_TIMEOUT')):
            for index in (0, 1):
                with self.subTest(field=field, index=index):
                    records = list(evidence()[:3])
                    records[index][field] = value
                    with self.assertRaises(p.PrepareError):
                        p.validate_evidence(*records)


class ProposalTests(unittest.TestCase):
    def test_only_access_points_change_and_yaml_preserves_special_ssid(self):
        _, _, candidate, original = evidence()
        before = copy.deepcopy(original)
        desired = p.proposal(original, candidate, 'KR')
        self.assertEqual(original, before)
        self.assertEqual(yaml.safe_load(yaml.safe_dump(desired, allow_unicode=True)), desired)
        reconstructed = copy.deepcopy(desired)
        reconstructed['network']['wifis']['wlan0']['access-points'] = \
            before['network']['wifis']['wlan0']['access-points']
        self.assertEqual(reconstructed, before)
        self.assertNotIn('FakeSecret2026!', yaml.safe_dump(desired))

    def test_different_ssid_or_key_refused_before_any_write(self):
        for field, value in (('ssid', 'another-router'), ('psk_hex', 'f' * 64),
                             ('hidden', True), ('regulatory_domain', 'US')):
            _, _, candidate, document = evidence()
            candidate[field] = value
            with self.subTest(field=field), self.assertRaises(p.PrepareError):
                p.proposal(document, candidate, 'KR')

    def test_password_shorthand_and_raw_psk_supported(self):
        for password in ('FakeSecret2026!', None):
            _, _, candidate, document = evidence()
            ap = document['network']['wifis']['wlan0']['access-points'][candidate['ssid']]
            ap.clear()
            ap['password'] = password if password is not None else candidate['psk_hex'].upper()
            self.assertEqual(p.proposal(document, candidate, 'KR')['network']['ethernets'],
                             document['network']['ethernets'])

    def test_country_is_optional_not_assumed(self):
        _, _, candidate, document = evidence()
        candidate['regulatory_domain'] = None
        del document['network']['wifis']['wlan0']['regulatory-domain']
        self.assertNotIn('regulatory-domain', p.proposal(document, candidate, None)['network']['wifis']['wlan0'])

    def test_enterprise_or_unreviewed_options_not_silently_removed(self):
        for key, value in (('bssid', '00:11:22:33:44:55'), ('band', '5GHz'),
                           ('auth', {'key-management': 'eap', 'password': 'FakeSecret2026!'})):
            _, _, candidate, document = evidence()
            document['network']['wifis']['wlan0']['access-points'][candidate['ssid']][key] = value
            with self.subTest(key=key), self.assertRaises(p.PrepareError):
                p.proposal(document, candidate, 'KR')


class FileAndGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.candidate = evidence()[2]

    def tearDown(self):
        self.tmp.cleanup()

    def fake_generate(self, argv, timeout):
        self.assertEqual(argv[:2], ['/fake/netplan', '--root-dir'])
        self.assertEqual(timeout, 40)
        root = Path(argv[2])
        self.assertTrue(root.is_relative_to(self.directory))
        p.write_private(root / 'run/systemd/network/10-netplan-wlan0.network',
                        b'[Match]\nName=wlan0\n[Network]\nDHCP=ipv4\n')
        p.write_private(root / 'run/systemd/network/10-netplan-eth0.network',
                        b'[Match]\nName=eth0\n[Network]\nDHCP=yes\n')
        p.write_private(root / 'run/netplan/wpa-wlan0.conf',
                        ('country=KR\nnetwork={\n  ssid="fixture"\n  psk=' +
                         self.candidate['psk_hex'] + '\n}\n').encode())
        return types.SimpleNamespace(returncode=0, stdout='', stderr='')

    def test_generation_targets_private_roots_only_and_preserves_eth0(self):
        with patch.object(p, 'run', side_effect=self.fake_generate) as command:
            p.generate_offline(self.directory, b'old', b'new', self.candidate, '/fake/netplan')
        self.assertEqual(command.call_count, 2)
        self.assertEqual((self.directory / 'proposal-root' / p.CLOUD_TARGET).read_text(), p.CLOUD_DRAFT)
        for path in self.directory.rglob('*'):
            if path.is_file():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_generator_failure_does_not_expose_private_error_output(self):
        secret_error = 'password=FakeSecret2026!'
        with patch.object(p, 'run', return_value=types.SimpleNamespace(
                returncode=1, stdout=secret_error, stderr=secret_error)):
            with self.assertRaisesRegex(p.PrepareError, '^OFFLINE_NETPLAN_GENERATE_FAILED$'):
                p.generate_offline(self.directory, b'old', b'new', self.candidate, '/fake/netplan')

    def test_generated_wrong_psk_is_rejected(self):
        with patch.object(p, 'run', side_effect=self.fake_generate):
            p.generate_offline(self.directory, b'old', b'new', self.candidate, '/fake/netplan')
        path = self.directory / 'proposal-root/run/netplan/wpa-wlan0.conf'
        path.write_text(path.read_text().replace(self.candidate['psk_hex'], 'f' * 64))
        with self.assertRaisesRegex(p.PrepareError, 'GENERATED_PSK_ENCODING_MISMATCH'):
            p.validate_generated(self.directory / 'proposal-root', self.candidate)

    def test_changed_generated_eth0_is_rejected(self):
        def altered(argv, timeout):
            result = self.fake_generate(argv, timeout)
            if argv[2].endswith('proposal-root'):
                (Path(argv[2]) / 'run/systemd/network/10-netplan-eth0.network').write_bytes(b'changed')
            return result
        with patch.object(p, 'run', side_effect=altered), self.assertRaisesRegex(
                p.PrepareError, 'GENERATED_ETHERNET_CHANGED'):
            p.generate_offline(self.directory, b'old', b'new', self.candidate, '/fake/netplan')

    def test_secret_read_rejects_world_readable_and_symlink(self):
        path = self.directory / 'secret'
        p.write_private(path, b'fixture')
        self.assertEqual(p.read_regular(path, private=True), b'fixture')
        path.chmod(0o644)
        with self.assertRaisesRegex(p.PrepareError, 'SECRET_FILE_PERMISSIONS_INVALID'):
            p.read_regular(path, private=True)
        link = self.directory / 'link'
        link.symlink_to(path)
        with self.assertRaises(OSError):
            p.read_regular(link)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state, self.report, self.candidate, self.document = evidence()
        self.c = types.SimpleNamespace(STATE=self.root / 'state.json', RESULT=self.root / 'last-result.json',
                                       ETC=self.root, CARECALL=('mosquitto', 'carecall-receiver',
                                                               'carecall-telegram', 'carecall-registration'))
        self.router = types.SimpleNamespace(NETPLAN=self.root / '50-cloud-init.yaml',
                                            preflight=Mock(), read_layout=Mock(return_value='KR'))
        self.snapshot = {str(self.router.NETPLAN): yaml.safe_dump(self.document).encode()}
        self.hashes = {key: p.sha(value) for key, value in self.snapshot.items()}
        self.state['source_hashes'] = self.hashes
        for path, record in ((self.c.STATE, self.state), (self.c.RESULT, self.report),
                             (self.root / 'tested-router-candidate.json', self.candidate)):
            p.write_private(path, json.dumps(record).encode())
        self.firewall = types.SimpleNamespace(check=Mock())

    def tearDown(self):
        self.tmp.cleanup()

    def do_prepare(self, changing=False, failed=False):
        calls = [(self.hashes, self.snapshot), (self.hashes, dict(self.snapshot))]
        if changing:
            calls[-1][1][str(self.router.NETPLAN)] = b'external mutation'
        with patch.object(p, 'ensure_idle') as idle, \
                patch.object(p, 'source_snapshot', side_effect=calls), \
                patch.object(p, 'PARENT', self.root / 'backups'), \
                patch.object(p.Path, 'is_file', return_value=True), \
                patch.object(p.os, 'access', return_value=True), \
                patch.object(p, 'generate_offline', side_effect=p.PrepareError('SIMULATED_GENERATOR_FAILURE') if failed else None), \
                redirect_stdout(io.StringIO()) as output:
            try:
                p.create_plan(self.c, self.router, self.firewall)
            except p.PrepareError:
                self.output = output.getvalue()
                raise
            self.output = output.getvalue()
        return idle

    def test_success_creates_verified_private_backup_and_never_applies(self):
        originals = {path: path.read_bytes() for path in self.root.glob('*.json')}
        idle = self.do_prepare()
        self.assertEqual(idle.call_count, 2)
        self.assertEqual(self.firewall.check.call_count, 2)
        self.assertIn('WIFI_PERSIST_PREPARE=SUCCESS', self.output)
        self.assertIn('BOOT_RECOVERY_INSTALLED=NO', self.output)
        self.assertNotIn(self.candidate['ssid'], self.output)
        self.assertNotIn(self.candidate['psk_hex'], self.output)
        self.assertNotIn('FakeSecret2026!', self.output)
        self.assertEqual({path: path.read_bytes() for path in originals}, originals)
        plans = list((self.root / 'backups').glob('*/plan.json'))
        self.assertEqual(len(plans), 1)
        plan = json.loads(plans[0].read_text())
        self.assertFalse(plan['live_configuration_changed'])
        self.assertFalse(plan['automatic_apply_authorized_by_this_file'])
        self.assertEqual(stat.S_IMODE(plans[0].parent.stat().st_mode), 0o700)

    def test_source_changed_after_pass_is_rejected_before_backup(self):
        self.state['source_hashes'] = {}
        self.c.STATE.write_text(json.dumps(self.state))
        with self.assertRaisesRegex(p.PrepareError, 'SOURCE_CHANGED_SINCE_SUCCESSFUL_TRIAL'):
            self.do_prepare()
        self.assertFalse((self.root / 'backups').exists())

    def test_postcheck_mutation_prevents_success_marker(self):
        with self.assertRaisesRegex(p.PrepareError, 'LIVE_FILES_CHANGED_DURING_PREPARE'):
            self.do_prepare(changing=True)
        self.assertNotIn('WIFI_PERSIST_PREPARE=SUCCESS', self.output)
        self.assertEqual(list((self.root / 'backups').glob('*/plan.json')), [])

    def test_generate_failure_keeps_private_backup_but_no_success_plan(self):
        with self.assertRaisesRegex(p.PrepareError, 'SIMULATED_GENERATOR_FAILURE'):
            self.do_prepare(failed=True)
        self.assertEqual(len(list((self.root / 'backups').glob('*/NOT_APPLIED.txt'))), 1)
        self.assertEqual(list((self.root / 'backups').glob('*/plan.json')), [])

    def test_early_boot_rejected_without_generation_or_service_changes(self):
        with patch.object(p, 'run', return_value=types.SimpleNamespace(stdout='initializing\n')) as cmd:
            with self.assertRaisesRegex(p.PrepareError, 'BOOT_MUST_BE_COMPLETE'):
                p.ensure_idle(self.c)
        self.assertEqual(cmd.call_args.args[0], ['/usr/bin/systemctl', 'is-system-running'])


if __name__ == '__main__':
    unittest.main()
