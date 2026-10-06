from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

from support import PACKAGE, c, fw, up, address, route, FakeUFW


class UpdateTests(unittest.TestCase):
    """Actual stage, directory exchange and recovery; simulated system services."""
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.ufw = FakeUFW()
        self.ufw.install(self.stack, self.root)
        paths = dict(ROOT=self.root / 'old-app', ETC=c.ETC, RUN=c.RUN,
                     DEPLOY=self.root / 'deploy', SYSTEM=self.root / 'systemd',
                     BACKUPS=self.root / 'backups', UFW_ETC=self.root / 'ufw')
        self.stack.enter_context(patch.multiple(up, **paths))
        for path in (up.ETC, up.RUN, up.SYSTEM, up.UFW_ETC, up.SYSTEM / 'multi-user.target.wants'):
            path.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PACKAGE / 'tests/fixtures/persist1', up.ROOT)
        for name, value in json.loads((PACKAGE / 'tests/fixtures/units.json').read_text()).items():
            up.atomic(up.SYSTEM / name, value.encode(), 0o644)
        for name in ('user.rules', 'user6.rules'):
            up.atomic(up.UFW_ETC / name, b'fixture firewall disk file\n')
        self.targets = {'netplan': self.root / 'netplan.yaml', 'cloud': self.root / 'cloud.cfg',
                        'profile': up.ETC / 'profile.json'}
        for path in self.targets.values():
            up.write_obj(path, {'ssid': 'fixture', 'committed_boot_id': 'previous-boot'})
        up.atomic(up.ETC / 'settings.json', b'{"password":"FixtureOnlyPassword"}\n')
        up.atomic(up.ETC / 'owner', b'20260929-persist-1\n')
        self.units_active = {up.UNIT, 'avahi-daemon.service'} | {n + '.service' for n in c.CARECALL}
        self.service_calls = []
        self.fail_new_start = False
        self.old = self.fake_manager('20260929-persist-1', types.SimpleNamespace(check=fw.base.check))
        self.new = self.fake_manager(up.VERSION, fw)
        self.stack.enter_context(patch.object(up, 'load_app', side_effect=self.load_app))
        self.stack.enter_context(patch.object(up, 'systemctl', side_effect=self.systemctl))
        self.stack.enter_context(patch.object(up, 'wait_online', side_effect=self.wait_online))
        self.write_state(self.old.c.VERSION)

    def fake_manager(self, version, firewall):
        common = types.SimpleNamespace(VERSION=version, ETC=up.ETC, CARECALL=c.CARECALL, PREFIX=c.PREFIX,
                                       read_state=lambda: up.obj(up.RUN / 'state.json'), command=self.ip_command)
        tx = types.SimpleNamespace(PROFILE=self.targets['profile'], JOURNAL=up.ETC / 'transaction.json',
                                   obj=up.obj, targets=lambda: self.targets,
                                   validate_candidate=Mock())
        return types.SimpleNamespace(c=common, tx=tx, OWNER=up.ETC / 'owner',
                                     REQUEST=up.ETC / 'activation-request.json', saved_ready=Mock(return_value=True),
                                     firewall=firewall)

    def ip_command(self, *args):
        self.assertEqual(args[:3], ('ip', '-j', '-4'))
        return types.SimpleNamespace(stdout=json.dumps(address() if args[3] == 'address' else route()))

    def load_app(self, path):
        text = (path / 'common.py').read_text()
        return self.new if "VERSION = '" + up.VERSION + "'" in text else self.old

    def write_state(self, version):
        up.write_obj(up.RUN / 'state.json', {'version': version, 'phase': 'online', 'link_ready': True})

    def systemctl(self, *args, **kwargs):
        self.service_calls.append(args)
        output, code = '', 0
        if args[0] == 'is-active':
            code = 0 if args[-1] in self.units_active else 3
        elif args[0] == 'show':
            output = '' if args[3] == 'DropInPaths' else str(up.SYSTEM / args[1])
        elif args[0] == 'stop':
            self.units_active.discard(args[1])
        elif args[0] == 'start':
            self.assertEqual(args[1], up.UNIT)
            manager = self.load_app(up.ROOT)
            if manager is self.new and self.fail_new_start:
                raise up.UpdateError('SIMULATED_MANAGER_START_FAILED')
            if manager is self.new:
                self.assertEqual(up.read(up.dropin()).decode(), up.DROPIN_TEXT)
                fw.reconcile(fw.ORIGINAL)
            else:
                self.assertFalse(up.dropin().exists())
                fw.base.check()
            self.write_state(manager.c.VERSION)
            self.units_active.add(up.UNIT)
        return subprocess.CompletedProcess(args, code, stdout=output, stderr='')

    def wait_online(self, version, seconds=115):
        state = up.obj(up.RUN / 'state.json')
        up.require(up.UNIT in self.units_active and state['version'] == version and state['phase'] == 'online',
                   'SIMULATED_ONLINE_TIMEOUT')

    def assert_old_restored(self):
        up.verify_tree(up.ROOT, json.loads((PACKAGE / 'previous_hashes.json').read_text())['app'])
        self.assertFalse(up.dropin().exists())
        self.assertFalse(fw.state_path().exists())
        self.assertFalse((up.DEPLOY / 'pending').exists())
        fw.base.check()
        up.check_protected(up.obj(up.DEPLOY / 'plan.json'))

    def test_stage_is_read_only_for_live_app_services_and_rules(self):
        before = {p.name: p.read_bytes() for p in up.ROOT.iterdir()}
        up.stage()
        self.assertEqual(before, {p.name: p.read_bytes() for p in up.ROOT.iterdir()})
        self.assertFalse(any(x[0] in ('stop', 'start', 'enable', 'daemon-reload') for x in self.service_calls))
        self.assertEqual(self.ufw.operations, [])
        self.assertEqual(up.obj(up.DEPLOY / 'result.json')['phase'], 'STAGED')
        up.stage()
        self.assertEqual(up.obj(up.DEPLOY / 'result.json')['phase'], 'STAGED')

    def test_unrecognized_live_source_is_rejected_before_staging(self):
        with (up.ROOT / 'manager.py').open('a') as stream:
            stream.write('\n# changed\n')
        with self.assertRaisesRegex(up.UpdateError, 'APP_CHANGED'):
            up.stage()
        self.assertFalse(up.DEPLOY.exists())
        self.assertFalse(self.ufw.operations)

    def test_extra_firewall_rule_is_rejected_before_staging(self):
        self.ufw.records.append('C unexpected-chain -')
        with self.assertRaises(c.TrialError):
            up.stage()
        self.assertFalse(up.DEPLOY.exists())

    def test_existing_dropin_prevents_unreviewed_combination(self):
        up.dropin().parent.mkdir()
        with self.assertRaisesRegex(up.UpdateError, 'DROPIN_DIRECTORY'):
            up.stage()

    def test_success_changes_complete_code_tree_preserves_wifi_and_lab_rules(self):
        up.stage()
        rules = list(self.ufw.records)
        with patch.object(fw, 'current_network', return_value=fw.ORIGINAL):
            up.perform_update()
        self.assertEqual(up.obj(up.DEPLOY / 'result.json')['phase'], 'COMMITTED')
        self.assertFalse((up.DEPLOY / 'pending').exists())
        self.assertEqual(self.ufw.records, rules)
        self.assertFalse(self.ufw.operations)
        up.check_protected(up.obj(up.DEPLOY / 'plan.json'))
        up.verify_tree(up.ROOT, up.obj(up.DEPLOY / 'plan.json')['app_hashes'])
        up.verify_tree(up.DEPLOY / 'next', up.obj(up.DEPLOY / 'plan.json')['previous']['app'])
        self.assertTrue((up.SYSTEM / up.GUARD_UNIT).exists())

    def test_new_manager_start_failure_restores_old_code_and_online_service(self):
        up.stage()
        self.fail_new_start = True
        with self.assertRaisesRegex(up.UpdateError, 'UPDATE_ROLLED_BACK'):
            up.perform_update()
        self.assert_old_restored()
        self.assertIn(up.UNIT, self.units_active)
        self.assertEqual(up.obj(up.DEPLOY / 'result.json')['phase'], 'ROLLED_BACK')

    def test_wifi_change_after_stage_is_rejected_without_switching_code(self):
        up.stage()
        self.targets['netplan'].write_text('changed independently\n')
        with self.assertRaisesRegex(up.UpdateError, 'CONFIGURATION_CHANGED'):
            up.perform_update()
        self.assertFalse(up.dropin().exists())
        self.assertEqual(up.obj(up.DEPLOY / 'result.json')['phase'], 'REJECTED_BEFORE_ACTIVATION')
        self.assertIn(up.UNIT, self.units_active)

    def test_boot_recovery_before_directory_exchange(self):
        up.stage()
        with patch.object(up, 'exchange', side_effect=SystemExit('power loss')):
            with self.assertRaises(SystemExit):
                up.perform_update()
        self.units_active.discard(up.UNIT)
        up.recover_boot()
        self.assert_old_restored()
        self.assertEqual(up.obj(up.DEPLOY / 'result.json')['phase'], 'ROLLED_BACK_AFTER_REBOOT')

    def test_boot_recovery_after_atomic_exchange(self):
        up.stage()
        real_exchange = up.exchange
        def interrupt(first, second):
            real_exchange(first, second)
            raise SystemExit('power loss')
        with patch.object(up, 'exchange', side_effect=interrupt):
            with self.assertRaises(SystemExit):
                up.perform_update()
        up.recover_boot()
        self.assert_old_restored()

    def test_boot_after_durable_commit_keeps_new_version(self):
        up.stage()
        real_unlink = Path.unlink
        def interrupt(path, *args, **kwargs):
            if path == up.DEPLOY / 'pending':
                raise SystemExit('power loss')
            return real_unlink(path, *args, **kwargs)
        with patch.object(fw, 'current_network', return_value=fw.ORIGINAL), patch.object(Path, 'unlink', interrupt):
            with self.assertRaises(SystemExit):
                up.perform_update()
        self.assertEqual(up.obj(up.DEPLOY / 'result.json')['phase'], 'COMMITTED')
        up.recover_boot()
        self.assertFalse((up.DEPLOY / 'pending').exists())
        up.verify_tree(up.ROOT, up.obj(up.DEPLOY / 'plan.json')['app_hashes'])

    def test_activate_queues_independent_nonblocking_systemd_job(self):
        up.stage()
        with patch.object(up, 'command') as command:
            up.activate()
        args = command.call_args.args
        self.assertIn('--no-block', args)
        self.assertIn('--property=Type=oneshot', args)
        self.assertEqual(args[-2:], (str(up.DEPLOY / 'apply_patch.py'), 'run'))
        self.assertFalse(up.dropin().exists())


if __name__ == '__main__':
    unittest.main()
