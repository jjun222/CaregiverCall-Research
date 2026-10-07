"""Run: python3 -B -m unittest discover -s tests -v (no production DB access)."""
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import device_registry as registry


def populate(path):
    with closing(sqlite3.connect(path)) as c, c:
        c.executescript((ROOT / "tests/legacy_schema.sql").read_text(encoding="utf-8"))
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA user_version=17")
        c.execute("""INSERT INTO call_events VALUES
            ('button01-0000000000000001-00000001',1,'button01','call',1,100,
             'carecall/v1/devices/button01/call','{}','before','before',3)""")
        c.execute("INSERT INTO notification_recipients(device_id,chat_id,enabled,created_at) VALUES('button01',12345,1,'before')")
        c.execute("INSERT INTO notification_recipients(device_id,chat_id,enabled,created_at) VALUES('button01',67890,0,'before')")
        c.execute("""INSERT INTO notification_outbox
            (event_id,device_id,chat_id,created_at)
            VALUES('button01-0000000000000001-00000001','button01',12345,'before')""")
        c.execute("""INSERT INTO confirmation_calls(event_id,device_id,boot_order,sequence)
            VALUES('button01-0000000000000001-00000001','button01','0000000000000001',1)""")
        c.execute("""INSERT INTO confirmation_latest
            VALUES('button01','button01-0000000000000001-00000001')""")
        c.execute("""INSERT INTO guardian_invites
            (token_hash,device_id,label,created_at,expires_at,state,chat_id)
            VALUES('fixture-token-hash','button01','fixture',1,2,'approved',12345)""")
        c.execute("INSERT INTO operator_config VALUES(1,12345,'fixture-generation',1)")
        c.execute("INSERT INTO operator_actions VALUES('fixture-nonce','home','',9999)")
        c.execute("""INSERT INTO operator_handover
            (singleton,request_id,owner_generation,token_hash,label,created_at,expires_at)
            VALUES(1,'fixture-request','fixture-generation','fixture-hash','fixture',1,9999)""")


def legacy_snapshot(path):
    with closing(sqlite3.connect(path)) as c:
        schemas = c.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
                            "WHERE name NOT LIKE 'carecall_%' "
                            "AND tbl_name NOT LIKE 'carecall_%' ORDER BY name").fetchall()
        rows = {}
        for kind, name, _, _ in schemas:
            if kind == 'table':
                rows[name] = sorted(c.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr)
        return schemas, rows, c.execute("PRAGMA user_version").fetchone()


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'events.db'
        populate(self.db)

    def test_migration_preserves_all_existing_tables_rows_and_user_version(self):
        before = legacy_snapshot(self.db)
        self.assertEqual(registry.initialize(self.db), 'installed')
        self.assertEqual(before, legacy_snapshot(self.db))
        status = registry.status(self.db)
        self.assertEqual(status['devices'][0]['device_id'], 'button01')
        self.assertEqual(status['devices'][0]['state'], 'legacy')
        self.assertFalse(status['additional_button_calls_enabled'])

    def test_repeat_apply_does_not_reset_entries(self):
        registry.initialize(self.db)
        registry.stage_device(self.db, 'button02', 'set01')
        before = registry.status(self.db)
        self.assertEqual(registry.initialize(self.db), 'already_installed')
        self.assertEqual(before, registry.status(self.db))

    def test_twenty_devices_are_unique_and_new_devices_stay_staged(self):
        registry.initialize(self.db)
        for i in range(2, 21):
            self.assertEqual(registry.stage_device(self.db, f'button{i:02}', 'set01'), 'staged')
        rows = registry.status(self.db)['devices']
        self.assertEqual(len(rows), 20)
        self.assertEqual(len({r['mqtt_client_id'] for r in rows}), 20)
        self.assertTrue(all(r['state'] == 'staged' for r in rows[1:]))

    def test_device_cannot_silently_move_between_sets(self):
        registry.initialize(self.db)
        registry.add_set(self.db, 'set02', 'Second set')
        registry.stage_device(self.db, 'button02', 'set01')
        with self.assertRaisesRegex(registry.RegistryError, 'ANOTHER_SET'):
            registry.stage_device(self.db, 'button02', 'set02')
        with self.assertRaisesRegex(registry.RegistryError, 'ALREADY_ASSIGNED'):
            registry.initialize(self.db, 'set02')

    def test_repeat_registration_is_idempotent(self):
        registry.initialize(self.db)
        registry.stage_device(self.db, 'button02', 'set01')
        self.assertEqual(registry.stage_device(self.db, 'button02', 'set01'), 'already_registered')
        self.assertEqual(registry.stage_device(self.db, 'button01', 'set01'), 'already_registered')
        self.assertEqual(registry.add_set(self.db, 'set01', 'CareCall set01'), 'already_registered')
        with self.assertRaisesRegex(registry.RegistryError, 'DIFFERENT_LABEL'):
            registry.add_set(self.db, 'set01', 'Changed label')

    def test_missing_database_is_not_created(self):
        path = self.db.parent / 'missing.db'
        with self.assertRaises(registry.RegistryError):
            registry.initialize(path)
        self.assertFalse(path.exists())

    def test_unknown_set_and_invalid_ids_do_not_write(self):
        registry.initialize(self.db)
        before = registry.status(self.db)
        for value in ('', 'x' * 33, 'button/02', '+', '#', '한글', "x';--", 'x\n'):
            with self.subTest(value=value), self.assertRaises(registry.RegistryError):
                registry.stage_device(self.db, value, 'set01')
        with self.assertRaisesRegex(registry.RegistryError, 'SET_NOT_REGISTERED'):
            registry.stage_device(self.db, 'button02', 'missing')
        self.assertEqual(before, registry.status(self.db))

    def test_partial_registry_stops_without_changing_legacy(self):
        with closing(sqlite3.connect(self.db)) as c:
            c.execute(registry.DDL[0])
        before = legacy_snapshot(self.db)
        with self.assertRaisesRegex(registry.RegistryError, 'PARTIAL_REGISTRY'):
            registry.initialize(self.db)
        self.assertEqual(before, legacy_snapshot(self.db))

    def test_failure_during_ddl_rolls_back_all_new_tables(self):
        before = legacy_snapshot(self.db)
        with patch.object(registry, 'DDL', registry.DDL[:2] + ('INVALID SQL',)):
            with self.assertRaises(sqlite3.Error):
                registry.initialize(self.db)
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'carecall_%'").fetchone()[0], 0)
        self.assertEqual(before, legacy_snapshot(self.db))

    def test_existing_other_device_data_requires_explicit_migration(self):
        with closing(sqlite3.connect(self.db)) as c, c:
            c.execute("INSERT INTO notification_recipients(device_id,chat_id,enabled,created_at) VALUES('button02',22222,1,'before')")
        before = legacy_snapshot(self.db)
        with self.assertRaisesRegex(registry.RegistryError, 'NON_LEGACY_DATA'):
            registry.initialize(self.db)
        self.assertEqual(before, legacy_snapshot(self.db))

    def test_status_does_not_initialize_registry(self):
        before = legacy_snapshot(self.db)
        self.assertEqual(registry.status(self.db)['registry'], 'not_installed')
        self.assertEqual(before, legacy_snapshot(self.db))
        with closing(sqlite3.connect(self.db)) as c:
            self.assertEqual(c.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'carecall_%'").fetchone()[0], 0)

    def test_sql_constraints_prevent_phase1_activation(self):
        registry.initialize(self.db)
        registry.stage_device(self.db, 'button02', 'set01')
        with closing(registry.connect(self.db)) as c:
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE carecall_devices SET state='legacy' WHERE device_id='button02'")
            with self.assertRaises(sqlite3.IntegrityError):
                c.execute("UPDATE carecall_devices SET mqtt_username='button01' WHERE device_id='button02'")


if __name__ == '__main__':
    unittest.main()
