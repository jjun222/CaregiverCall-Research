"""Guarded v3 -> v4 upgrade. Pauses registration only; backs up code, never tokens/DB.

Rollback restores v3 code and cancels pending handovers. Current operator,
call records, recipients and completed deletions are retained, not rolled back.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shutil
import stat
import tempfile
import time
from install_telegram import atomic_copy, command, text_hash

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/home/carecallserver/carecall_receiver')
DB = PROJECT / 'data/carecall_events.db'
UNITS = Path('/etc/systemd/system')
BACKUPS = Path('/var/backups/carecall')
SERVICE = 'carecall-registration.service'
CHANGED = ('operator_store.py', 'telegram_registration.py')
ADDED = ('operator_handover.py',)


def run_python(code, cwd=None):
    args = [str(PROJECT / '.venv/bin/python'), '-B', '-c', code]
    if os.geteuid() == 0:
        args = ['runuser', '-u', 'carecallserver', '--'] + args
    return command(args, cwd=cwd or PROJECT, capture=True)


def match_file(path, digest, label):
    if path.is_symlink() or not path.is_file() or text_hash(path) != digest:
        raise RuntimeError(label + ': ' + path.name)


def check_sources():
    manifest = json.loads((ROOT / 'v4_manifest.json').read_text())
    for name, digest in manifest['release'].items():
        match_file(ROOT / name, digest, 'Bundle file differs')
    for name, digest in manifest['v3'].items():
        match_file(PROJECT / name, digest, 'Installed v3 file differs; no automatic overwrite')
    for name, digest in manifest['v3_units'].items():
        match_file(UNITS / name, digest, 'Installed service differs')
    for name in ADDED:
        if (PROJECT / name).exists() or (PROJECT / name).is_symlink():
            raise RuntimeError('New file already exists; inspect previous installation: ' + name)
    return manifest


def check():
    check_sources()
    if ROOT == PROJECT or ROOT.is_relative_to(PROJECT):
        raise RuntimeError('Extract the bundle beside the project, not inside it')
    if DB.parent.is_symlink() or DB.is_symlink() or not DB.is_file():
        raise RuntimeError('Expected existing database unavailable')
    info = json.loads(run_python("""
import json,sqlite3,sys,importlib.metadata
from pathlib import Path
from contextlib import closing
with closing(sqlite3.connect(Path('data/carecall_events.db').resolve().as_uri()+'?mode=ro',uri=True)) as c:
    print(json.dumps({'python':list(sys.version_info[:2]),'paho':importlib.metadata.version('paho-mqtt'),
      'tables':[r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE type='table'")],
      'integrity':c.execute('PRAGMA quick_check').fetchone()[0],
      'operator_count':c.execute('SELECT count(*) FROM operator_config').fetchone()[0],
      'recipients':c.execute('SELECT count(*) FROM notification_recipients WHERE enabled=1').fetchone()[0]}))
"""))
    required = {'call_events', 'notification_recipients', 'notification_outbox', 'guardian_invites',
                'guardian_cursor', 'guardian_replies', 'guardian_audit', 'operator_config',
                'operator_bootstrap', 'operator_actions', 'operator_state'}
    if not required <= set(info['tables']) or set(info['tables']) - required - {'sqlite_sequence'}:
        raise RuntimeError('Database schema differs from reviewed v3')
    if info['integrity'] != 'ok' or info['python'] < [3, 12] or info['paho'] != '2.1.0':
        raise RuntimeError('Database/Python/paho check failed')
    if info['operator_count'] != 1:
        raise RuntimeError('No current operator; finish existing operator setup before this upgrade')
    for name in ('carecall-receiver.service', 'carecall-telegram.service', SERVICE):
        output = command(['systemctl', 'show', name, '-p', 'ActiveState', '-p', 'User', '-p', 'Group',
                          '-p', 'WorkingDirectory', '-p', 'DropInPaths'], capture=True)
        props = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if props != {'ActiveState':'active', 'User':'carecallserver', 'Group':'carecallserver',
                     'WorkingDirectory':str(PROJECT), 'DropInPaths':''}:
            raise RuntimeError('Service state or configuration differs: ' + name)
        command(['systemctl', 'is-enabled', '--quiet', name])
    args = [str(PROJECT / '.venv/bin/python'), '-B', '-m', 'unittest', 'discover', '-s', 'telegram_tests', '-q']
    if os.geteuid() == 0:
        args = ['runuser', '-u', 'carecallserver', '--'] + args
    command(args, cwd=ROOT)
    print('EXISTING_OPERATOR=CONFIGURED', flush=True)
    print('ENABLED_RECIPIENTS=' + str(info['recipients']), flush=True)
    print('V4_PRECHECK=SUCCESS', flush=True)


def migrate_installed():
    run_python("""
from operator_store import OperatorStore
from contextlib import closing
store=OperatorStore('data/carecall_events.db')
with closing(store.connect()) as c:
    before_operator=[tuple(r) for r in c.execute('SELECT * FROM operator_config')]
    before_recipients=[tuple(r) for r in c.execute('SELECT * FROM notification_recipients ORDER BY device_id,chat_id')]
store.migrate()
with closing(store.connect()) as c:
    if before_operator != [tuple(r) for r in c.execute('SELECT * FROM operator_config')]:
        raise RuntimeError('Operator changed during migration')
    if before_recipients != [tuple(r) for r in c.execute('SELECT * FROM notification_recipients ORDER BY device_id,chat_id')]:
        raise RuntimeError('Recipients changed during migration')
    if c.execute('PRAGMA foreign_key_check').fetchone():
        raise RuntimeError('Database relationships invalid')
print('V4_SCHEMA=READY; operator and recipients preserved')
""")


def cancel_pending_handover():
    # Runs using stdlib only: also works if installation was interrupted mid-copy.
    run_python("""
import sqlite3
from contextlib import closing
with closing(sqlite3.connect('file:data/carecall_events.db?mode=rw',uri=True)) as c, c:
    c.execute('PRAGMA secure_delete=ON')
    c.execute('BEGIN IMMEDIATE')
    c.execute("DELETE FROM guardian_replies WHERE admin_reply=1 OR dedupe_key LIKE 'handover:%'")
    c.execute('DELETE FROM operator_actions')
    c.execute('DELETE FROM operator_state')
    c.execute('DROP TABLE IF EXISTS operator_handover')
""")


def restore_code(backup, meta):
    for name in CHANGED:
        atomic_copy(backup / name, PROJECT / name, meta['uid'], meta['gid'], meta['modes'][name])
    for name in ADDED:
        path = PROJECT / name
        if path.exists() or path.is_symlink():
            match_file(path, meta['new_hash'][name], 'Added file modified; retained')
            path.unlink()


def start_registration():
    command(['systemctl', 'start', SERVICE])
    time.sleep(2)
    command(['systemctl', 'is-active', '--quiet', SERVICE])


def install():
    if os.geteuid() != 0:
        raise RuntimeError('Use sudo for the one-time install')
    check()
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = BACKUPS.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Expected root-owned mode-700 backup directory')
    backup = Path(tempfile.mkdtemp(prefix='telegram_v4_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '_', dir=BACKUPS))
    account = pwd.getpwnam('carecallserver')
    meta = {'uid':account.pw_uid,'gid':account.pw_gid,'modes':{},'old_hash':{},'new_hash':{}}
    for name in CHANGED:
        source = PROJECT / name
        shutil.copy2(source, backup / name)
        meta['modes'][name] = stat.S_IMODE(source.stat().st_mode)
        meta['old_hash'][name] = text_hash(source)
    for name in CHANGED + ADDED:
        meta['new_hash'][name] = text_hash(ROOT / name)
    (backup / 'v4_state.json').write_text(json.dumps(meta), encoding='utf-8')
    print('V4_CODE_BACKUP=' + str(backup), flush=True)
    mutated = False
    try:
        command(['systemctl', 'stop', SERVICE])
        check_sources()
        mutated = True
        for name in ADDED + CHANGED:
            atomic_copy(ROOT / name, PROJECT / name, account.pw_uid, account.pw_gid, 0o640)
        migrate_installed()
        start_registration()
    except BaseException:
        try:
            command(['systemctl', 'stop', SERVICE])
            if mutated:
                cancel_pending_handover()
                restore_code(backup, meta)
            start_registration()
            print(('V4_RECOVERY=V3_CODE_RESTORED; current database and operator retained' if mutated
                   else 'V4_RECOVERY=SERVICE_RESTARTED; existing files retained'), flush=True)
        except Exception as exc:
            print('V4_RECOVERY=CHECK_REQUIRED; error_type=' + type(exc).__name__, flush=True)
        raise
    print('V4_UPGRADE=SUCCESS', flush=True)
    print('BOT_TOKEN=UNCHANGED; CURRENT_OPERATOR=UNCHANGED', flush=True)
    print('NEXT=send /admin from the current operator account', flush=True)


def rollback(backup):
    if os.geteuid() != 0:
        raise RuntimeError('Use sudo for rollback')
    backup = backup.resolve()
    if backup.parent != BACKUPS or not backup.name.startswith('telegram_v4_'):
        raise RuntimeError('Unexpected backup path')
    info = backup.lstat()
    if info.st_uid != 0 or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise RuntimeError('Backup owner/permissions differ')
    meta = json.loads((backup / 'v4_state.json').read_text())
    for name in CHANGED:
        match_file(backup / name, meta['old_hash'][name], 'Backup differs')
        path = PROJECT / name
        if path.is_symlink() or text_hash(path) not in (meta['old_hash'][name], meta['new_hash'][name]):
            raise RuntimeError('Installed file modified after upgrade: ' + name)
    for name in ADDED:
        if (PROJECT / name).exists() or (PROJECT / name).is_symlink():
            match_file(PROJECT / name, meta['new_hash'][name], 'Installed file modified after upgrade')
    command(['systemctl', 'stop', SERVICE])
    cancel_pending_handover()
    restore_code(backup, meta)
    start_registration()
    print('V4_ROLLBACK=SUCCESS; current operator, calls, recipients and completed deletions retained')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install', 'rollback'))
    parser.add_argument('--backup', type=Path)
    args = parser.parse_args()
    if args.action == 'check': check()
    elif args.action == 'install': install()
    elif args.backup is None: parser.error('--backup is required')
    else: rollback(args.backup)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('V4_UPGRADE=CANCELLED') from None
    except Exception as exc:
        raise SystemExit('V4_UPGRADE=FAILED: ' + str(exc)) from None
