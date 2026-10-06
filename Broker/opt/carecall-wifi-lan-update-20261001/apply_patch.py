#!/usr/bin/env python3
"""Stage, activate and report the CareCall LAN update. Run stage/activate with sudo.

Activation runs under systemd so SSH loss does not terminate the transaction.
Complete application directories are atomically exchanged, never mixed file by
file. A boot recovery unit restores the old version after an interrupted update.
"""
import argparse
import ctypes
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

PACKAGE = Path(__file__).resolve().parent
ROOT = Path('/opt/carecall-wifi-manager')
ETC = Path('/etc/carecall-wifi-manager')
RUN = Path('/run/carecall-wifi-manager')
DEPLOY = Path('/opt/carecall-wifi-lan-update-20261001')
SYSTEM = Path('/etc/systemd/system')
BACKUPS = Path('/var/backups/carecall')
UFW_ETC = Path('/etc/ufw')
UNIT = 'carecall-wifi-manager.service'
UPDATE_UNIT = 'carecall-wifi-lan-update'
GUARD_UNIT = 'carecall-wifi-lan-recover.service'
DROPIN_NAME = '60-carecall-lan.conf'
DROPIN_TEXT = '# CareCall LAN updater 20261001\n[Service]\nReadWritePaths=/etc/ufw\n'
VERSION = '20261001-lan-1'
ORIGINAL = '192.168.0.0/24'
MODULES = ('manager', 'web', 'router', 'transaction', 'firewall', 'firewall_base', 'common')


class UpdateError(Exception):
    pass


def require(value, code):
    if not value:
        raise UpdateError(code)


def emit(key, value):
    print(str(key) + '=' + str(value), flush=True)


def sha(value):
    return hashlib.sha256(value).hexdigest()


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(path):
    for item in path.rglob('*'):
        if item.is_file():
            with item.open('rb') as stream:
                os.fsync(stream.fileno())
    for directory in sorted((p for p in path.rglob('*') if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        sync_dir(directory)
    sync_dir(path)


def read(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022
                and info.st_size < 4 * 1024 * 1024, 'INSTALLED_FILE_NOT_TRUSTED')
        return stream.read()


def atomic(path, data, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.lan-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def obj(path):
    return json.loads(read(path))


def write_obj(path, value):
    atomic(path, (json.dumps(value, indent=2) + '\n').encode())


def command(*args, check=True, timeout=40):
    result = subprocess.run(list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=timeout,
                            env=dict(os.environ, PATH='/usr/sbin:/usr/bin:/sbin:/bin', LC_ALL='C'))
    require(not check or result.returncode == 0, 'COMMAND_FAILED_' + Path(args[0]).name.upper().replace('-', '_'))
    return result


def systemctl(*args, **kwargs):
    return command('/usr/bin/systemctl', *args, **kwargs)


def active(name):
    return systemctl('is-active', '--quiet', name, check=False).returncode == 0


def guard_text():
    return f'''[Unit]
Description=Recover an interrupted CareCall LAN update
After=local-fs.target ufw.service
Before={UNIT}
ConditionPathExists={DEPLOY}/pending
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 -I -B {DEPLOY}/apply_patch.py recover-boot
TimeoutStartSec=180s
UMask=0077
StandardOutput=journal
StandardError=null
[Install]
WantedBy=multi-user.target
'''


def dropin():
    return SYSTEM / (UNIT + '.d') / DROPIN_NAME


def exchange(first, second):
    """Linux atomic directory exchange. Probe before touching the live tree."""
    libc = ctypes.CDLL(None, use_errno=True)
    function = libc.renameat2
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    require(function(-100, os.fsencode(first), -100, os.fsencode(second), 2) == 0,
            'ATOMIC_DIRECTORY_EXCHANGE_FAILED')
    sync_dir(first.parent)
    sync_dir(second.parent)


def verify_bundle(path):
    entries = {}
    for line in (path / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        rel = Path(name)
        require(not rel.is_absolute() and '..' not in rel.parts and name not in entries
                and not (path / rel).is_symlink(), 'BUNDLE_MANIFEST_INVALID')
        require(sha((path / rel).read_bytes()) == expected, 'BUNDLE_HASH_MISMATCH')
        entries[name] = expected
    required = {'apply_patch.py', 'previous_hashes.json', 'README_KO.md'}
    require(required <= entries.keys(), 'BUNDLE_MANIFEST_INCOMPLETE')
    app = {name[4:]: value for name, value in entries.items() if name.startswith('app/')}
    require(set(app) == {'common.py', 'manager.py', 'router.py', 'transaction.py', 'web.py',
                         'firewall.py', 'firewall_base.py', 'reviewed_firewall.json'}, 'BUNDLE_APP_LAYOUT_INVALID')
    for name in app:
        if name.endswith('.py'):
            compile((path / 'app' / name).read_bytes(), name, 'exec')
    return app


def verify_tree(path, expected):
    require(path.is_dir() and not path.is_symlink() and path.stat().st_uid == 0
            and not path.stat().st_mode & 0o022, 'APP_DIRECTORY_NOT_TRUSTED')
    names = {p.name for p in path.iterdir() if p.name != '__pycache__'}
    require(names == set(expected), 'INSTALLED_APP_LAYOUT_CHANGED')
    for name, digest in expected.items():
        require(sha(read(path / name)) == digest, 'INSTALLED_APP_CHANGED')


def load_app(path):
    for name in MODULES:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(path))
    try:
        return importlib.import_module('manager')
    finally:
        sys.path.pop(0)


def protected_files(manager):
    paths = list(manager.tx.targets().values()) + [manager.c.ETC / n for n in ('settings.json', 'owner')]
    return {str(path): sha(read(path)) for path in paths}


def check_protected(plan):
    for name, expected in plan['protected'].items():
        require(sha(read(Path(name))) == expected, 'WIFI_CONFIGURATION_CHANGED_DURING_UPDATE')


def preflight(bundle, allow_guard=False):
    previous = json.loads((bundle / 'previous_hashes.json').read_text())
    verify_tree(ROOT, previous['app'])
    for name, digest in previous['units'].items():
        require(sha(read(SYSTEM / name)) == digest, 'INSTALLED_UNIT_CHANGED')
        require(not systemctl('show', name, '-p', 'DropInPaths', '--value').stdout.strip(), 'EXISTING_UNIT_DROPIN_REQUIRES_REVIEW')
        require(systemctl('show', name, '-p', 'FragmentPath', '--value').stdout.strip() == str(SYSTEM / name),
                'UNIT_FRAGMENT_CHANGED')
    require(not dropin().parent.exists(), 'DROPIN_DIRECTORY_ALREADY_EXISTS')
    require(not (ETC / 'lan-firewall.json').exists(), 'LAN_STATE_ALREADY_EXISTS')
    require(not (RUN / 'setup-request').exists(), 'SETUP_REQUEST_PENDING')
    require(not (SYSTEM / GUARD_UNIT).exists() or allow_guard and read(SYSTEM / GUARD_UNIT).decode() == guard_text(),
            'RECOVERY_UNIT_CONFLICT')
    manager = load_app(ROOT)
    require(manager.c.VERSION == previous['version'] and active(UNIT), 'KNOWN_ACTIVE_MANAGER_REQUIRED')
    state = manager.c.read_state() or {}
    require(state.get('phase') == 'online' and state.get('link_ready') is True, 'MANAGER_MUST_BE_ONLINE')
    profile = manager.tx.obj(manager.tx.PROFILE)
    manager.tx.validate_candidate(profile)
    require(profile.get('committed_boot_id') and manager.OWNER.exists(), 'COMMITTED_WIFI_REQUIRED')
    require(not manager.REQUEST.exists(), 'ACTIVATION_REQUEST_PENDING')
    require(not manager.tx.JOURNAL.exists() or manager.tx.obj(manager.tx.JOURNAL).get('phase') != 'pending',
            'WIFI_TRANSACTION_PENDING')
    require(manager.saved_ready(profile), 'SAVED_WIFI_NOT_READY')
    require(all(active(name + '.service') for name in manager.c.CARECALL) and active('avahi-daemon.service'),
            'CARECALL_SERVICE_NOT_ACTIVE')
    require(not any(active(manager.c.PREFIX + name + '.service') for name in ('hostapd', 'dhcp', 'web', 'router')),
            'SETUP_SERVICE_ACTIVE')
    manager.firewall.check()
    addresses = json.loads(manager.c.command('ip', '-j', '-4', 'address', 'show', 'dev', 'wlan0').stdout)
    routes = json.loads(manager.c.command('ip', '-j', '-4', 'route', 'show', 'default').stdout)
    # Validate using the staged new selector without changing live rules.
    protected = protected_files(manager)
    new = load_app(bundle / 'app')
    require(new.firewall.select_network(addresses, routes) == ORIGINAL, 'APPLY_ON_VERIFIED_LAB_LAN_REQUIRED')
    return protected


def write_result(phase, **values):
    write_obj(DEPLOY / 'result.json', dict(version=VERSION, phase=phase, **values))


def stage():
    app_hashes = verify_bundle(PACKAGE)
    if DEPLOY.exists():
        require(not DEPLOY.is_symlink(), 'STAGE_DIRECTORY_NOT_TRUSTED')
        verify_bundle(DEPLOY)
        require(sha(read(DEPLOY / 'SHA256SUMS')) == sha((PACKAGE / 'SHA256SUMS').read_bytes()), 'DIFFERENT_LAN_PACKAGE_STAGED')
        plan = obj(DEPLOY / 'plan.json')
        require(plan['app_hashes'] == app_hashes, 'STAGED_APP_RECORD_CHANGED')
        emit('WIFI_LAN_STAGE', 'ALREADY_STAGED')
        status()
        return
    protected = preflight(PACKAGE)
    BACKUPS.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='wifi_lan_20261001_', dir=BACKUPS))
    old = json.loads((PACKAGE / 'previous_hashes.json').read_text())
    for name in old['app']:
        atomic(backup / 'app' / name, read(ROOT / name))
    for filename in protected:
        atomic(backup / 'private' / filename.lstrip('/'), read(Path(filename)))
    for name in ('user.rules', 'user6.rules'):
        atomic(backup / 'ufw' / name, read(UFW_ETC / name))
    try:
        shutil.copytree(PACKAGE, DEPLOY, ignore=shutil.ignore_patterns('__pycache__'))
        for directory in [DEPLOY] + [p for p in DEPLOY.rglob('*') if p.is_dir()]:
            os.chmod(directory, 0o755)
        for path in DEPLOY.rglob('*'):
            if path.is_file():
                os.chmod(path, 0o644)
        verify_bundle(DEPLOY)
        shutil.copytree(DEPLOY / 'app', DEPLOY / 'next')
        sync_tree(DEPLOY)
        require(ROOT.stat().st_dev == (DEPLOY / 'next').stat().st_dev, 'APP_DIRECTORIES_NOT_ON_SAME_FILESYSTEM')
        with tempfile.TemporaryDirectory(prefix='.exchange-probe-', dir=DEPLOY) as temporary:
            first, second = Path(temporary) / 'a', Path(temporary) / 'b'
            first.mkdir(); second.mkdir()
            exchange(first, second)
        plan = {'version': VERSION, 'app_hashes': app_hashes, 'previous': old,
                'protected': protected, 'backup_dir': str(backup)}
        write_obj(DEPLOY / 'plan.json', plan)
        write_result('STAGED')
        check_protected(plan)
    except BaseException:
        if DEPLOY.exists():
            shutil.rmtree(DEPLOY)
        raise
    emit('PATCH_BACKUP_DIR', backup)
    emit('BACKUP_CONTAINS_SECRETS', 'KEEP_ON_PI_DO_NOT_UPLOAD')
    emit('WIFI_LAN_STAGE', 'SUCCESS')
    emit('LIVE_CODE_CHANGED', 'NO')
    emit('NETWORK_CHANGED', 'NO')
    emit('UFW_RULES_CHANGED', 'NO')
    emit('NEXT_ACTION', 'ACTIVATE')


def activate():
    verify_bundle(DEPLOY)
    require(obj(DEPLOY / 'result.json')['phase'] == 'STAGED', 'UPDATE_NOT_IN_STAGED_STATE')
    systemctl('reset-failed', UPDATE_UNIT + '.service', check=False)
    command('/usr/bin/systemd-run', '--unit=' + UPDATE_UNIT, '--collect', '--no-block',
            '--property=Type=oneshot', '--property=TimeoutStartSec=600s', '--property=UMask=0077',
            '/usr/bin/python3', '-I', '-B', str(DEPLOY / 'apply_patch.py'), 'run')
    emit('WIFI_LAN_ACTIVATION', 'SCHEDULED')
    emit('SSH_MAY_DISCONNECT', 'YES')
    emit('ON_FAILURE', 'RESTORE_PREVIOUS_MANAGER_AND_LAB_FIREWALL')


def wait_online(expected_version, seconds=115):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            state = obj(RUN / 'state.json')
            if active(UNIT) and state.get('version') == expected_version and state.get('phase') == 'online' and state.get('link_ready') is True:
                return
        except (OSError, ValueError, UpdateError):
            pass
        time.sleep(1)
    raise UpdateError('UPDATED_MANAGER_ONLINE_TIMEOUT')


def rollback(plan, start_manager):
    if start_manager:
        systemctl('stop', UNIT)
    else:
        require(not active(UNIT), 'BOOT_RECOVERY_MANAGER_ALREADY_RUNNING')
    check_protected(plan)
    new = load_app(DEPLOY / 'app')
    new.firewall.reconcile(ORIGINAL)
    if sha(read(ROOT / 'common.py')) == plan['app_hashes']['common.py']:
        verify_tree(ROOT, plan['app_hashes'])
        verify_tree(DEPLOY / 'next', plan['previous']['app'])
        exchange(ROOT, DEPLOY / 'next')
    else:
        verify_tree(ROOT, plan['previous']['app'])
    path = ETC / 'lan-firewall.json'
    if path.exists():
        require(new.firewall.load_state()['target'] == ORIGINAL, 'ROLLBACK_LAN_STATE_NOT_RESTORED')
        path.unlink()
        sync_dir(ETC)
    if dropin().exists():
        require(read(dropin()).decode() == DROPIN_TEXT, 'ROLLBACK_DROPIN_CHANGED')
        dropin().unlink()
        dropin().parent.rmdir()
        sync_dir(SYSTEM)
    systemctl('daemon-reload')
    old = load_app(ROOT)
    old.firewall.check()
    if start_manager:
        systemctl('start', UNIT)
        wait_online(plan['previous']['version'])
    write_result('ROLLED_BACK' if start_manager else 'ROLLED_BACK_AFTER_REBOOT')
    (DEPLOY / 'pending').unlink(missing_ok=True)
    sync_dir(DEPLOY)


def perform_update():
    verify_bundle(DEPLOY)
    plan = obj(DEPLOY / 'plan.json')
    require(obj(DEPLOY / 'result.json')['phase'] == 'STAGED', 'UPDATE_NOT_IN_STAGED_STATE')
    try:
        preflight(DEPLOY)
        check_protected(plan)
        verify_tree(DEPLOY / 'next', plan['app_hashes'])
    except Exception as exc:
        write_result('REJECTED_BEFORE_ACTIVATION', failure=error_code(exc))
        raise
    # Install and enable the boot guard BEFORE the durable pending marker.
    atomic(SYSTEM / GUARD_UNIT, guard_text().encode(), 0o644)
    systemctl('daemon-reload')
    systemctl('enable', GUARD_UNIT)
    sync_dir(SYSTEM / 'multi-user.target.wants')
    sync_dir(SYSTEM)
    atomic(DEPLOY / 'pending', b'pending\n')
    write_result('ACTIVATING')
    committed = False
    try:
        systemctl('stop', UNIT)
        check_protected(plan)
        require(not (RUN / 'setup-request').exists(), 'SETUP_REQUEST_PENDING')
        atomic(dropin(), DROPIN_TEXT.encode(), 0o644)
        exchange(ROOT, DEPLOY / 'next')
        systemctl('daemon-reload')
        systemctl('start', UNIT)
        wait_online(VERSION)
        check_protected(plan)
        manager = load_app(ROOT)
        require(manager.saved_ready(manager.tx.obj(manager.tx.PROFILE)), 'UPDATED_SAVED_WIFI_NOT_READY')
        require(manager.firewall.current_network() == ORIGINAL, 'LAN_CHANGED_DURING_UPDATE')
        state, _ = manager.firewall.inspect()
        require(state['phase'] == 'committed' and state['target'] == ORIGINAL and manager.firewall.state_path().exists(),
                'UPDATED_LAN_FIREWALL_NOT_READY')
        require(all(active(n + '.service') for n in manager.c.CARECALL) and active('avahi-daemon.service'),
                'CARECALL_SERVICE_POSTCHECK_FAILED')
        verify_tree(ROOT, plan['app_hashes'])
        write_result('COMMITTED')
        committed = True
        (DEPLOY / 'pending').unlink()
        sync_dir(DEPLOY)
        emit('WIFI_LAN_UPDATE', 'SUCCESS')
    except Exception as exc:
        if committed:
            # The commit is durable. A leftover marker is cleaned on next boot.
            raise UpdateError('UPDATE_COMMITTED_STATUS_CLEANUP_REQUIRED') from None
        failure = error_code(exc)
        emit('WIFI_LAN_UPDATE', 'ROLLING_BACK code=' + failure)
        rollback(plan, start_manager=True)
        write_result('ROLLED_BACK', failure=failure)
        raise UpdateError('UPDATE_ROLLED_BACK') from None


def recover_boot():
    if not (DEPLOY / 'pending').exists():
        return
    verify_bundle(DEPLOY)
    result = obj(DEPLOY / 'result.json')
    if result['phase'] == 'COMMITTED':
        (DEPLOY / 'pending').unlink()
        sync_dir(DEPLOY)
        return
    rollback(obj(DEPLOY / 'plan.json'), start_manager=False)


def status():
    result = obj(DEPLOY / 'result.json')
    emit('WIFI_LAN_UPDATE_PHASE', result['phase'])
    emit('WIFI_LAN_UPDATE_FAILURE', result.get('failure', 'None'))
    emit('UPDATE_RECOVERY_PENDING', 'YES' if (DEPLOY / 'pending').exists() else 'NO')


def error_code(exc):
    text = str(exc)
    return text if re.fullmatch('[A-Z0-9_]+', text) else type(exc).__name__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('stage', 'activate', 'status', 'run', 'recover-boot'))
    args = parser.parse_args()
    os.umask(0o077)
    sys.dont_write_bytecode = True
    try:
        require(os.geteuid() == 0, 'RUN_WITH_SUDO')
        if args.action == 'status':
            status()
        else:
            with Path('/run/lock/carecall-wifi-lan-update.lock').open('a') as handle:
                # The detached runner may start before activate releases its lock.
                # It waits for that short hand-off; interactive commands fail fast.
                flags = fcntl.LOCK_EX if args.action in ('run', 'recover-boot') else fcntl.LOCK_EX | fcntl.LOCK_NB
                fcntl.flock(handle.fileno(), flags)
                {'stage': stage, 'activate': activate, 'run': perform_update,
                 'recover-boot': recover_boot}[args.action]()
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        emit('WIFI_LAN_PATCH', 'FAILED code=' + error_code(exc))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
