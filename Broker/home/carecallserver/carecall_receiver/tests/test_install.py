"""Stage a complete synthetic Pi tree and verify rollback without touching host services."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from contextlib import ExitStack, redirect_stdout
from unittest.mock import Mock, patch
import io

import yaml

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'app'))
import common
spec = importlib.util.spec_from_file_location('stage_fixture', PACKAGE / 'install.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)


class StageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.target_root = self.root / 'opt/carecall-wifi-manager'
        self.target_etc = self.root / 'etc/carecall-wifi-manager'
        self.system = self.root / 'etc/systemd/system'
        self.backups = self.root / 'var/backups/carecall'
        for path in (self.target_root.parent, self.target_etc.parent, self.system, self.backups):
            path.mkdir(parents=True, exist_ok=True)
        self.stack.enter_context(patch.multiple(s, ROOT=self.target_root, ETC=self.target_etc,
                                              SYSTEM=self.system, OWNER=self.target_etc / 'owner'))
        self.stack.enter_context(patch.object(s.p, 'PARENT', self.backups))
        self.stack.enter_context(patch.object(s, 'verify_bundle'))
        self.stack.enter_context(patch.object(s.p, 'ensure_idle'))
        self.command = Mock(return_value=types.SimpleNamespace(stdout='', returncode=0))
        self.old_root = self.root / 'opt/carecall-wifi-aptrial'
        self.old_etc = self.root / 'etc/carecall-wifi-aptrial'
        self.state_path = self.root / 'state.json'
        self.result_path = self.old_etc / 'last-result.json'
        self.netplan = self.root / 'etc/netplan/50-cloud-init.yaml'
        self.original_controller = (PACKAGE / 'tests/fixtures/r4_controller.py').read_text()
        common.atomic(self.old_root / 'controller.py', self.original_controller, 0o644)
        common.atomic(self.old_etc / 'settings.json', json.dumps({'ssid': 'CareCall-Pi-fixture',
                                                               'password': 'FixtureFixedPass2026'}))
        self.candidate = {'ssid': 'fixture-router', 'psk_hex': 'a' * 64, 'hidden': False,
                          'regulatory_domain': 'KR', 'version': s.p.R4, 'test_id': 'a' * 24,
                          'purpose': 'candidate-only-not-boot-config'}
        doc = {'network': {'version': 2, 'ethernets': {'eth0': {'dhcp4': True, 'dhcp6': True, 'optional': True}},
                          'wifis': {'wlan0': {'dhcp4': True, 'optional': True, 'regulatory-domain': 'KR',
                                             'access-points': {'fixture-router': {'password': 'a' * 64}}}}}}
        common.atomic(self.netplan, yaml.safe_dump(doc))
        self.sources = {str(path): s.p.sha(path.read_bytes()) for path in
                        (self.netplan, self.old_etc / 'settings.json')}
        shared = {'mode': 'router', 'test_id': 'a' * 24, 'restored': True, 'failure': None,
                  'router_connected': True, 'candidate_saved': True, 'sources_unchanged': True,
                  'candidate_identity_match': True, 'last_attempt': 'CONNECTED',
                  'last_control_result': 'OK', 'last_wpa_state': 'COMPLETED', 'last_readiness': 'READY'}
        common.atomic(self.state_path, json.dumps(dict(shared, phase='restored', confirmed=True,
                                                       source_hashes=self.sources)))
        common.atomic(self.result_path, json.dumps(dict(shared, version=s.p.R4, result='PASS',
                                                        phone_confirmed=True, persistent_wifi_changed=False)))
        common.atomic(self.old_etc / 'tested-router-candidate.json', json.dumps(self.candidate))
        self.c = types.SimpleNamespace(ROOT=self.old_root, ETC=self.old_etc, STATE=self.state_path,
                                      RESULT=self.result_path, CARECALL=common.CARECALL,
                                      atomic=common.atomic, command=self.command,
                                      active=Mock(return_value=True), station_ready=Mock(return_value=True))
        self.router = types.SimpleNamespace(preflight=Mock(), source_hashes=lambda: dict(self.sources),
                                            NETPLAN=self.netplan, read_layout=Mock(return_value='KR'))
        self.stack.enter_context(patch.object(s.p, 'load_installed', return_value=(
            self.c, self.router, types.SimpleNamespace(check=Mock()))))
        self.prepared = self.backups / 'wifi_persist_prepare_20260929_fixture'
        self.prepared.mkdir(mode=0o700)
        proposal = yaml.safe_dump(s.p.proposal(doc, self.candidate, 'KR'))
        common.atomic(self.prepared / 'proposal-root' / s.p.NETPLAN_TARGET, proposal)
        common.atomic(self.prepared / 'proposal-root' / s.p.CLOUD_TARGET, s.p.CLOUD_DRAFT)
        common.atomic(self.prepared / 'plan.json', json.dumps({'version': s.p.VERSION,
            'status': 'PREPARED_NOT_APPLIED', 'test_id': 'a' * 24, 'candidate_matches_current_wifi': True,
            'sources': self.sources, 'proposal_netplan_sha256': s.p.sha(proposal.encode())}))
        # A real manifest is generated when packaging. Supply only its byte read here.
        self.original_read_bytes = Path.read_bytes
        def read_bytes(path):
            if path == PACKAGE / 'SHA256SUMS' and not path.exists():
                return b'fixture-manifest'
            return self.original_read_bytes(path)
        self.stack.enter_context(patch.object(Path, 'read_bytes', read_bytes))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def test_stage_preserves_network_and_installs_readable_web_code(self):
        before = {path: Path(path).read_bytes() for path in self.sources}
        with redirect_stdout(io.StringIO()) as output:
            s.stage(self.prepared)
        self.assertIn('WIFI_PERSIST_STAGE=SUCCESS', output.getvalue())
        self.assertNotIn(self.candidate['psk_hex'], output.getvalue())
        self.assertEqual(self.target_root.stat().st_mode & 0o777, 0o755)
        self.assertEqual(self.target_etc.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.target_etc / 'owner').exists())
        self.assertEqual(before, {path: Path(path).read_bytes() for path in before})
        self.assertEqual(self.command.call_args_list[0].args, ('systemctl', 'daemon-reload'))
        self.assertEqual(self.command.call_count, 1)
        self.assertEqual((self.target_etc / 'settings.json').read_bytes(),
                         (self.old_etc / 'settings.json').read_bytes())

    def test_failure_after_code_write_restores_legacy_and_removes_new_units(self):
        self.command.side_effect = [OSError('simulated reload failure'), types.SimpleNamespace(returncode=0)]
        with redirect_stdout(io.StringIO()), self.assertRaises(OSError):
            s.stage(self.prepared)
        self.assertEqual((self.old_root / 'controller.py').read_text(), self.original_controller)
        self.assertFalse(self.target_root.exists())
        self.assertFalse(self.target_etc.exists())
        self.assertEqual(list(self.system.glob('carecall-wifi-manager*.service')), [])

    def test_changed_prepared_source_refuses_before_install(self):
        common.atomic(self.netplan, 'changed')
        with self.assertRaisesRegex(s.p.PrepareError, 'SOURCE_CHANGED_SINCE_PREPARE'):
            s.stage(self.prepared)
        self.assertFalse(self.target_root.exists())
        self.command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
