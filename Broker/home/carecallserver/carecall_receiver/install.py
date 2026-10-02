#!/usr/bin/env python3
"""Stage the Wi-Fi manager and preserve the verified R4 configuration."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile

PACKAGE = Path(__file__).resolve().parent
ROOT = Path('/opt/carecall-wifi-manager')
ETC = Path('/etc/carecall-wifi-manager')
SYSTEM = Path('/etc/systemd/system')
SERVICE = 'carecall-wifi-manager.service'
OWNER = ETC / 'owner'
spec = importlib.util.spec_from_file_location('baseline', PACKAGE / 'baseline.py')
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)


def common_unit(description, executable, writable, user='root', extra=''):
    return f'''[Unit]
Description={description}
BindsTo={SERVICE}
After={SERVICE} systemd-networkd.service
ConditionPathExists={OWNER}
{extra}[Service]
Type=simple
User={user}
ExecStart={executable}
Restart=no
TimeoutStopSec=5s
KillMode=control-group
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths={writable}
StandardOutput=null
StandardError=null
'''


def units():
    run = '/run/carecall-wifi-manager'
    result = {SERVICE: f'''[Unit]
Description=CareCall persistent Wi-Fi and phone recovery
Wants=systemd-networkd.service
After=systemd-networkd.service cloud-init-local.service ufw.service
Conflicts=carecall-wifi-aptrial-test.service carecall-wifi-aptrial-guard.service carecall-wifi-aptrial-hostapd.service carecall-wifi-aptrial-dhcp.service carecall-wifi-aptrial-web.service carecall-wifi-aptrial-router.service
ConditionPathExists={OWNER}
StartLimitIntervalSec=0
[Service]
Type=notify
NotifyAccess=main
ExecStart=/usr/bin/python3 -B {ROOT}/manager.py run
Restart=always
RestartSec=5s
WatchdogSec=120s
TimeoutStartSec=30s
TimeoutStopSec=10s
KillMode=control-group
RuntimeDirectory=carecall-wifi-manager
RuntimeDirectoryMode=0755
RuntimeDirectoryPreserve=yes
UMask=0077
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
ReadWritePaths=/run /etc/netplan /etc/cloud/cloud.cfg.d {ETC}
StandardOutput=journal
StandardError=null
[Install]
WantedBy=multi-user.target
'''}
    for suffix, binary, writable, user, extra in (
        ('hostapd', f'/usr/sbin/hostapd {run}/hostapd.conf', run, 'root', ''),
        ('dhcp', f'/usr/sbin/dnsmasq --no-daemon --conf-file={run}/dnsmasq.conf', run, 'root', ''),
        ('web', f'/usr/bin/python3 -B {ROOT}/web.py', run + '/public', 'carecall-wifi-web', ''),
        ('router', f'/usr/sbin/wpa_supplicant -Dnl80211 -iwlan0 -c{run}/private/candidate.conf',
         run, 'root', 'Conflicts=netplan-wpa-wlan0.service carecall-wifi-manager-hostapd.service\n')):
        result['carecall-wifi-manager-' + suffix + '.service'] = common_unit(
            'CareCall Wi-Fi setup ' + suffix, binary, writable, user, extra)
    return result


def verify_bundle():
    names = set()
    for line in (PACKAGE / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        relative = Path(name)
        p.require(not relative.is_absolute() and '..' not in relative.parts and name not in names,
                  'MANIFEST_INVALID')
        target = PACKAGE / relative
        p.require(not target.is_symlink() and p.sha(target.read_bytes()) == expected, 'BUNDLE_HASH_MISMATCH')
        names.add(name)
    required = {'install.py', 'baseline.py', 'r4_hashes.json', 'README_KO.md',
                'app/manager.py', 'app/transaction.py', 'app/common.py', 'app/router.py',
                'app/web.py', 'app/firewall.py', 'app/reviewed_firewall.json'}
    p.require(required <= names, 'MANIFEST_INCOMPLETE')


def guard_legacy(text):
    anchor = "    action = parser.parse_args().action\n"
    p.require(text.count(anchor) == 1, 'LEGACY_CONTROLLER_LAYOUT_CHANGED')
    return text.replace(anchor, anchor +
        "    if action not in ('status', 'show-setup') and Path('/etc/carecall-wifi-manager/owner').exists():\n"
        "        print('ERROR=WIFI_MANAGER_OWNS_NETWORK_USE_MANAGER_COMMAND')\n"
        "        return 1\n")


def stage(prepared):
    p.require(os.geteuid() == 0, 'RUN_WITH_SUDO')
    verify_bundle()
    if ROOT.exists() or ETC.exists():
        marker = ETC / 'install-complete.json'
        p.require(marker.is_file(), 'PARTIAL_INSTALL_REQUIRES_REVIEW')
        installed = p.read_json(marker)
        p.require(installed.get('package_sha256') == p.sha((PACKAGE / 'SHA256SUMS').read_bytes()),
                  'DIFFERENT_MANAGER_VERSION_INSTALLED')
        for path in (PACKAGE / 'app').iterdir():
            p.require(p.sha(p.read_regular(ROOT / path.name)) == p.sha(path.read_bytes()),
                      'INSTALLED_MANAGER_CHANGED')
        for name, value in units().items():
            p.require(p.read_regular(SYSTEM / name).decode() == value, 'INSTALLED_UNIT_CHANGED')
        legacy = Path('/opt/carecall-wifi-aptrial/controller.py')
        p.require(p.sha(p.read_regular(legacy)) == installed['legacy_guarded_sha256'],
                  'LEGACY_GUARD_CHANGED')
        print('WIFI_PERSIST_STAGE=ALREADY_STAGED')
        return
    c, router, firewall = p.load_installed()
    p.ensure_idle(c)
    firewall.check()
    router.preflight()
    candidate = p.read_json(c.ETC / 'tested-router-candidate.json')
    state, report = p.read_json(c.STATE), p.read_json(c.RESULT)
    p.validate_evidence(state, report, candidate)
    p.require(prepared.is_absolute() and prepared.parent == p.PARENT and
              prepared.name.startswith('wifi_persist_prepare_20260929_') and not prepared.is_symlink(),
              'UNEXPECTED_PREPARE_PATH')
    info = prepared.stat()
    p.require(info.st_uid == 0 and info.st_mode & 0o077 == 0, 'PREPARE_DIRECTORY_NOT_PRIVATE')
    plan = p.read_json(prepared / 'plan.json')
    p.require(plan.get('version') == p.VERSION and plan.get('status') == 'PREPARED_NOT_APPLIED' and
              plan.get('test_id') == candidate['test_id'] and
              plan.get('candidate_matches_current_wifi') is True, 'VERIFIED_PLAN_REQUIRED')
    for path, expected in plan['sources'].items():
        p.require(p.sha(p.read_regular(Path(path))) == expected, 'SOURCE_CHANGED_SINCE_PREPARE')
    sources = router.source_hashes()
    p.require(sources == state['source_hashes'], 'SOURCE_CHANGED_SINCE_TRIAL')
    document = p.yaml.safe_load(p.read_regular(router.NETPLAN))
    desired = p.proposal(document, candidate, router.read_layout())
    proposal = p.read_regular(prepared / 'proposal-root' / p.NETPLAN_TARGET, private=True)
    p.require(p.sha(proposal) == plan['proposal_netplan_sha256'] and
              p.yaml.safe_load(proposal) == desired, 'PREPARED_PROPOSAL_CHANGED')
    p.require(p.read_regular(prepared / 'proposal-root' / p.CLOUD_TARGET, private=True).decode() ==
              p.CLOUD_DRAFT, 'PREPARED_CLOUD_POLICY_CHANGED')
    p.require(not os.path.lexists(Path('/') / p.CLOUD_TARGET), 'CLOUD_POLICY_ALREADY_EXISTS')
    for name in units():
        p.require(not os.path.lexists(SYSTEM / name) and
                  not (SYSTEM / (name + '.d')).exists(), 'MANAGER_UNIT_ALREADY_EXISTS')
    legacy = c.ROOT / 'controller.py'
    original = p.read_regular(legacy)
    patched = guard_legacy(original.decode()).encode()
    backup = Path(tempfile.mkdtemp(prefix='wifi_persist_install_20260929_', dir=p.PARENT))
    for name, data in ((str(legacy), original), (str(router.NETPLAN), p.read_regular(router.NETPLAN)),
                       (str(c.ETC / 'settings.json'), p.read_regular(c.ETC / 'settings.json'))):
        p.write_private(backup / name.lstrip('/'), data)
    print('INSTALL_BACKUP_DIR=' + str(backup), flush=True)
    print('BACKUP_CONTAINS_SECRETS=KEEP_ON_PI_DO_NOT_UPLOAD', flush=True)
    created_units = []
    try:
        ROOT.mkdir(mode=0o755)
        os.chmod(ROOT, 0o755)  # The unprivileged web service must traverse its code directory.
        ETC.mkdir(mode=0o700)
        for source in sorted((PACKAGE / 'app').iterdir()):
            p.require(source.is_file() and not source.is_symlink(), 'UNEXPECTED_APP_FILE')
            c.atomic(ROOT / source.name, source.read_text(), 0o644)
        c.atomic(ETC / 'settings.json', p.read_regular(c.ETC / 'settings.json').decode())
        initial = {key: candidate.get(key) for key in ('ssid', 'psk_hex', 'hidden', 'regulatory_domain')}
        c.atomic(ETC / 'profile.json', json.dumps(initial) + '\n')
        # Actual activation checks freshness again; stage does not alter these sources.
        activation_hashes = {name: value for name, value in sources.items()}
        c.atomic(ETC / 'install-baseline.json', json.dumps({
            'prepare_dir': str(prepared), 'backup_dir': str(backup),
            'activation_hashes': activation_hashes}) + '\n')
        for name, value in units().items():
            c.atomic(SYSTEM / name, value, 0o644)
            created_units.append(SYSTEM / name)
        c.atomic(legacy, patched.decode(), 0o644)
        c.command('systemctl', 'daemon-reload')
        p.require(router.source_hashes() == sources and c.station_ready() and
                  all(c.active(name + '.service') for name in c.CARECALL), 'STAGE_POSTCHECK_FAILED')
        c.atomic(ETC / 'install-complete.json', json.dumps({
            'package_sha256': p.sha((PACKAGE / 'SHA256SUMS').read_bytes()),
            'legacy_original_sha256': p.sha(original), 'legacy_guarded_sha256': p.sha(patched)}) + '\n')
    except Exception:
        c.atomic(legacy, original.decode(), 0o644)
        for path in created_units:
            path.unlink(missing_ok=True)
        for path in (ROOT, ETC):
            if path.exists():
                shutil.rmtree(path)
        c.command('systemctl', 'daemon-reload', check=False)
        raise
    print('WIFI_PERSIST_STAGE=SUCCESS')
    print('NETPLAN_CLOUD_INIT_AND_UFW_UNCHANGED=YES')
    print('AP_PASSWORD_UNCHANGED=YES')
    print('NETWORK_ACTIVATED=NO')
    print('BOOT_AUTOSTART_ENABLED=NO')
    print('NEXT_ACTION=ACTIVATE')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stage',))
    parser.add_argument('--prepare-dir', type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    sys.dont_write_bytecode = True
    try:
        stage(args.prepare_dir)
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, p.PrepareError) or type(exc).__name__ == 'TrialError' else type(exc).__name__
        code = code if re.fullmatch('[A-Za-z0-9_]+', code) else 'UNEXPECTED_ERROR'
        print('WIFI_PERSIST_STAGE=FAILED code=' + code)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
