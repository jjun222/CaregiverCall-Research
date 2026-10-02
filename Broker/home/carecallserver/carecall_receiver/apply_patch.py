#!/usr/bin/env python3
"""Install the router-input trial without starting it or changing Wi-Fi configuration."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
import firewall
import router

PAYLOAD = ('router.py', 'web.py', 'common.py', 'controller.py')
UNIT_PATH = Path('/etc/systemd/system') / router.UNIT


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unit_text():
    return f'''[Unit]
Description=CareCall bounded router connection trial
After=systemd-networkd.service
Conflicts=netplan-wpa-wlan0.service carecall-wifi-aptrial-hostapd.service
[Service]
Type=simple
User=root
ExecStart=/usr/sbin/wpa_supplicant -Dnl80211 -iwlan0 -c{c.RUN}/private/candidate.conf
Restart=no
TimeoutStopSec=5s
KillMode=control-group
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={c.RUN}
StandardOutput=null
StandardError=null
'''


def verify_bundle():
    names = set()
    for line in (PACKAGE / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or name in names:
            raise c.TrialError('INVALID_MANIFEST')
        target = PACKAGE / relative
        if target.is_symlink() or digest(target) != expected:
            raise c.TrialError('BUNDLE_HASH_MISMATCH')
        names.add(name)
    required = {'apply_patch.py', 'baseline_hashes.json', 'app/firewall.py', 'app/reviewed_firewall.json'}
    required.update('app/' + name for name in PAYLOAD)
    if not required <= names:
        raise c.TrialError('MANIFEST_INCOMPLETE')


def preflight():
    if os.geteuid() != 0:
        raise c.TrialError('RUN_WITH_SUDO')
    original = json.loads((PACKAGE / 'baseline_hashes.json').read_text())
    for name in set(PAYLOAD) | set(original):
        target = c.ROOT / name
        if name == 'router.py' and not target.exists():
            continue
        acceptable = {digest(PACKAGE / 'app' / name)}
        if name in original:
            acceptable.add(original[name])
        if target.is_symlink() or not target.is_file() or digest(target) not in acceptable:
            raise c.TrialError('INSTALLED_SOURCE_DIFFERS_' + name.replace('.', '_'))
    if UNIT_PATH.exists() and (UNIT_PATH.is_symlink() or UNIT_PATH.read_text() != unit_text()):
        raise c.TrialError('ROUTER_SERVICE_CONFLICT')
    for suffix in ('test', 'guard', 'hostapd', 'dhcp', 'web') + (('router',) if UNIT_PATH.exists() else ()):
        value = c.command('systemctl', 'show', '-p', 'ActiveState', '--value', c.PREFIX + suffix + '.service').stdout.strip()
        if value not in ('inactive', 'failed'):
            raise c.TrialError('TRIAL_MUST_BE_IDLE')
    state = c.read_state()
    if c.AP_NETWORK.exists() or (state and not state.get('restored')):
        raise c.TrialError('PREVIOUS_TRIAL_REQUIRES_RESTORE')
    if not c.station_ready() or not all(c.active(u + '.service') for u in c.CARECALL):
        raise c.TrialError('BASELINE_SERVICES_NOT_READY')
    c.settings()  # Validate but never print or regenerate the user's fixed AP password.
    firewall.check()
    return router.preflight()


def backup():
    parent = Path('/var/backups/carecall')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix='wifi_routertrial_20260928_', dir=parent))
    os.chmod(directory, 0o700)
    sources = [c.ROOT / name for name in PAYLOAD]
    sources += [UNIT_PATH, c.ETC / 'settings.json', c.RESULT, router.NETPLAN]
    saved = {}
    for source in sources:
        if not source.exists():
            continue
        destination = directory / str(source).lstrip('/')
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        os.chmod(destination, 0o600)
        if digest(destination) != digest(source):
            raise c.TrialError('BACKUP_VERIFY_FAILED')
        saved[str(destination.relative_to(directory))] = digest(destination)
    c.atomic(directory / 'SHA256.json', json.dumps(saved, indent=2) + '\n')
    print('PATCH_BACKUP_DIR=' + str(directory), flush=True)
    print('BACKUP_CONTAINS_SECRETS=KEEP_ON_PI_DO_NOT_UPLOAD', flush=True)


def apply():
    verify_bundle()
    before = preflight()
    if UNIT_PATH.exists() and all((c.ROOT / name).exists() and
            digest(c.ROOT / name) == digest(PACKAGE / 'app' / name) for name in PAYLOAD):
        print('WIFI_ROUTERTRIAL_PATCH=ALREADY_APPLIED')
        return
    result = json.loads(c.RESULT.read_text())
    if not (result.get('result') == 'PASS' and result.get('phone_confirmed') and result.get('restored')):
        raise c.TrialError('PASSED_PHONE_AP_TRIAL_REQUIRED')
    backup()
    targets = [c.ROOT / name for name in PAYLOAD] + [UNIT_PATH]
    old = {p: p.read_text() if p.exists() else None for p in targets}
    try:
        for name in PAYLOAD:
            c.atomic(c.ROOT / name, (PACKAGE / 'app' / name).read_text(), 0o644)
        c.atomic(UNIT_PATH, unit_text(), 0o644)
        c.command('systemctl', 'daemon-reload')
        firewall.check()
        if router.source_hashes() != before:
            raise c.TrialError('SOURCE_CONFIGURATION_CHANGED')
        if not c.station_ready() or not all(c.active(u + '.service') for u in c.CARECALL):
            raise c.TrialError('POSTCHECK_SERVICES_NOT_READY')
    except BaseException:
        restored = True
        for path, contents in old.items():
            try:
                if contents is None:
                    path.unlink(missing_ok=True)
                else:
                    c.atomic(path, contents, 0o644)
            except Exception:
                restored = False
        c.command('systemctl', 'daemon-reload', check=False)
        print('PATCH_CODE_ROLLBACK=' + ('SUCCESS' if restored else 'REQUIRES_REVIEW'), flush=True)
        raise
    print('WIFI_ROUTERTRIAL_PATCH=SUCCESS')
    print('VERSION=' + c.VERSION)
    print('FIREWALL_PRECHECK=PASS')
    print('AP_PASSWORD_UNCHANGED=YES')
    print('NETPLAN_AND_CLOUD_INIT_UNCHANGED=YES')
    print('UFW_RULES_UNCHANGED=YES')
    print('AP_ACTIVATED=NO')
    print('BOOT_AUTOSTART_ENABLED=NO')
    print('NEW_ACTION=test-router')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('apply',))
    parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        print('WIFI_ROUTERTRIAL_PATCH=FAILED code=RUN_WITH_SUDO')
        return 1
    try:
        with Path('/run/carecall-wifi-routertrial-patch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with c.network_lock():
                apply()
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, c.TrialError) else type(exc).__name__
        print('WIFI_ROUTERTRIAL_PATCH=FAILED code=' + code, file=sys.stderr)
        print('AP_ACTIVATED=NO; share this output before starting a trial.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
