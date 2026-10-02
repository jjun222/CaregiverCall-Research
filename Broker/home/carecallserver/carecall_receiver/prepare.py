#!/usr/bin/env python3
"""Build a private, offline Netplan proposal. There is deliberately no apply action."""
import argparse
import configparser
import copy
import fcntl
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile

import yaml

VERSION = '20260929-persist-prepare-1'
R4 = '20260928-routertrial-4-routejson'
PACKAGE = Path(__file__).resolve().parent
INSTALLED = Path('/opt/carecall-wifi-aptrial')
PARENT = Path('/var/backups/carecall')
NETPLAN_TARGET = 'etc/netplan/50-cloud-init.yaml'
CLOUD_TARGET = 'etc/cloud/cloud.cfg.d/99-carecall-disable-network-config.cfg'
CLOUD_DRAFT = '# CareCall: manage network settings independently of cloud-init.\nnetwork:\n  config: disabled\n'


class PrepareError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise PrepareError(code)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_regular(path, private=False):
    """Never follow a final symlink or print parser errors containing credentials."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and
                not info.st_mode & 0o022 and info.st_size <= 2 * 1024 * 1024,
                'SOURCE_OWNERSHIP_OR_TYPE_INVALID')
        if private:
            require(not info.st_mode & 0o077, 'SECRET_FILE_PERMISSIONS_INVALID')
        return handle.read(2 * 1024 * 1024 + 1)


def read_json(path, private=True):
    try:
        data = json.loads(read_regular(path, private))
    except (ValueError, UnicodeError):
        raise PrepareError('JSON_PARSE_FAILED') from None
    require(isinstance(data, dict), 'JSON_MAPPING_REQUIRED')
    return data


def verify_bundle():
    manifest = {}
    for line in (PACKAGE / 'SHA256SUMS').read_text().splitlines():
        expected, name = line.split('  ', 1)
        relative = Path(name)
        require(not relative.is_absolute() and '..' not in relative.parts and
                name not in manifest and re.fullmatch('[0-9a-f]{64}', expected),
                'BUNDLE_MANIFEST_INVALID')
        path = PACKAGE / relative
        require(not path.is_symlink() and sha(path.read_bytes()) == expected,
                'BUNDLE_HASH_MISMATCH')
        manifest[name] = expected
    require({'prepare.py', 'r4_hashes.json', 'README_KO.md', 'tests/test_prepare.py'} <=
            set(manifest), 'BUNDLE_MANIFEST_INCOMPLETE')


def load_installed():
    hashes = json.loads((PACKAGE / 'r4_hashes.json').read_text())
    required = {'common.py', 'controller.py', 'router.py', 'web.py',
                'firewall.py', 'reviewed_firewall.json'}
    require(set(hashes) == required, 'R4_MANIFEST_INVALID')
    for directory in (INSTALLED.parent, INSTALLED):
        info = directory.lstat()
        require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and
                not info.st_mode & 0o022, 'INSTALLED_DIRECTORY_UNTRUSTED')
    for name, expected in hashes.items():
        require(sha(read_regular(INSTALLED / name)) == expected,
                'INSTALLED_R4_SOURCE_MISMATCH')
    # The entire installed module set was pinned before importing it.
    sys.path.insert(0, str(INSTALLED))
    import common
    import router
    import firewall
    require(common.VERSION == R4, 'R4_REQUIRED')
    return common, router, firewall


def run(argv, timeout=20):
    try:
        return subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=timeout,
                              env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.TimeoutExpired):
        raise PrepareError('COMMAND_UNAVAILABLE_OR_TIMED_OUT') from None


def ensure_idle(c):
    system = run(['/usr/bin/systemctl', 'is-system-running']).stdout.strip()
    # generate has a special early-boot path. Only permit a fully booted host.
    require(system in ('running', 'degraded'), 'BOOT_MUST_BE_COMPLETE')
    for suffix in ('test', 'guard', 'hostapd', 'dhcp', 'web', 'router'):
        result = run(['/usr/bin/systemctl', 'show', '-p', 'ActiveState', '--value',
                      c.PREFIX + suffix + '.service'])
        require(result.returncode == 0 and result.stdout.strip() == 'inactive',
                'TRIAL_MUST_BE_INACTIVE')
    require(not os.path.lexists(c.AP_NETWORK), 'AP_OVERRIDE_PRESENT')
    require(c.station_ready(), 'ORIGINAL_WIFI_NOT_READY')
    require(all(c.active(name + '.service') for name in c.CARECALL),
            'CARECALL_SERVICES_NOT_READY')


def validate_evidence(state, report, candidate):
    require(state.get('phase') == 'restored' and state.get('mode') == 'router' and
            state.get('restored') is True and state.get('confirmed') is True and
            state.get('failure') is None, 'RESTORED_ROUTER_TRIAL_REQUIRED')
    require(report.get('version') == R4 and report.get('mode') == 'router' and
            report.get('result') == 'PASS' and report.get('failure') is None and
            report.get('phone_confirmed') is True and report.get('restored') is True and
            report.get('persistent_wifi_changed') is False, 'R4_PASS_REQUIRED')
    for record in (state, report):
        require(all(record.get(key) is True for key in
                    ('router_connected', 'candidate_saved', 'sources_unchanged',
                     'candidate_identity_match')) and record.get('last_attempt') == 'CONNECTED' and
                record.get('last_control_result') == 'OK' and
                record.get('last_wpa_state') == 'COMPLETED' and
                record.get('last_readiness') == 'READY', 'COMPLETE_ROUTER_EVIDENCE_REQUIRED')
    test_id = report.get('test_id')
    require(isinstance(test_id, str) and re.fullmatch('[0-9a-f]{24}', test_id) and
            state.get('test_id') == test_id and candidate.get('test_id') == test_id,
            'CANDIDATE_TRIAL_ID_MISMATCH')
    require(candidate.get('version') == R4 and
            candidate.get('purpose') == 'candidate-only-not-boot-config',
            'TESTED_CANDIDATE_REQUIRED')
    ssid, psk = candidate.get('ssid'), candidate.get('psk_hex')
    require(isinstance(ssid, str) and 1 <= len(ssid.encode('utf-8')) <= 32 and
            not any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid), 'CANDIDATE_SSID_INVALID')
    require(isinstance(psk, str) and re.fullmatch('[0-9a-f]{64}', psk) and
            type(candidate.get('hidden')) is bool, 'CANDIDATE_CREDENTIALS_INVALID')


def proposal(document, candidate, country):
    """First persistent proposal must describe the same known laboratory Wi-Fi."""
    require(candidate.get('regulatory_domain') == country, 'CANDIDATE_COUNTRY_MISMATCH')
    wifi = document['network']['wifis']['wlan0']
    aps = wifi.get('access-points')
    require(isinstance(aps, dict) and len(aps) == 1 and candidate['ssid'] in aps,
            'CANDIDATE_MUST_MATCH_CURRENT_LAB_WIFI')
    ap = aps[candidate['ssid']]
    require(isinstance(ap, dict) and not set(ap) - {'password', 'auth', 'hidden', 'mode'},
            'CURRENT_AP_OPTIONS_REQUIRE_REVIEW')
    require(ap.get('mode', 'infrastructure') == 'infrastructure' and
            type(ap.get('hidden', False)) is bool and ap.get('hidden', False) == candidate['hidden'],
            'CURRENT_AP_MODE_OR_HIDDEN_MISMATCH')
    if 'auth' in ap:
        auth = ap['auth']
        require('password' not in ap and isinstance(auth, dict) and
                not set(auth) - {'key-management', 'password'} and
                auth.get('key-management') == 'psk', 'CURRENT_AP_AUTH_REQUIRES_REVIEW')
        password = auth.get('password')
    else:
        password = ap.get('password')
    require(isinstance(password, str), 'CURRENT_PSK_REQUIRED')
    if re.fullmatch('[0-9A-Fa-f]{64}', password):
        current_psk = password.lower()
    else:
        require(8 <= len(password) <= 63 and all(32 <= ord(ch) <= 126 for ch in password),
                'CURRENT_PSK_FORMAT_UNSUPPORTED')
        current_psk = hashlib.pbkdf2_hmac('sha1', password.encode('ascii'),
                                        candidate['ssid'].encode('utf-8'), 4096, 32).hex()
    require(hmac.compare_digest(current_psk, candidate['psk_hex']),
            'CANDIDATE_MUST_MATCH_CURRENT_LAB_WIFI')
    result = copy.deepcopy(document)
    result['network']['wifis']['wlan0']['access-points'] = {
        candidate['ssid']: {'auth': {'key-management': 'psk', 'password': candidate['psk_hex']},
                            'hidden': candidate['hidden']}}
    return result


def write_private(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('xb') as handle:
        os.chmod(path, 0o600)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def validate_generated(root, candidate):
    try:
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(read_regular(root / 'run/systemd/network/10-netplan-wlan0.network').decode())
        require(parser.get('Match', 'Name', fallback='') == 'wlan0' and
                parser.get('Network', 'DHCP', fallback='').lower() in ('yes', 'true', 'ipv4') and
                not parser.has_option('Network', 'Address') and not parser.has_section('Route'),
                'GENERATED_DHCP_LAYOUT_INVALID')
        config = read_regular(root / 'run/netplan/wpa-wlan0.conf', private=True).decode()
    except (OSError, UnicodeError, configparser.Error):
        raise PrepareError('GENERATED_FILES_UNAVAILABLE') from None
    psk_values = re.findall(r'^\s*psk=(\S+)\s*$', config, re.MULTILINE)
    require(psk_values == [candidate['psk_hex']], 'GENERATED_PSK_ENCODING_MISMATCH')
    require(len(re.findall(r'^\s*network=\{', config, re.MULTILINE)) == 1,
            'GENERATED_NETWORK_COUNT_INVALID')
    country = candidate.get('regulatory_domain')
    if country is not None:
        require([value.upper() for value in re.findall(r'^country=(\S+)\s*$', config, re.MULTILINE)] == [country],
                'GENERATED_COUNTRY_MISMATCH')


def generate_offline(directory, old_bytes, new_bytes, candidate, executable):
    require(directory.is_absolute() and directory != Path('/'), 'PRIVATE_ROOT_REQUIRED')
    for name, data in (('baseline-root', old_bytes), ('proposal-root', new_bytes)):
        root = directory / name
        write_private(root / NETPLAN_TARGET, data)
        for relative in ('lib/netplan', 'run/netplan', 'run/systemd/network'):
            (root / relative).mkdir(parents=True, exist_ok=True, mode=0o700)
        # Call the packaged renderer directly. Older netplan CLI wrappers can
        # daemon-reload even with --root-dir; no wrapper or generator argv[0].
        result = run([executable, '--root-dir', str(root)], timeout=40)
        # Netplan errors may quote the SSID or password: never echo stdout/stderr.
        require(result.returncode == 0, 'OFFLINE_NETPLAN_GENERATE_FAILED')
    validate_generated(directory / 'proposal-root', candidate)
    old = read_regular(directory / 'baseline-root/run/systemd/network/10-netplan-eth0.network')
    new = read_regular(directory / 'proposal-root/run/systemd/network/10-netplan-eth0.network')
    require(old == new, 'GENERATED_ETHERNET_CHANGED')
    write_private(directory / 'proposal-root' / CLOUD_TARGET, CLOUD_DRAFT.encode())


def source_snapshot(c, router):
    # The R4 hash inventory includes cloud-init configuration and the fixed AP key.
    hashes = router.source_hashes()
    paths = set(hashes) | {str(c.STATE), str(c.RESULT),
                           str(c.ETC / 'tested-router-candidate.json'), str(router.NETWORK),
                           '/run/netplan/wpa-wlan0.conf'}
    for pattern in ('/etc/systemd/system/carecall-wifi-aptrial-*.service', '/etc/ufw/*'):
        parent, mask = pattern.rsplit('/', 1)
        paths.update(str(p) for p in Path(parent).glob(mask) if p.is_file())
    data = {path: read_regular(Path(path)) for path in sorted(paths)}
    require(all(sha(data[path]) == value for path, value in hashes.items()),
            'SOURCES_CHANGED_DURING_READ')
    return hashes, data


def create_plan(c, router, firewall):
    ensure_idle(c)
    firewall.check()
    router.preflight()
    state = read_json(c.STATE)
    report = read_json(c.RESULT)
    candidate = read_json(c.ETC / 'tested-router-candidate.json')
    validate_evidence(state, report, candidate)
    hashes, snapshot = source_snapshot(c, router)
    require(state.get('source_hashes') == hashes, 'SOURCE_CHANGED_SINCE_SUCCESSFUL_TRIAL')
    try:
        document = yaml.safe_load(snapshot[str(router.NETPLAN)])
    except yaml.YAMLError:
        raise PrepareError('NETPLAN_PARSE_FAILED') from None
    require(set(document) == {'network'} and
            not set(document['network']) - {'version', 'renderer', 'ethernets', 'wifis'} and
            set(document['network'].get('ethernets', {})) == {'eth0'},
            'ONLY_REVIEWED_ETH0_WLAN0_LAYOUT_SUPPORTED')
    country = router.read_layout()
    desired = proposal(document, candidate, country)
    new_bytes = ('# CareCall managed Wi-Fi proposal; not applied by this tool.\n' +
                 yaml.safe_dump(desired, allow_unicode=True, sort_keys=False)).encode()
    require(yaml.safe_load(new_bytes) == desired, 'YAML_ROUNDTRIP_FAILED')
    # Cloud-init policy is a draft only; existing custom policy requires review.
    require(not os.path.lexists(Path('/') / CLOUD_TARGET), 'CLOUD_POLICY_TARGET_ALREADY_EXISTS')
    executable = '/usr/libexec/netplan/generate'
    require(Path(executable).is_file() and os.access(executable, os.X_OK),
            'NETPLAN_GENERATOR_MISSING')
    PARENT.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = PARENT.lstat()
    require(stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
            'BACKUP_PARENT_UNTRUSTED')
    directory = Path(tempfile.mkdtemp(prefix='wifi_persist_prepare_20260929_', dir=PARENT))
    print('PREPARE_DIR=' + str(directory), flush=True)
    print('PREPARE_CONTAINS_SECRETS=KEEP_ON_PI_DO_NOT_UPLOAD', flush=True)
    # This marker remains if generation or postchecks fail. It is never activation authority.
    write_private(directory / 'NOT_APPLIED.txt',
                  b'Offline proposal only. Do not copy it into /etc manually.\n')
    for name, data in snapshot.items():
        target = directory / 'original' / name.lstrip('/')
        write_private(target, data)
        require(sha(read_regular(target, private=True)) == sha(data), 'BACKUP_VERIFY_FAILED')
    generate_offline(directory, snapshot[str(router.NETPLAN)], new_bytes, candidate, executable)
    ensure_idle(c)
    firewall.check()
    after_hashes, after_snapshot = source_snapshot(c, router)
    require(hashes == after_hashes and snapshot == after_snapshot, 'LIVE_FILES_CHANGED_DURING_PREPARE')
    plan = {'version': VERSION, 'status': 'PREPARED_NOT_APPLIED', 'test_id': report['test_id'],
            'candidate_matches_current_wifi': True, 'sources': {k: sha(v) for k, v in snapshot.items()},
            'proposal_netplan_sha256': sha(new_bytes), 'proposal_cloud_sha256': sha(CLOUD_DRAFT.encode()),
            'targets': [NETPLAN_TARGET, CLOUD_TARGET], 'live_configuration_changed': False,
            'boot_recovery_installed': False, 'automatic_apply_authorized_by_this_file': False}
    write_private(directory / 'plan.json', (json.dumps(plan, indent=2) + '\n').encode())
    print('SUCCESSFUL_TRIAL_AND_CANDIDATE_MATCH=YES')
    print('CANDIDATE_MATCHES_CURRENT_WIFI=YES')
    print('PRIVATE_BACKUP_VERIFY=PASS')
    print('OFFLINE_NETPLAN_GENERATE=PASS')
    print('GENERATED_PSK_ENCODING=PASS')
    print('ETHERNET_AND_REGULATORY_DOMAIN_PRESERVED=YES')
    print('AP_PASSWORD_UNCHANGED=YES')
    print('NETPLAN_CLOUD_INIT_AND_UFW_UNCHANGED=YES')
    print('NETWORK_ACTIVATION_REQUESTED=NO')
    print('PERSISTENT_WIFI_CHANGED=NO')
    print('BOOT_RECOVERY_INSTALLED=NO')
    for name in c.CARECALL:
        print('SERVICE ' + name + '=active')
    print('WIFI_PERSIST_PREPARE=SUCCESS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare',))
    parser.parse_args()
    os.umask(0o077)
    sys.dont_write_bytecode = True
    c = None
    try:
        require(os.geteuid() == 0, 'RUN_WITH_SUDO')
        verify_bundle()
        c, router, firewall = load_installed()
        lock = c.RUN / 'network.lock'
        fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as handle:
            info = os.fstat(handle.fileno())
            require(stat.S_ISREG(info.st_mode) and info.st_uid == 0 and not info.st_mode & 0o022,
                    'NETWORK_LOCK_UNTRUSTED')
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PrepareError('NETWORK_OPERATION_IN_PROGRESS') from None
            create_plan(c, router, firewall)
        return 0
    except Exception as exc:
        if isinstance(exc, PrepareError) or (c is not None and isinstance(exc, c.TrialError)):
            code = str(exc)
        else:
            code = type(exc).__name__
        if not re.fullmatch('[A-Za-z0-9_]+', code):
            code = 'UNEXPECTED_ERROR'
        print('WIFI_PERSIST_PREPARE=FAILED code=' + code)
        print('NETWORK_ACTIVATION_REQUESTED=NO')
        print('SHARE_TERMINAL_OUTPUT_ONLY=YES')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
