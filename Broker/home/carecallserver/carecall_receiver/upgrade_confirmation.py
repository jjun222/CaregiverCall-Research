"""Guarded code-only update. Never copies credentials or restores an old database."""
import argparse
import ast
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import time

ROOT = Path(__file__).resolve().parent
PROJECT = Path('/home/carecallserver/carecall_receiver')
DB = PROJECT / 'data/carecall_events.db'
BACKUPS = Path('/var/backups/carecall')
SERVICES = ('carecall-receiver.service','carecall-telegram.service','carecall-registration.service')


def run(args, cwd=None):
    result = subprocess.run([str(x) for x in args],cwd=cwd,text=True,capture_output=True)
    if result.returncode:
        # Avoid printing service environment, credentials, DB rows or API errors.
        raise RuntimeError('Command failed: ' + str(args[0]))
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_text(encoding='utf-8-sig').encode()).hexdigest()


def match(path, expected):
    return not path.is_symlink() and path.is_file() and digest(path)==expected


def manifest():
    return json.loads((ROOT/'confirmation_manifest.json').read_text())


def app_python(code, cwd=None):
    args=[PROJECT/'.venv/bin/python','-B','-c',code]
    if os.geteuid()==0: args=['runuser','-u','carecallserver','--']+args
    return run(args,cwd or PROJECT)


def check():
    m=manifest()
    if ROOT==PROJECT or ROOT.is_relative_to(PROJECT):
        raise RuntimeError('Extract the bundle beside the project, not inside it')
    for name,expected in m['bundle'].items():
        if not match(ROOT/name,expected): raise RuntimeError('Bundle mismatch: '+name)
        if name.endswith('.py'): ast.parse((ROOT/name).read_text(encoding='utf-8-sig'),filename=name)
    for name,expected in m['dependencies'].items():
        if not match(PROJECT/name,expected): raise RuntimeError('Installed dependency differs: '+name)
    installed=all(match(PROJECT/name,expected) for name,expected in m['release'].items())
    if not installed:
        for name,expected in m['original'].items():
            if not match(PROJECT/name,expected):
                raise RuntimeError('Installed source differs; no overwrite: '+name)
        for name in set(m['release'])-set(m['original']):
            if (PROJECT/name).exists() or (PROJECT/name).is_symlink():
                raise RuntimeError('New file already exists: '+name)
    if DB.is_symlink() or DB.parent.is_symlink() or not DB.is_file():
        raise RuntimeError('Expected database unavailable')
    info=json.loads(app_python("""
import ast,json,sqlite3,sys,importlib.metadata
from contextlib import closing
from pathlib import Path
ast.parse(Path('receiver_config.py').read_text())
with closing(sqlite3.connect(Path('data/carecall_events.db').resolve().as_uri()+'?mode=ro',uri=True)) as c:
 print(json.dumps({'python':list(sys.version_info[:2]),'paho':importlib.metadata.version('paho-mqtt'),
 'integrity':c.execute('PRAGMA quick_check').fetchone()[0],
 'tables':[r[0] for r in c.execute("SELECT name FROM sqlite_schema WHERE type='table'")],
 'operator':c.execute('SELECT count(*) FROM operator_config').fetchone()[0],
 'recipients':c.execute('SELECT count(*) FROM notification_recipients WHERE enabled=1').fetchone()[0]}))
"""))
    required={'call_events','notification_outbox','notification_recipients','guardian_cursor',
              'guardian_replies','operator_config','operator_handover'}
    if (not required<=set(info['tables']) or info['integrity']!='ok' or info['python']<[3,12]
            or info['paho']!='2.1.0' or info['operator']!=1 or info['recipients']<1):
        raise RuntimeError('Runtime/database prerequisite differs')
    for service in SERVICES:
        props=dict(line.split('=',1) for line in run(['systemctl','show',service,'-p','ActiveState',
            '-p','User','-p','Group','-p','WorkingDirectory','-p','DropInPaths']).splitlines() if '=' in line)
        expected={'ActiveState':'active','User':'carecallserver','Group':'carecallserver',
                  'WorkingDirectory':str(PROJECT),'DropInPaths':''}
        if props!=expected: raise RuntimeError('Service configuration/state differs: '+service)
    # Test release files with temporary databases only. Never sends real notifications.
    args=[PROJECT/'.venv/bin/python','-B','-m','unittest','discover','-s','confirmation_tests','-q']
    if os.geteuid()==0: args=['runuser','-u','carecallserver','--']+args
    run(args,ROOT)
    print('CONFIRMATION_PRECHECK=SUCCESS',flush=True)
    print('CURRENT_RELEASE=' + ('installed' if installed else 'baseline'),flush=True)
    return m,installed


def atomic_copy(src,dst,uid,gid,mode):
    fd,name=tempfile.mkstemp(prefix='.confirmation-',dir=dst.parent)
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(src.read_bytes()); f.flush(); os.fsync(f.fileno())
        os.chown(name,uid,gid); os.chmod(name,mode); os.replace(name,dst)
        d=os.open(dst.parent,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(name): os.unlink(name)


def stop_services():
    for service in reversed(SERVICES): run(['systemctl','stop',service])


def start_services():
    for service in SERVICES: run(['systemctl','start',service])
    # Detect immediate startup failures after systemctl accepted the start request.
    time.sleep(3)
    for service in SERVICES: run(['systemctl','is-active','--quiet',service])


def migrate():
    app_python("""
from confirmation_store import ConfirmationStore
from contextlib import closing
s=ConfirmationStore('data/carecall_events.db')
with closing(s.connect()) as c:
 before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in
 ('operator_config','notification_recipients','guardian_cursor')}
s.migrate()
with closing(s.connect()) as c:
 for t,rows in before.items():
  if rows!=[tuple(r) for r in c.execute('SELECT * FROM '+t)]: raise RuntimeError('Existing state changed')
 if c.execute('PRAGMA foreign_key_check').fetchone(): raise RuntimeError('Foreign key check failed')
import mqtt_receiver,telegram_worker,telegram_registration
""")


def backup_code(m):
    BACKUPS.mkdir(parents=True,exist_ok=True)
    backup=BACKUPS/('guardian_confirm_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%fZ'))
    backup.mkdir(mode=0o700)
    files={}
    for name in m['release']:
        p=PROJECT/name
        if p.exists():
            st=p.stat(); shutil.copyfile(p,backup/name); os.chmod(backup/name,0o600)
            files[name]={'digest':digest(p),'uid':st.st_uid,'gid':st.st_gid,'mode':st.st_mode&0o777}
        else: files[name]=None
    (backup/'state.json').write_text(json.dumps({'project':str(PROJECT),'files':files,'release':m['release']},indent=2))
    os.chmod(backup/'state.json',0o600)
    return backup


def restore_code(backup,state):
    for name,info in state['files'].items():
        p=PROJECT/name
        if info is None:
            if p.exists(): p.unlink()
        else:
            atomic_copy(backup/name,p,info['uid'],info['gid'],info['mode'])


def install():
    if os.geteuid()!=0: raise RuntimeError('Use sudo for install')
    m,installed=check()
    if installed:
        print('CONFIRMATION_ALREADY_INSTALLED'); return
    backup=backup_code(m)
    print('CODE_BACKUP='+str(backup),flush=True)
    try:
        stop_services()
        # Recheck after stopping, before any code replacement.
        for name,expected in m['original'].items():
            if not match(PROJECT/name,expected): raise RuntimeError('Source changed during installation')
        owner=pwd.getpwnam('carecallserver')
        for name in m['release']:
            atomic_copy(ROOT/name,PROJECT/name,owner.pw_uid,owner.pw_gid,0o640)
        migrate()
        start_services()
    except BaseException:
        # Added tables stay. Restoring an earlier DB would discard calls/deletions.
        try:
            stop_services()
            state=json.loads((backup/'state.json').read_text())
            restore_code(backup,state)
            start_services()
            print('AUTOMATIC_CODE_ROLLBACK=SUCCESS',flush=True)
        except Exception:
            print('AUTOMATIC_CODE_ROLLBACK=FAILED; backup='+str(backup),flush=True)
        raise
    print('CONFIRMATION_INSTALL=SUCCESS',flush=True)


def rollback(backup):
    if os.geteuid()!=0: raise RuntimeError('Use sudo for rollback')
    raw=Path(backup)
    if raw.is_symlink(): raise RuntimeError('Invalid backup')
    backup=raw.resolve()
    if backup.parent!=BACKUPS or not backup.name.startswith('guardian_confirm_'):
        raise RuntimeError('Invalid backup directory')
    st=backup.stat()
    if st.st_uid!=0 or st.st_mode&0o077: raise RuntimeError('Backup ownership/mode differs')
    state=json.loads((backup/'state.json').read_text()); m=manifest()
    if state['project']!=str(PROJECT) or set(state['files'])!=set(m['release']):
        raise RuntimeError('Backup belongs to another release')
    for name,info in state['files'].items():
        if Path(name).name!=name: raise RuntimeError('Invalid backup filename')
        if info is not None and not match(backup/name,info['digest']): raise RuntimeError('Backup differs: '+name)
        current=PROJECT/name
        if current.is_symlink(): raise RuntimeError('Installed source is a symlink')
        if current.exists() and digest(current) not in {state['release'][name],info['digest'] if info else ''}:
            raise RuntimeError('Later source change detected: '+name)
    stop_services(); restore_code(backup,state); start_services()
    print('CONFIRMATION_CODE_ROLLBACK=SUCCESS; current database retained')


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('action',choices=('check','install','rollback'))
    parser.add_argument('--backup')
    args=parser.parse_args()
    if args.action=='rollback' and not args.backup: parser.error('--backup is required')
    try:
        if args.action=='check': check()
        else:
            # One root installer/rollback at a time; not a service configuration change.
            if os.geteuid()!=0: raise RuntimeError('Use sudo for install/rollback')
            with open('/run/lock/carecall-confirmation-upgrade.lock','w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                install() if args.action=='install' else rollback(args.backup)
    except Exception as exc:
        print('CONFIRMATION_ERROR='+str(exc)); return 1
    return 0

if __name__=='__main__': raise SystemExit(main())
