import contextlib
import importlib.util
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
spec = importlib.util.spec_from_file_location('routertrial_installer', PACKAGE / 'apply_patch.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_failed_but_restored_known_router_trial_can_be_repaired_without_fake_pass(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / 'installed'
            app.mkdir()
            for name in installer.PAYLOAD:
                (app / name).write_text('known previous ' + name)
            expected = {name:hashlib.sha256((app / name).read_bytes()).hexdigest()
                        for name in installer.PAYLOAD}
            (root / 'previous_r3_hashes.json').write_text(json.dumps(expected))
            unit = root / 'router.service'
            unit.write_text('known-unit')
            report = dict(version='20260928-routertrial-3-ctrlpath', mode='router',
                          result='NOT_PASSED', restored=True, sources_unchanged=True,
                          persistent_wifi_changed=False, failure=None, phone_confirmed=False)
            with mock.patch.object(c, 'ROOT', app), \
                 mock.patch.multiple(installer, PACKAGE=root, UNIT_PATH=unit):
                self.assertEqual(installer.upgrade_basis(report), 'RESTORED_KNOWN_ROUTER_TRIAL')
                self.assertEqual(report['result'], 'NOT_PASSED')
                for key, value in (('restored', False), ('sources_unchanged', False),
                                   ('persistent_wifi_changed', True), ('failure', 'RESTORE_FAILED')):
                    with self.subTest(key=key), self.assertRaises(c.TrialError):
                        installer.upgrade_basis(dict(report, **{key: value}))
                (app / 'router.py').write_text('unrecognized edit')
                with self.assertRaises(c.TrialError):
                    installer.upgrade_basis(report)

    def fixture(self, root):
        app = root / 'app'
        app.mkdir()
        for name in ('common.py', 'controller.py', 'web.py'):
            (app / name).write_text('original-' + name)
        settings = root / 'settings.json'
        settings.write_text('private fixed setup password must remain byte-for-byte')
        result = root / 'result.json'
        result.write_text('{"result":"PASS","phone_confirmed":true,"restored":true}')
        return app, settings, result, root / 'router.service'

    def test_install_stages_code_without_start_enable_or_network_mutation(self):
        with tempfile.TemporaryDirectory() as folder:
            app, settings, result, unit = self.fixture(Path(folder))
            before = settings.read_bytes()
            calls = []
            with mock.patch.multiple(c, ROOT=app, RESULT=result), \
                 mock.patch.object(installer, 'UNIT_PATH', unit), \
                 mock.patch.object(installer, 'verify_bundle'), \
                 mock.patch.object(installer, 'preflight', return_value={'unchanged': 'hash'}), \
                 mock.patch.object(installer, 'backup'), \
                 mock.patch.object(installer.router, 'source_hashes', return_value={'unchanged': 'hash'}), \
                 mock.patch.object(installer.firewall, 'check'), \
                 mock.patch.object(c, 'station_ready', return_value=True), \
                 mock.patch.object(c, 'active', return_value=True), \
                 mock.patch.object(c, 'command', side_effect=lambda *a, **k: calls.append(a)), \
                 contextlib.redirect_stdout(io.StringIO()):
                installer.apply()
            self.assertEqual(settings.read_bytes(), before)
            self.assertEqual(calls, [('systemctl', 'daemon-reload')])
            self.assertNotIn('[Install]', unit.read_text())
            for name in installer.PAYLOAD:
                self.assertEqual((app / name).read_bytes(), (PACKAGE / 'app' / name).read_bytes())

    def test_failed_postcheck_restores_previous_code_and_removes_new_unit(self):
        with tempfile.TemporaryDirectory() as folder:
            app, settings, result, unit = self.fixture(Path(folder))
            original = {p.name: p.read_bytes() for p in app.iterdir()}
            with mock.patch.multiple(c, ROOT=app, RESULT=result), \
                 mock.patch.object(installer, 'UNIT_PATH', unit), \
                 mock.patch.object(installer, 'verify_bundle'), \
                 mock.patch.object(installer, 'preflight', return_value={'unchanged': 'hash'}), \
                 mock.patch.object(installer, 'backup'), \
                 mock.patch.object(installer.router, 'source_hashes', return_value={'changed': 'hash'}), \
                 mock.patch.object(installer.firewall, 'check'), \
                 mock.patch.object(c, 'command'), contextlib.redirect_stdout(io.StringIO()), \
                 self.assertRaisesRegex(c.TrialError, 'SOURCE_CONFIGURATION_CHANGED'):
                installer.apply()
            self.assertEqual({p.name: p.read_bytes() for p in app.iterdir()}, original)
            self.assertFalse(unit.exists())


if __name__ == '__main__':
    unittest.main()
