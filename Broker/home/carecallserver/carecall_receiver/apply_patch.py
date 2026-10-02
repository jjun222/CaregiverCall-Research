#!/usr/bin/env python3
"""Apply four narrow UFW additions and update the previously staged AP trial."""
import argparse
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
import firewall as fw

ORIGINAL = {
    'common.py': '407da400741054840f4fd8cd0d6346f79359343ded8cc68e7e13d3fe3b65af6b',
    'controller.py': 'b399de44d80dd6eda76c8e2278de1a9180848a7b046dbab692a867b12fde2d40',
    'web.py': '16f214a5db4d968df54008ed48a2470247516d6e0addcb1393c0722b0913394e',
}
PAYLOAD = ('firewall.py', 'reviewed_firewall.json', 'common.py', 'controller.py')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def verify_bundle():
    names = set()
    for line in (PACKAGE / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or name in names:
            raise c.TrialError('INVALID_BUNDLE_MANIFEST')
        target = PACKAGE / path
        if target.is_symlink() or digest(target) != expected:
            raise c.TrialError('BUNDLE_HASH_MISMATCH')
        names.add(name)
    required = {'apply_patch.py', *(('app/' + name) for name in PAYLOAD), 'app/web.py'}
    if not required <= names:
        raise c.TrialError('BUNDLE_MANIFEST_INCOMPLETE')


def preflight():
    if os.geteuid() != 0:
        raise c.TrialError('RUN_WITH_SUDO')
    if not (c.ETC / 'settings.json').is_file():
        raise c.TrialError('ORIGINAL_APTRIAL_STAGE_REQUIRED')
    if c.AP_NETWORK.exists():
        raise c.TrialError('AP_TRIAL_MUST_BE_STOPPED')
    for suffix in ('test', 'guard', 'hostapd', 'dhcp', 'web'):
        result = c.command('systemctl', 'show', '-p', 'ActiveState', '--value',
                           c.PREFIX + suffix + '.service')
        if result.stdout.strip() not in ('inactive', 'failed'):
            raise c.TrialError('AP_TRIAL_MUST_BE_STOPPED')
    if not c.station_ready() or not all(c.active(u + '.service') for u in c.CARECALL):
        raise c.TrialError('ORIGINAL_SERVICES_NOT_READY')
    lab = ipaddress.ip_network('192.168.0.0/24')
    if not any(ipaddress.ip_address(a) in lab for a in c.ipv4_addresses()):
        raise c.TrialError('RESEARCH_LAB_NETWORK_REQUIRED')
    for name, original_hash in ORIGINAL.items():
        target = c.ROOT / name
        if target.is_symlink() or not target.is_file():
            raise c.TrialError('INSTALLED_FILE_MISSING_OR_SYMLINK')
        if digest(target) not in (original_hash, digest(PACKAGE / 'app' / name)):
            raise c.TrialError('INSTALLED_SOURCE_DIFFERS_' + name.replace('.', '_'))
    for name in ('firewall.py', 'reviewed_firewall.json'):
        target = c.ROOT / name
        if target.exists() and (target.is_symlink() or digest(target) != digest(PACKAGE / 'app' / name)):
            raise c.TrialError('EXISTING_PATCH_FILE_DIFFERS')
    old = c.read_state()
    if old and not old.get('restored'):
        raise c.TrialError('PREVIOUS_AP_TRIAL_REQUIRES_RESTORE')


def snapshot_netplan():
    return {str(p): digest(p) for p in Path('/etc/netplan').glob('*.yaml')}


def backup_files(status, raw):
    parent = Path('/var/backups/carecall')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix='wifi_ufw_patch_20260928_', dir=parent))
    os.chmod(root, 0o700)
    (root / 'app').mkdir(mode=0o700)
    for name in set(PAYLOAD) | {'web.py'}:
        source = c.ROOT / name
        if source.is_file():
            shutil.copyfile(source, root / 'app' / name)
            os.chmod(root / 'app' / name, 0o600)
    shutil.copytree('/etc/ufw', root / 'ufw', symlinks=True)
    c.atomic(root / 'ufw-status-before.txt', status)
    c.atomic(root / 'ufw-raw-before.txt', raw)
    manifest = {str(p.relative_to(root)): digest(p) for p in root.rglob('*')
                if p.is_file() and not p.is_symlink()}
    c.atomic(root / 'SHA256.json', json.dumps(manifest, indent=2) + '\n')
    for name, expected in manifest.items():
        if digest(root / name) != expected:
            raise c.TrialError('PATCH_BACKUP_VERIFY_FAILED')
    print('PATCH_BACKUP_DIR=' + str(root), flush=True)
    return root


def apply_rules(start_count, attempted):
    for index in range(start_count, len(fw.RULES)):
        # Record intent first: a process can add a rule and still exit with error.
        attempted.append(index)
        fw.run_ufw(*fw.rule_args(fw.RULES[index]))
        count, _, _ = fw.inspect()
        if count != index + 1:
            raise c.TrialError('UFW_POST_ADD_MISMATCH')


def rollback_rules(attempted, start_count):
    for index in reversed(attempted):
        try:
            fw.run_ufw('--force', 'delete', *fw.rule_args(fw.RULES[index]))
        except c.TrialError:
            pass
    try:
        count, _, _ = fw.inspect()
        return count == start_count
    except c.TrialError:
        return False


def apply():
    verify_bundle()
    preflight()
    start_count, before_status, before_raw = fw.inspect()
    before_netplan = snapshot_netplan()
    if start_count == len(fw.RULES) and all(
            (c.ROOT / name).is_file() and digest(c.ROOT / name) == digest(PACKAGE / 'app' / name)
            for name in PAYLOAD):
        print('WIFI_UFW_PATCH=ALREADY_APPLIED')
        print('FIREWALL_PRECHECK=PASS')
        print('AP_ACTIVATED=NO')
        return
    # UFW validates the exact syntax before any firewall mutation.
    for rule in fw.RULES[start_count:]:
        fw.run_ufw('--dry-run', *fw.rule_args(rule))
    backup_files(before_status, before_raw)
    old_code = {name: (c.ROOT / name).read_text() for name in ('common.py', 'controller.py')}
    attempted = []
    try:
        apply_rules(start_count, attempted)
        # Install dependencies first; controller.py is the commit point.
        for name in PAYLOAD:
            c.atomic(c.ROOT / name, (PACKAGE / 'app' / name).read_text(), 0o644)
        fw.check()
        if before_netplan != snapshot_netplan():
            raise c.TrialError('NETPLAN_SOURCE_CHANGED')
        if not c.station_ready() or not all(c.active(u + '.service') for u in c.CARECALL):
            raise c.TrialError('POSTCHECK_SERVICES_NOT_READY')
    except BaseException:
        code_ok = True
        for name in ('controller.py', 'common.py'):
            try:
                c.atomic(c.ROOT / name, old_code[name], 0o644)
            except Exception:
                code_ok = False
        rules_ok = rollback_rules(attempted, start_count)
        print('PATCH_CODE_ROLLBACK=' + ('SUCCESS' if code_ok else 'REQUIRES_REVIEW'), flush=True)
        print('PATCH_RULE_ROLLBACK=' + ('SUCCESS' if rules_ok else 'REQUIRES_REVIEW'), flush=True)
        raise
    print('WIFI_UFW_PATCH=SUCCESS')
    print('VERSION=' + c.VERSION)
    print('FIREWALL_PRECHECK=PASS')
    print('UFW_ACTIVE=YES')
    print('REVIEWED_FIREWALL_PRESERVED=YES')
    print('SETUP_AP_ALLOW_RULES=4')
    print('NETPLAN_SOURCE_UNCHANGED=YES')
    print('AP_ACTIVATED=NO')
    print('BOOT_AUTOSTART_ENABLED=NO')
    for unit in c.CARECALL:
        print('SERVICE ' + unit + '=active')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('apply',))
    parser.parse_args()
    os.umask(0o077)
    if os.geteuid() != 0:
        print('WIFI_UFW_PATCH=FAILED code=RUN_WITH_SUDO')
        return 1
    try:
        with Path('/run/carecall-wifi-ufw-patch.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            apply()
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, c.TrialError) else type(exc).__name__
        print('WIFI_UFW_PATCH=FAILED code=' + code, file=sys.stderr)
        print('AP_ACTIVATED=NO; send this output before starting the AP trial.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
