"""Guarded v1-to-v2 upgrade; never reads token values or restores an old DB."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import time
from install_telegram import atomic_copy, command, snapshot_database, text_hash

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/home/carecallserver/carecall_receiver')
DB = PROJECT / 'data/carecall_events.db'
UNIT = Path('/etc/systemd/system/carecall-registration.service')
CHANGED = ('telegram_api.py', 'telegram_worker.py')
ADDED = ('guardian_store.py', 'guardian_admin.py', 'telegram_registration.py')
BACKUPS = Path('/var/backups/carecall')

def run_python(code):
    args = [str(PROJECT / '.venv/bin/python'), '-B', '-c', code]
    if os.geteuid() == 0:
        args = ['runuser','-u','carecallserver','--'] + args
    return command(args, cwd=PROJECT, capture=True)

def check():
    manifest = json.loads((ROOT/'upgrade_manifest.json').read_text())
    for name, expected in manifest['release'].items():
        file = ROOT/name
        if file.is_symlink() or not file.is_file() or text_hash(file) != expected:
            raise RuntimeError('Upgrade bundle differs: '+name)
    for name, expected in manifest['v1'].items():
        file = PROJECT/name
        if file.is_symlink() or not file.is_file() or text_hash(file) != expected:
            raise RuntimeError('Installed v1 file differs: '+name)
    if UNIT.exists() or UNIT.is_symlink():
        raise RuntimeError('Registration service already exists; do not repeat this upgrade')
    for name in ADDED:
        if (PROJECT/name).exists() or (PROJECT/name).is_symlink():
            raise RuntimeError('Additional filename already exists: '+name)
    if not DB.is_file() or DB.is_symlink():
        raise RuntimeError('Existing database missing or symlinked')
    info = json.loads(run_python("import json,sqlite3; c=sqlite3.connect('file:data/carecall_events.db?mode=ro',uri=True); "
        "print(json.dumps({'tables':[r[0] for r in c.execute(\"SELECT name FROM sqlite_schema WHERE type='table'\")],"
        "'recipients':c.execute('SELECT count(*) FROM notification_recipients WHERE enabled=1').fetchone()[0]})); c.close()"))
    if not {'call_events','notification_recipients','notification_outbox'} <= set(info['tables']):
        raise RuntimeError('Required v1 database tables missing')
    if any(x.startswith('guardian_') for x in info['tables']) or info['recipients'] < 1:
        raise RuntimeError('Not the expected configured v1 database')
    for service in ('carecall-receiver.service','carecall-telegram.service'):
        state = command(['systemctl','show',service,'-p','ActiveState','-p','User','-p','Group','-p','WorkingDirectory'], capture=True)
        props = dict(line.split('=',1) for line in state.splitlines() if '=' in line)
        if props != {'ActiveState':'active','User':'carecallserver','Group':'carecallserver','WorkingDirectory':str(PROJECT)}:
            raise RuntimeError('Service state or account differs: '+service)
    args = [str(PROJECT/'.venv/bin/python'),'-B','-m','unittest','discover','-s','telegram_tests','-q']
    if os.geteuid() == 0:
        args = ['runuser','-u','carecallserver','--'] + args
    command(args,cwd=ROOT)
    print('UPGRADE_PRECHECK=SUCCESS',flush=True)

def fix_owners(uid,gid):
    for suffix in ('','-wal','-shm'):
        path = Path(str(DB)+suffix)
        if path.is_symlink():
            raise RuntimeError('Unexpected database sidecar symlink')
        if path.exists():
            os.chown(path,uid,gid)

def restore_code(backup, metadata):
    for name in CHANGED:
        atomic_copy(backup/name, PROJECT/name, metadata['uid'],metadata['gid'],metadata['modes'][name])

def install():
    if os.geteuid() != 0:
        raise RuntimeError('Run installation with sudo')
    check()
    account = pwd.getpwnam('carecallserver')
    BACKUPS.mkdir(mode=0o700,parents=True,exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='telegram_v2_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'_',dir=BACKUPS))
    meta = {'uid':account.pw_uid,'gid':account.pw_gid,'modes':{},'old_hash':{},'new_hash':{}}
    for name in CHANGED:
        shutil.copy2(PROJECT/name,backup/name)
        meta['modes'][name] = (PROJECT/name).stat().st_mode & 0o777
        meta['old_hash'][name],meta['new_hash'][name] = text_hash(PROJECT/name),text_hash(ROOT/name)
    (backup/'upgrade_state.json').write_text(json.dumps(meta))
    print('BACKUP_DIRECTORY='+str(backup),flush=True)
    stopped, replaced, created_unit = False, False, False
    try:
        # Receiver source remains v1, but pause both writers for a clear upgrade snapshot.
        stopped = True
        command(['systemctl','stop','carecall-telegram.service','carecall-receiver.service'])
        snapshot_database(DB,backup/'carecall_events.db')
        fix_owners(account.pw_uid,account.pw_gid)
        for name in ADDED:
            atomic_copy(ROOT/name,PROJECT/name,account.pw_uid,account.pw_gid)
        for name in CHANGED:
            replaced = True
            atomic_copy(ROOT/name,PROJECT/name,account.pw_uid,account.pw_gid,meta['modes'][name])
        run_python("from guardian_store import GuardianStore; GuardianStore('data/carecall_events.db').migrate(); print('GUARDIAN_SCHEMA=READY')")
        atomic_copy(ROOT/'carecall-registration.service',UNIT,0,0,0o644)
        created_unit = True
        command(['systemd-analyze','verify',str(UNIT)])
        command(['systemctl','daemon-reload'])
        command(['systemctl','start','carecall-receiver.service','carecall-telegram.service'])
        time.sleep(2)
        for service in ('carecall-receiver.service','carecall-telegram.service'):
            command(['systemctl','is-active','--quiet',service])
    except BaseException:
        # Stop a partially restarted sender before restoring its modules.
        if stopped:
            subprocess.run(['systemctl','stop','carecall-telegram.service'])
        if replaced:
            restore_code(backup,meta)
        if created_unit:
            os.rename(UNIT,backup/'carecall-registration.service.unused')
            subprocess.run(['systemctl','daemon-reload'])
        if stopped:
            fix_owners(account.pw_uid,account.pw_gid)
            subprocess.run(['systemctl','start','carecall-receiver.service','carecall-telegram.service'])
        print('UPGRADE=FAILED; old sender restored where replaced; database retained')
        raise
    print('UPGRADE=SUCCESS')
    print('MESSAGE_FORMAT=V2')
    print('EXISTING_RECIPIENTS=PRESERVED')
    print('REGISTRATION_SERVICE=INSTALLED_NOT_STARTED')

def rollback(backup):
    if os.geteuid() != 0:
        raise RuntimeError('Run rollback with sudo')
    backup = backup.resolve()
    if not backup.is_relative_to(BACKUPS):
        raise RuntimeError('Unexpected backup location')
    meta = json.loads((backup/'upgrade_state.json').read_text())
    for name in CHANGED:
        if text_hash(PROJECT/name) not in {meta['old_hash'][name],meta['new_hash'][name]}:
            raise RuntimeError('Source changed after upgrade: '+name)
    command(['systemctl','disable','--now','carecall-registration.service'])
    command(['systemctl','stop','carecall-telegram.service'])
    try:
        restore_code(backup,meta)
    finally:
        command(['systemctl','start','carecall-telegram.service'])
    print('ROLLBACK=SUCCESS; new calls/recipients retained; registration service disabled')

def main():
    p=argparse.ArgumentParser()
    p.add_argument('action',choices=('check','install','rollback'))
    p.add_argument('--backup',type=Path)
    args=p.parse_args()
    if args.action=='check': check()
    elif args.action=='install': install()
    else:
        if args.backup is None: p.error('--backup is required')
        rollback(args.backup)

if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit('UPGRADE=CANCELLED') from None
    except Exception as exc:
        raise SystemExit('UPGRADE=FAILED: '+str(exc)) from None
