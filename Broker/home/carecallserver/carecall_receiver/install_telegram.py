"""Install this exact release with source guards and a reversible code backup.

No package installation, MQTT credential changes, automatic Telegram sends,
call deletion, or database snapshot restoration is performed by this tool.
"""
from __future__ import annotations
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/home/carecallserver/carecall_receiver')
UNIT = Path('/etc/systemd/system/carecall-telegram.service')
DATABASE = PROJECT / 'data/carecall_events.db'
BACKUP_ROOT = Path('/var/backups/carecall')
RUNTIME_FILES = ('event_database.py', 'notification_store.py', 'telegram_api.py',
                 'telegram_worker.py', 'configure_telegram.py', 'telegram_status.py')

def text_hash(path):
    # Permit CRLF/LF differences introduced by Windows transfers, not code changes.
    text = path.read_text(encoding='utf-8-sig')
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def command(args, *, cwd=None, capture=False):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=capture)
    if result.returncode:
        raise RuntimeError('Command failed: ' + ' '.join(str(x) for x in args))
    return result.stdout.strip() if capture else ''

def receiver_command(code):
    return command(['runuser', '-u', 'carecallserver', '--', str(PROJECT / '.venv/bin/python'),
                    '-B', '-c', code], cwd=PROJECT, capture=True)

def atomic_copy(source, destination, uid, gid, mode=0o640):
    fd, name = tempfile.mkstemp(prefix='.carecall-install-', dir=destination.parent)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(source.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
        info = os.stat(name)
        if (info.st_uid, info.st_gid) != (uid, gid):
            os.chown(name, uid, gid)
        os.chmod(name, mode)
        os.replace(name, destination)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def snapshot_database(source, destination):
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(destination)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise RuntimeError('Database backup integrity check failed')

def read_manifest():
    return json.loads((ROOT / 'release_manifest.json').read_text())

def check():
    manifest = read_manifest()
    for name, expected in manifest['release'].items():
        file = ROOT / name
        if not file.is_file() or text_hash(file) != expected:
            raise RuntimeError('Release file mismatch: ' + name)
    for name, expected in manifest['original'].items():
        file = PROJECT / name
        if file.is_symlink() or not file.is_file() or text_hash(file) != expected:
            raise RuntimeError('Current source differs from reviewed upload: ' + name)
    if UNIT.exists() or UNIT.is_symlink():
        raise RuntimeError('Telegram unit already exists; do not repeat initial installation')
    for name in RUNTIME_FILES[1:]:
        if (PROJECT / name).exists() or (PROJECT / name).is_symlink():
            raise RuntimeError('New file name already exists: ' + name)
    if not DATABASE.is_file() or DATABASE.is_symlink():
        raise RuntimeError('Expected existing database is unavailable')
    schema_code = ("import json,sqlite3; "
                   "c=sqlite3.connect('file:data/carecall_events.db?mode=ro',uri=True); "
                   "print(json.dumps([r[0] for r in c.execute(\"SELECT name FROM sqlite_schema WHERE type='table'\")])); c.close()")
    if os.geteuid() == 0:
        names = set(json.loads(receiver_command(schema_code)))
    else:
        names = set(json.loads(command([str(PROJECT / '.venv/bin/python'), '-B', '-c', schema_code],
                                       cwd=PROJECT, capture=True)))
    if 'call_events' not in names or names & {'notification_outbox', 'notification_recipients'}:
        raise RuntimeError('Database is not the reviewed pre-Telegram schema')
    state = command(['systemctl', 'show', 'carecall-receiver.service', '--no-pager',
                     '-p', 'User', '-p', 'Group', '-p', 'WorkingDirectory', '-p', 'ActiveState'], capture=True)
    props = dict(line.split('=', 1) for line in state.splitlines() if '=' in line)
    if (props.get('User') != 'carecallserver' or props.get('Group') != 'carecallserver'
            or props.get('WorkingDirectory') != str(PROJECT) or props.get('ActiveState') != 'active'):
        raise RuntimeError('Receiver service settings differ from reviewed configuration')
    python = str(PROJECT / '.venv/bin/python')
    tests = [python, '-B', '-m', 'unittest', 'discover', '-s', 'telegram_tests', '-v']
    if os.geteuid() == 0:
        tests = ['runuser', '-u', 'carecallserver', '--'] + tests
    command(tests, cwd=ROOT)
    print('PRECHECK=SUCCESS', flush=True)

def fix_database_file_owners(uid, gid):
    # A root read-only SQLite inspection may have created shared-memory sidecars.
    for suffix in ('', '-wal', '-shm'):
        path = Path(str(DATABASE) + suffix)
        if path.is_symlink():
            raise RuntimeError('Unexpected database symlink')
        if path.exists():
            os.chown(path, uid, gid)

def rollback(backup):
    if os.geteuid() != 0:
        raise RuntimeError('Use sudo for rollback')
    backup = backup.resolve()
    if not backup.is_relative_to(BACKUP_ROOT):
        raise RuntimeError('Unexpected backup location')
    meta = json.loads((backup / 'install_state.json').read_text())
    current = text_hash(PROJECT / 'event_database.py')
    if current not in {meta['old_event_hash'], meta['new_event_hash']}:
        raise RuntimeError('event_database.py changed after installation; inspect before rollback')
    # Preserve all subsequently received events and notification audit rows.
    command(['systemctl', 'stop', 'carecall-telegram.service'])
    command(['systemctl', 'disable', 'carecall-telegram.service'])
    command(['systemctl', 'stop', 'carecall-receiver.service'])
    try:
        atomic_copy(backup / 'event_database.py', PROJECT / 'event_database.py',
                    meta['uid'], meta['gid'], meta['mode'])
    finally:
        command(['systemctl', 'start', 'carecall-receiver.service'])
    print('ROLLBACK=SUCCESS')
    print('DATABASE_RESTORED=False; current events and outbox records are preserved')
    print('Telegram unit remains disabled; do not enable it until the issue is resolved')

def install():
    if os.geteuid() != 0:
        raise RuntimeError('Use sudo for installation')
    check()
    account = pwd.getpwnam('carecallserver')
    old_info = (PROJECT / 'event_database.py').stat()
    base = BACKUP_ROOT
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    backup = Path(tempfile.mkdtemp(prefix='telegram_' + stamp + '_', dir=base))
    print('BACKUP_DIRECTORY=' + str(backup), flush=True)
    meta = {'uid': old_info.st_uid, 'gid': old_info.st_gid,
            'mode': old_info.st_mode & 0o777,
            'old_event_hash': text_hash(PROJECT / 'event_database.py'),
            'new_event_hash': text_hash(ROOT / 'event_database.py')}
    (backup / 'install_state.json').write_text(json.dumps(meta))
    for name in read_manifest()['original']:
        shutil.copy2(PROJECT / name, backup / name)
    stopped = False
    changed = False
    unit_created = False
    try:
        command(['systemctl', 'stop', 'carecall-receiver.service'])
        stopped = True
        snapshot_database(DATABASE, backup / 'carecall_events.db')
        fix_database_file_owners(account.pw_uid, account.pw_gid)
        # Dependency modules first; replace event_database.py last.
        for name in RUNTIME_FILES[1:] + RUNTIME_FILES[:1]:
            atomic_copy(ROOT / name, PROJECT / name, account.pw_uid, account.pw_gid)
            if name == 'event_database.py':
                changed = True
        receiver_command("from event_database import EventDatabase; "
                         "d=EventDatabase('data/carecall_events.db'); print('MIGRATION=SUCCESS')")
        atomic_copy(ROOT / 'carecall-telegram.service', UNIT, 0, 0, 0o644)
        unit_created = True
        command(['systemd-analyze', 'verify', str(UNIT)])
        command(['systemctl', 'daemon-reload'])
        command(['systemctl', 'start', 'carecall-receiver.service'])
        time.sleep(2)
        command(['systemctl', 'is-active', '--quiet', 'carecall-receiver.service'])
    except BaseException:
        if changed:
            atomic_copy(backup / 'event_database.py', PROJECT / 'event_database.py',
                        meta['uid'], meta['gid'], meta['mode'])
        if unit_created:
            os.rename(UNIT, backup / 'carecall-telegram.service.not-activated')
            subprocess.run(['systemctl', 'daemon-reload'])
        if stopped:
            fix_database_file_owners(account.pw_uid, account.pw_gid)
            subprocess.run(['systemctl', 'start', 'carecall-receiver.service'])
        print('INSTALL=FAILED; receiver code restored where replaced; database retained', flush=True)
        raise
    print('INSTALL=SUCCESS')
    print('RECEIVER=ACTIVE')
    print('TELEGRAM=NOT_STARTED (configure recipient and token next)')
    print('ROLLBACK_BACKUP=' + str(backup))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('check', 'install', 'rollback'))
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if args.action == 'check':
        check()
    elif args.action == 'install':
        install()
    else:
        if args.backup is None:
            parser.error('--backup is required for rollback')
        rollback(args.backup)

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('OPERATION=CANCELLED') from None
    except Exception as exc:
        # This installer does not read token values or dump environment files.
        raise SystemExit('OPERATION=FAILED: ' + str(exc)) from None
