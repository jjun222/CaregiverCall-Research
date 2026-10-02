#!/usr/bin/env python3
"""Stage isolated AP-trial files; never activate the trial during installation."""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import pwd
import secrets
import shutil
import subprocess
import sys

PACKAGE = Path(__file__).resolve().parent
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c


def file_hash(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def netplan_hashes():
    return {str(p): file_hash(p) for p in Path('/etc/netplan').glob('*.yaml')}


def verify_bundle():
    names = set()
    for line in (PACKAGE / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or name in names:
            raise c.TrialError('INVALID_BUNDLE_MANIFEST')
        path = PACKAGE / relative
        if path.is_symlink() or file_hash(path) != expected:
            raise c.TrialError('BUNDLE_HASH_MISMATCH')
        names.add(name)
    required = {'install.py', 'app/common.py', 'app/controller.py', 'app/web.py'}
    if not required.issubset(names):
        raise c.TrialError('BUNDLE_MANIFEST_INCOMPLETE')


def run(args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def preflight(backup):
    if os.geteuid() != 0:
        raise c.TrialError('RUN_WITH_SUDO')
    if sys.version_info < (3, 10):
        raise c.TrialError('PYTHON_TOO_OLD')
    if not Path('/usr/sbin/iw').is_file():
        raise c.TrialError('IW_REQUIRED')
    if not backup.is_dir() or not (backup / 'manifest.json').is_file():
        raise c.TrialError('VERIFIED_BACKUP_NOT_FOUND')
    if not str(backup.resolve()).startswith('/var/backups/carecall/wifi_prepare_'):
        raise c.TrialError('UNEXPECTED_BACKUP_PATH')
    manifest = json.loads((backup / 'manifest.json').read_text())
    if manifest.get('database_integrity') != 'ok':
        raise c.TrialError('DATABASE_BACKUP_NOT_VERIFIED')
    for field, filename in (('archive_sha256', 'code_and_configuration.tar.gz'),
                            ('database_sha256', 'carecall_events.db')):
        if file_hash(backup / filename) != manifest[field]:
            raise c.TrialError('BACKUP_HASH_MISMATCH')
    if not c.station_ready() or not all(c.active(u + '.service') for u in c.CARECALL):
        raise c.TrialError('CURRENT_SYSTEM_NOT_READY')
    if c.active('NetworkManager.service') or c.active('hostapd.service') or c.active('dnsmasq.service'):
        raise c.TrialError('EXISTING_NETWORK_SERVICE_REQUIRES_REVIEW')
    network = Path('/run/systemd/network/10-netplan-wlan0.network')
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.read_string(network.read_text())
    if parser.get('Match', 'Name', fallback='') != 'wlan0':
        raise c.TrialError('NETWORK_INTERFACE_MISMATCH')
    if parser.get('Network', 'DHCP', fallback='').lower() not in ('yes', 'true', 'ipv4'):
        raise c.TrialError('DHCP_BASELINE_REQUIRED')
    if parser.has_option('Network', 'Address'):
        raise c.TrialError('STATIC_ADDRESS_REQUIRES_REVIEW')
    if c.AP_NETWORK.exists():
        raise c.TrialError('AP_RUNTIME_FILE_ALREADY_EXISTS')
    if c.ETC.exists() or c.ROOT.exists():
        raise c.TrialError('ALREADY_STAGED_USE_STATUS')
    # Require an unused subnet on all interfaces before exposing a local DHCP server.
    import ipaddress
    addresses = json.loads(c.command('ip', '-j', '-4', 'address', 'show').stdout)
    subnet = ipaddress.ip_network(c.AP_IP + '/24', strict=False)
    for link in addresses:
        for item in link.get('addr_info', []):
            if item.get('family') == 'inet' and ipaddress.ip_network(
                    item['local'] + '/' + str(item['prefixlen']), strict=False).overlaps(subnet):
                raise c.TrialError('SETUP_SUBNET_CONFLICT')


def unit(description, command, *, user='root', writable='', timeout=8):
    return ('[Unit]\nDescription=' + description + '\nAfter=systemd-networkd.service\n'
            '[Service]\nType=simple\nUser=' + user + '\nExecStart=' + command + '\n'
            'Restart=no\nTimeoutStopSec=' + str(timeout) + '\nKillMode=control-group\n'
            'UMask=0077\nNoNewPrivileges=yes\nProtectSystem=strict\nProtectHome=yes\n'
            'PrivateTmp=yes\n' + ('ReadWritePaths=' + writable + '\n' if writable else '') +
            'StandardOutput=journal\nStandardError=journal\n')


def stage(backup):
    verify_bundle()
    preflight(backup)
    before = netplan_hashes()
    print('PRECHECK=SUCCESS', flush=True)
    # Mask only the vendor hostapd unit before installation; our trial uses a separate unit.
    vendor = Path('/etc/systemd/system/hostapd.service')
    if os.path.lexists(vendor) and not (vendor.is_symlink() and os.readlink(vendor) == '/dev/null'):
        raise c.TrialError('EXISTING_HOSTAPD_UNIT_REQUIRES_REVIEW')
    vendor_created = not os.path.lexists(vendor)
    if vendor_created:
        vendor.symlink_to('/dev/null')
    c.command('systemctl', 'daemon-reload')
    env = dict(os.environ, NEEDRESTART_MODE='l', LC_ALL='C')
    run(['/usr/bin/apt-get', '-o', 'APT::Update::Error-Mode=any', 'update'], env=env)
    run(['/usr/bin/apt-get', 'install', '-y', '--no-install-recommends', '--no-upgrade',
         '--no-remove', 'hostapd', 'dnsmasq-base'], env=env)
    for executable in ('/usr/sbin/hostapd', '/usr/sbin/dnsmasq'):
        if not Path(executable).is_file():
            raise c.TrialError('DEPENDENCY_EXECUTABLE_MISSING')
    try:
        account = pwd.getpwnam('carecall-wifi-web')
        if account.pw_shell not in ('/usr/sbin/nologin', '/sbin/nologin'):
            raise c.TrialError('WEB_ACCOUNT_CONFLICT')
    except KeyError:
        run(['/usr/sbin/useradd', '--system', '--user-group', '--no-create-home',
             '--home-dir', '/nonexistent', '--shell', '/usr/sbin/nologin', 'carecall-wifi-web'])
    c.ROOT.mkdir(mode=0o755)
    os.chmod(c.ROOT, 0o755)
    for name in ('common.py', 'controller.py', 'web.py'):
        source = PACKAGE / 'app' / name
        shutil.copyfile(source, c.ROOT / source.name)
        os.chmod(c.ROOT / source.name, 0o644)
    c.ETC.mkdir(mode=0o700)
    config = {'version': c.VERSION, 'ssid': 'CareCall-Pi-' + secrets.token_hex(3),
              'password': ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789') for _ in range(20)),
              'backup_dir': str(backup), 'vendor_hostapd_mask_created': vendor_created}
    c.atomic(c.ETC / 'settings.json', json.dumps(config) + '\n')
    c.RUN.mkdir(exist_ok=True, mode=0o755)
    os.chmod(c.RUN, 0o755)
    c.atomic(Path('/etc/tmpfiles.d/carecall-wifi-aptrial.conf'),
             f'd {c.RUN} 0755 root root -\n', 0o644)
    writable = f'{c.RUN} /run/systemd/network {c.ETC}'
    definitions = {
        'test': unit('CareCall temporary Wi-Fi AP trial', f'/usr/bin/python3 -B {c.ROOT}/controller.py run-test', writable=writable, timeout=3),
        'guard': unit('CareCall independent AP trial recovery', f'/usr/bin/python3 -B {c.ROOT}/controller.py guard', writable=writable, timeout=3),
        'hostapd': unit('CareCall isolated setup AP', f'/usr/sbin/hostapd {c.RUN}/hostapd.conf', writable=str(c.RUN)),
        'dhcp': unit('CareCall setup AP DHCP', f'/usr/sbin/dnsmasq --no-daemon --conf-file={c.RUN}/dnsmasq.conf', writable=str(c.RUN)),
        'web': unit('CareCall unprivileged setup connection page', f'/usr/bin/python3 -B {c.ROOT}/web.py',
                    user='carecall-wifi-web', writable=str(c.RUN / 'public')),
    }
    for suffix, definition in definitions.items():
        if suffix == 'guard':
            definition = definition.replace('[Unit]\n', '[Unit]\nStartLimitIntervalSec=0\n')
            definition = definition.replace('Restart=no', 'Restart=on-failure\nRestartSec=2s')
        if suffix in ('hostapd', 'dhcp', 'web'):
            definition = definition.replace('StandardOutput=journal', 'StandardOutput=null').replace('StandardError=journal', 'StandardError=null')
        c.atomic(Path('/etc/systemd/system') / (c.PREFIX + suffix + '.service'), definition, 0o644)
    c.command('systemctl', 'daemon-reload')
    if before != netplan_hashes():
        raise c.TrialError('NETPLAN_SOURCE_CHANGED')
    if not c.station_ready() or not all(c.active(u + '.service') for u in c.CARECALL):
        raise c.TrialError('POSTCHECK_NOT_READY')
    print('WIFI_APTRIAL_STAGE=SUCCESS')
    print('NETPLAN_SOURCE_UNCHANGED=YES')
    print('AP_ACTIVATED=NO')
    print('BOOT_AUTOSTART_ENABLED=NO')
    print('SETUP_CARD=AVAILABLE_PRIVATELY')
    run(['/usr/bin/python3', '-B', str(c.ROOT / 'controller.py'), 'status'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('stage',))
    parser.add_argument('--backup-dir', required=True, type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    try:
        stage(args.backup_dir)
        return 0
    except Exception as exc:
        print('WIFI_APTRIAL_STAGE=FAILED code=' + (str(exc) if isinstance(exc, c.TrialError) else type(exc).__name__), file=sys.stderr)
        print('AP activation was not requested. Send this terminal output; do not change Wi-Fi manually.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
