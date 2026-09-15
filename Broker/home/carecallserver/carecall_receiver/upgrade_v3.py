"""Guarded v2-to-v3 upgrade. Keeps bot token, current call data and recipients.

check performs no migration or service stop. install briefly pauses three services.
Rollback restores code/units and removes operator permissions, never an old DB.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import subprocess
import tempfile
import time
from install_telegram import atomic_copy, command, snapshot_database, text_hash

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/home/carecallserver/carecall_receiver')
DB = PROJECT / 'data/carecall_events.db'
UNITS = Path('/etc/systemd/system')
BACKUPS = Path('/var/backups/carecall')
CHANGED = ('guardian_store.py', 'guardian_admin.py', 'telegram_api.py',
           'telegram_worker.py', 'telegram_registration.py')
ADDED = ('delivery_gate.py', 'operator_store.py', 'operator_admin.py')
UNIT_FILES = ('carecall-telegram.service', 'carecall-registration.service')
SERVICES = ('carecall-receiver.service',) + UNIT_FILES


def run_python(code, *, cwd=PROJECT):
    args = [str(PROJECT / '.venv/bin/python'), '-B', '-c', code]
    if os.geteuid() == 0:
        args = ['runuser', '-u', 'carecallserver', '--'] + args
    return command(args, cwd=cwd, capture=True)


def match_file(path, expected, label):
    if path.is_symlink() or not path.is_file() or text_hash(path) != expected:
        raise RuntimeError(label + ': ' + path.name + '; no overwrite performed by precheck')


def check():
    manifest = json.loads((ROOT / 'v3_manifest.json').read_text())
    for name, expected in manifest['release'].items():
        match_file(ROOT / name, expected, 'Bundle file differs')
    for name, expected in manifest['v2'].items():
        match_file(PROJECT / name, expected, 'Installed v2 file differs')
    for name, expected in manifest['v2_units'].items():
        match_file(UNITS / name, expected, 'Installed service file differs')
    for name in ADDED:
        path = PROJECT / name
        if path.exists() or path.is_symlink():
            raise RuntimeError('New filename already exists: ' + name + '; inspect prior upgrade/rollback first')
    if DB.parent.is_symlink() or DB.is_symlink() or not DB.is_file():
        raise RuntimeError('Expected existing database is missing or symlinked')
    if ROOT == PROJECT or ROOT.is_relative_to(PROJECT):
        raise RuntimeError('Extract the upgrade beside the project, not inside it')
    info = json.loads(run_python("""
import importlib.metadata,json,sqlite3,sys
from contextlib import closing
from pathlib import Path
with closing(sqlite3.connect(Path('data/carecall_events.db').resolve().as_uri()+'?mode=ro',uri=True)) as c:
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    print(json.dumps({'tables':sorted(tables), 'python':list(sys.version_info[:2]),
      'paho':importlib.metadata.version('paho-mqtt'),
      'integrity':c.execute('PRAGMA quick_check').fetchone()[0],
      'recipients':c.execute('SELECT count(*) FROM notification_recipients WHERE enabled=1').fetchone()[0]}))
"""))
    required = {'call_events', 'notification_recipients', 'notification_outbox', 'guardian_invites',
                'guardian_cursor', 'guardian_replies', 'guardian_audit'}
    optional = {'sqlite_sequence', 'operator_config', 'operator_bootstrap', 'operator_actions', 'operator_state'}
    if not required <= set(info['tables']) or set(info['tables']) - required - optional:
        raise RuntimeError('Database tables differ from the reviewed v2/v3 schema')
    if info['integrity'] != 'ok' or info['python'] < [3, 12] or info['paho'] != '2.1.0':
        raise RuntimeError('Database integrity or Python/paho version does not match')
    for service in SERVICES:
        output = command(['systemctl', 'show', service, '-p', 'ActiveState', '-p', 'User',
                          '-p', 'Group', '-p', 'WorkingDirectory', '-p', 'DropInPaths'], capture=True)
        props = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        expected = {'ActiveState': 'active', 'User': 'carecallserver', 'Group': 'carecallserver',
                    'WorkingDirectory': str(PROJECT), 'DropInPaths': ''}
        if props != expected:
            raise RuntimeError('Service account, active state or override differs: ' + service)
        command(['systemctl', 'is-enabled', '--quiet', service])
    args = [str(PROJECT / '.venv/bin/python'), '-B', '-m', 'unittest', 'discover', '-s', 'telegram_tests', '-q']
    if os.geteuid() == 0:
        args = ['runuser', '-u', 'carecallserver', '--'] + args
    command(args, cwd=ROOT)
    print('ENABLED_RECIPIENTS=' + str(info['recipients']), flush=True)
    print('V3_PRECHECK=SUCCESS', flush=True)


def secure_root_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Expected root-owned mode-700 directory: ' + str(path))


def fix_owners(uid, gid):
    if DB.parent.is_symlink() or not DB.parent.is_dir():
        raise RuntimeError('Unexpected database directory')
    os.chown(DB.parent, uid, gid)
    os.chmod(DB.parent, 0o700)
    for suffix in ('', '-wal', '-shm'):
        path = Path(str(DB) + suffix)
        if path.is_symlink():
            raise RuntimeError('Unexpected database sidecar symlink')
        if path.exists():
            os.chown(path, uid, gid)
            os.chmod(path, 0o600)


def clear_operator_state():
    # Compatible with a migration that failed before all new tables/columns existed.
    run_python("""
import sqlite3
from contextlib import closing
with closing(sqlite3.connect('file:data/carecall_events.db?mode=rw',uri=True)) as c, c:
    c.execute('PRAGMA secure_delete=ON')
    c.execute('BEGIN IMMEDIATE')
    tables={r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
    for name in ('operator_config','operator_bootstrap','operator_actions','operator_state'):
        if name in tables: c.execute('DELETE FROM '+name)
    columns={r[1] for r in c.execute('PRAGMA table_info(guardian_replies)')}
    if 'admin_reply' in columns:
        c.execute("DELETE FROM guardian_replies WHERE admin_reply=1 OR dedupe_key LIKE 'bootstrap:%'")
""")


def restore_code(backup, meta):
    for name in CHANGED:
        atomic_copy(backup / name, PROJECT / name, meta['uid'], meta['gid'], meta['modes'][name])
    for name in UNIT_FILES:
        atomic_copy(backup / name, UNITS / name, 0, 0, meta['modes'][name])
    for name in ADDED:
        target = PROJECT / name
        if target.exists():
            match_file(target, meta['new_hash'][name], 'Added file changed; retained')
            target.unlink()


def start_services():
    command(['systemctl', 'daemon-reload'])
    command(['systemctl', 'start', *SERVICES])
    time.sleep(2)
    for service in SERVICES:
        command(['systemctl', 'is-active', '--quiet', service])


def install():
    if os.geteuid() != 0:
        raise RuntimeError('Run installation with sudo')
    check()
    # Inspect permissions only; do not read, copy, replace or log the bot token.
    credential_dir = Path('/etc/carecall/telegram')
    credential = credential_dir / 'bot-token'
    dir_info, info = credential_dir.lstat(), credential.lstat()
    if (not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != 0 or stat.S_IMODE(dir_info.st_mode) != 0o700
            or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600):
        raise RuntimeError('Existing bot credential permissions differ; token was not modified')
    account = pwd.getpwnam('carecallserver')
    secure_root_directory(BACKUPS)
    backup = Path(tempfile.mkdtemp(prefix='telegram_v3_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_', dir=BACKUPS))
    meta = {'uid': account.pw_uid, 'gid': account.pw_gid, 'modes': {}, 'old_hash': {}, 'new_hash': {}}
    for name in CHANGED + UNIT_FILES:
        source = (UNITS if name in UNIT_FILES else PROJECT) / name
        shutil.copy2(source, backup / name)
        meta['modes'][name] = stat.S_IMODE(source.stat().st_mode)
        meta['old_hash'][name], meta['new_hash'][name] = text_hash(source), text_hash(ROOT / name)
    for name in ADDED:
        meta['new_hash'][name] = text_hash(ROOT / name)
    (backup / 'v3_state.json').write_text(json.dumps(meta), encoding='utf-8')
    print('V3_BACKUP_DIRECTORY=' + str(backup), flush=True)
    stopped = False
    try:
        stopped = True
        command(['systemctl', 'stop', *SERVICES])
        snapshot_database(DB, backup / 'carecall_events.db')
        fix_owners(account.pw_uid, account.pw_gid)
        for name in ADDED + CHANGED:
            atomic_copy(ROOT / name, PROJECT / name, account.pw_uid, account.pw_gid, 0o640)
        run_python("from operator_store import OperatorStore; OperatorStore('data/carecall_events.db').migrate(); print('OPERATOR_SCHEMA=READY')")
        for name in UNIT_FILES:
            atomic_copy(ROOT / name, UNITS / name, 0, 0, 0o644)
        command(['systemd-analyze', 'verify', *(str(UNITS / name) for name in UNIT_FILES)])
        start_services()
    except BaseException:
        if stopped:
            subprocess.run(['systemctl', 'stop', *SERVICES])
            try:
                fix_owners(account.pw_uid, account.pw_gid)
                clear_operator_state()
                restore_code(backup, meta)
                start_services()
                print('V3_FAILURE_RECOVERY=OLD_CODE_RESTORED; current database retained', flush=True)
            except Exception as recovery_error:
                print('V3_FAILURE_RECOVERY=CHECK_REQUIRED; ' + type(recovery_error).__name__, flush=True)
        raise
    print('V3_UPGRADE=SUCCESS', flush=True)
    print('BOT_TOKEN=UNCHANGED', flush=True)
    print('EXISTING_CALLS_AND_RECIPIENTS=PRESERVED', flush=True)
    print('NEXT=run operator_admin.py setup as carecallserver (without sudo)', flush=True)


def rollback(backup):
    if os.geteuid() != 0:
        raise RuntimeError('Run rollback with sudo')
    backup = backup.resolve()
    if backup.parent != BACKUPS or not backup.name.startswith('telegram_v3_'):
        raise RuntimeError('Unexpected v3 backup directory')
    if backup.stat().st_uid != 0 or stat.S_IMODE(backup.stat().st_mode) != 0o700:
        raise RuntimeError('Backup owner or permissions differ')
    meta = json.loads((backup / 'v3_state.json').read_text())
    for name in CHANGED + UNIT_FILES:
        match_file(backup / name, meta['old_hash'][name], 'Backup file changed')
        current = (UNITS if name in UNIT_FILES else PROJECT) / name
        if current.is_symlink() or text_hash(current) not in (meta['old_hash'][name], meta['new_hash'][name]):
            raise RuntimeError('Installed file changed since upgrade: ' + name)
    for name in ADDED:
        path = PROJECT / name
        if path.exists() or path.is_symlink():
            match_file(path, meta['new_hash'][name], 'Installed added file changed')
    command(['systemctl', 'stop', *SERVICES])
    clear_operator_state()
    restore_code(backup, meta)
    start_services()
    print('V3_ROLLBACK=SUCCESS; v2 code restored; current calls/recipients/deletions retained')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install', 'rollback'))
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if args.action == 'check': check()
    elif args.action == 'install': install()
    else:
        if args.backup is None: parser.error('--backup is required')
        rollback(args.backup)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('V3_UPGRADE=CANCELLED') from None
    except Exception as exc:
        raise SystemExit('V3_UPGRADE=FAILED: ' + str(exc)) from None
