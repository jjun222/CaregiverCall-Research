"""Bounded WPA2 router trial. Never writes Netplan/cloud-init or UFW settings."""
import configparser
import hashlib
import hmac
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time

import common as c

TRIAL_SECONDS = 900
CONNECT_SECONDS = 75
UNIT = c.PREFIX + 'router.service'
NETPLAN = Path('/etc/netplan/50-cloud-init.yaml')
NETWORK = Path('/run/systemd/network/10-netplan-wlan0.network')


def private():
    return c.RUN / 'private'


def credentials(body):
    if not isinstance(body, dict) or set(body) != {'token', 'ssid', 'password', 'hidden'}:
        raise c.TrialError('INVALID_REQUEST')
    ssid, password = body['ssid'], body['password']
    if not isinstance(ssid, str) or not isinstance(password, str) or type(body['hidden']) is not bool:
        raise c.TrialError('INVALID_REQUEST')
    try:
        encoded = ssid.encode('utf-8')
    except UnicodeError:
        raise c.TrialError('INVALID_SSID') from None
    if not 1 <= len(encoded) <= 32 or any(ord(ch) < 32 or ord(ch) == 127 for ch in ssid):
        raise c.TrialError('INVALID_SSID')
    if re.fullmatch(r'[0-9A-Fa-f]{64}', password):
        psk = password.lower()
    elif 8 <= len(password) <= 63 and all(32 <= ord(ch) <= 126 for ch in password):
        psk = hashlib.pbkdf2_hmac('sha1', password.encode('ascii'), encoded, 4096, 32).hex()
    else:
        raise c.TrialError('INVALID_ROUTER_PASSWORD')
    return {'ssid': ssid, 'psk_hex': psk, 'hidden': body['hidden']}


def supplicant_config(candidate, test_id):
    if not re.fullmatch(r'[0-9a-f]{24}', test_id):
        raise c.TrialError('INVALID_TEST_ID')
    if not re.fullmatch(r'[0-9a-f]{64}', candidate['psk_hex']):
        raise c.TrialError('INVALID_PSK')
    # Values supplied by the user are encoded as hex, never interpolated as directives.
    return (f'ctrl_interface={private()}/ctrl\nupdate_config=0\nap_scan=1\nnetwork={{\n'
            f'    ssid={candidate["ssid"].encode("utf-8").hex()}\n'
            f'    psk={candidate["psk_hex"]}\n'
            f'    scan_ssid={1 if candidate["hidden"] else 0}\n'
            f'    id_str="carecall-{test_id}"\n'
            '    key_mgmt=WPA-PSK\n    proto=RSN\n    pairwise=CCMP\n    group=CCMP\n}\n')


def source_hashes():
    paths = [NETPLAN, c.ETC / 'settings.json']
    paths += sorted(Path('/etc/cloud/cloud.cfg.d').glob('*.cfg'))
    paths += [p for p in (Path('/etc/cloud/cloud.cfg'), Path('/boot/firmware/network-config')) if p.is_file()]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def preflight():
    import yaml
    actual = [p for directory in ('/lib/netplan', '/etc/netplan', '/run/netplan')
              for p in Path(directory).glob('*.yaml')]
    if actual != [NETPLAN] or NETPLAN.is_symlink():
        raise c.TrialError('NETPLAN_LAYOUT_CHANGED')
    try:
        n = yaml.safe_load(NETPLAN.read_text())['network']
        w = n['wifis']['wlan0']
        if (n.get('version') != 2 or n.get('renderer', 'networkd') != 'networkd'
                or set(n['wifis']) != {'wlan0'} or w.get('renderer', 'networkd') != 'networkd'
                or w.get('dhcp4') is not True
                or set(w) - {'dhcp4', 'dhcp6', 'optional', 'access-points', 'renderer'}):
            raise ValueError()
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.read_string(NETWORK.read_text())
        if (parser.get('Match', 'Name', fallback='') != 'wlan0'
                or parser.get('Network', 'DHCP', fallback='').lower() not in ('yes', 'true', 'ipv4')
                or parser.has_option('Network', 'Address') or parser.has_section('Address')
                or parser.has_section('Route')):
            raise ValueError()
    except Exception:
        raise c.TrialError('EXPECTED_DHCP_LAYOUT_REQUIRED') from None
    for name in ('wpa_cli', 'wpa_supplicant'):
        if not Path('/usr/sbin/' + name).is_file():
            raise c.TrialError('MISSING_' + name.upper())
    addresses = json.loads(c.command('ip', '-j', '-4', 'address', 'show').stdout)
    ap_subnet = ipaddress.ip_network(c.AP_IP + '/24', strict=False)
    for link in addresses:
        for address in link.get('addr_info', []):
            if address.get('family') == 'inet' and ipaddress.ip_network(
                    f'{address["local"]}/{address["prefixlen"]}', strict=False).overlaps(ap_subnet):
                raise c.TrialError('SETUP_SUBNET_CONFLICT')
    return source_hashes()


def prepare(state):
    state.update(mode='router', deadline=c.now() + TRIAL_SECONDS, attempts=0,
                 router_connected=False, candidate_saved=False, last_attempt='NOT_STARTED',
                 last_wpa_state='NOT_STARTED',
                 sources_unchanged=None, source_hashes=preflight())
    private().mkdir(mode=0o700, parents=True, exist_ok=True)
    if private().is_symlink() or private().stat().st_uid != 0:
        raise c.TrialError('PRIVATE_DIRECTORY_INVALID')
    os.chmod(private(), 0o700)
    (private() / 'candidate.conf').unlink(missing_ok=True)
    (c.RUN / 'public' / 'request.json').unlink(missing_ok=True)
    return state


def publish(state, notice):
    data = c.public_state(state['test_id'], state['token'])
    data.update(mode='router', notice=notice, attempts=state['attempts'],
                can_submit=not state['router_connected'],
                seconds_remaining=max(0, int(state['deadline'] - c.now())))
    path = c.RUN / 'public' / 'session.json'
    c.atomic(path, json.dumps(data), 0o640)
    os.chown(path, 0, pwd.getpwnam('carecall-wifi-web').pw_gid)


def take_request(state):
    path = c.RUN / 'public' / 'request.json'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError:
        raise c.TrialError('INVALID_REQUEST_FILE') from None
    try:
        with os.fdopen(fd, 'rb') as f:
            info = os.fstat(f.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 4096
                    or info.st_uid != pwd.getpwnam('carecall-wifi-web').pw_uid):
                raise c.TrialError('INVALID_REQUEST_FILE')
            try:
                body = json.loads(f.read(4097))
            except (ValueError, UnicodeError):
                raise c.TrialError('INVALID_REQUEST') from None
        if (not isinstance(body, dict) or not isinstance(body.get('token'), str)
                or not hmac.compare_digest(body['token'], state['token'])):
            raise c.TrialError('INVALID_REQUEST_TOKEN')
        return credentials(body)
    finally:
        path.unlink(missing_ok=True)


def stop_candidate():
    c.command('systemctl', 'stop', UNIT, check=False)
    if c.active(UNIT):
        raise c.TrialError('CANDIDATE_SERVICE_DID_NOT_STOP')


def remove_ap_override():
    if c.AP_NETWORK.exists():
        if not c.AP_NETWORK.read_text().startswith(c.MARKER):
            raise c.TrialError('UNEXPECTED_AP_RUNTIME_FILE')
        c.AP_NETWORK.unlink()


def candidate_ready(state):
    if not c.active(UNIT):
        return None
    result = c.command('wpa_cli', '-p', str(private() / 'ctrl'), '-i', 'wlan0', 'status', check=False)
    values = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
    phase = values.get('wpa_state')
    state['last_wpa_state'] = phase if phase in (
        'DISCONNECTED', 'INTERFACE_DISABLED', 'INACTIVE', 'SCANNING', 'AUTHENTICATING',
        'ASSOCIATING', 'ASSOCIATED', '4WAY_HANDSHAKE', 'GROUP_HANDSHAKE', 'COMPLETED') else 'UNAVAILABLE'
    if (result.returncode or values.get('wpa_state') != 'COMPLETED'
            or values.get('id_str') != 'carecall-' + state['test_id']):
        return None
    addresses = json.loads(c.command('ip', '-j', '-4', 'address', 'show', 'dev', 'wlan0').stdout)
    networks = [ipaddress.ip_network(f'{a["local"]}/{a["prefixlen"]}', strict=False)
                for link in addresses for a in link.get('addr_info', [])
                if a.get('family') == 'inet' and a.get('scope') == 'global']
    ap = ipaddress.ip_network(c.AP_IP + '/24', strict=False)
    if any(n.overlaps(ap) for n in networks):
        raise c.TrialError('ROUTER_SUBNET_CONFLICT')
    routes = json.loads(c.command('ip', '-j', '-4', 'route', 'show', 'default', 'dev', 'wlan0').stdout)
    for network in networks:
        if any(r.get('gateway') and r.get('dev') == 'wlan0'
               and ipaddress.ip_address(r['gateway']) in network for r in routes):
            return str(network)
    return None


def attempt(candidate, should_stop):
    with c.network_lock():
        state = c.read_state()
        state['phase'] = 'router_connecting'
        state['attempts'] += 1
        state['last_wpa_state'] = 'STARTING'
        c.save_state(state)
        for suffix in ('web', 'dhcp', 'hostapd'):
            c.command('systemctl', 'stop', c.PREFIX + suffix + '.service')
        c.command('systemctl', 'stop', c.STATION)
        stop_candidate()
        remove_ap_override()
        c.command('ip', 'link', 'set', 'wlan0', 'down')
        c.command('iw', 'dev', 'wlan0', 'set', 'type', 'managed')
        # Drop only wlan0 global IPv4 addresses so a stale lease cannot pass the test.
        addresses = json.loads(c.command('ip', '-j', '-4', 'address', 'show', 'dev', 'wlan0').stdout)
        for link in addresses:
            for a in link.get('addr_info', []):
                if a.get('family') == 'inet' and a.get('scope') == 'global':
                    c.command('ip', '-4', 'address', 'del', f'{a["local"]}/{a["prefixlen"]}', 'dev', 'wlan0', check=False)
        c.atomic(private() / 'candidate.conf', supplicant_config(candidate, state['test_id']))
        c.command('ip', 'link', 'set', 'wlan0', 'up')
        c.command('networkctl', 'reload')
        c.command('networkctl', 'reconfigure', 'wlan0')
        c.command('systemctl', 'reset-failed', UNIT, check=False)
        c.command('systemctl', 'start', UNIT)
        end = min(c.now() + CONNECT_SECONDS, state['deadline'])
        result = 'CONNECT_TIMEOUT'
        try:
            while c.now() < end and not should_stop():
                subnet = candidate_ready(state)
                if subnet:
                    saved = dict(candidate, version=c.VERSION, test_id=state['test_id'],
                                 tested_subnet=subnet, purpose='candidate-only-not-boot-config')
                    c.atomic(c.ETC / 'tested-router-candidate.json', json.dumps(saved, ensure_ascii=True) + '\n')
                    state.update(router_connected=True, candidate_saved=True)
                    result = 'CONNECTED'
                    break
                if not c.active(UNIT):
                    result = 'CANDIDATE_SERVICE_STOPPED'
                    break
                time.sleep(1)
        except c.TrialError as exc:
            if str(exc) != 'ROUTER_SUBNET_CONFLICT':
                raise
            result = str(exc)
        finally:
            stop_candidate()
            (private() / 'candidate.conf').unlink(missing_ok=True)
        state['last_attempt'] = result
        c.save_state(state)
        return result


def run(setup_ap, should_stop, phone_confirmed):
    publish(c.read_state(), 'READY')
    setup_ap()
    while not should_stop():
        state = c.read_state()
        if c.deadline_due(state):
            return
        if phone_confirmed(state):
            state['confirmed'] = True
            c.save_state(state)
            time.sleep(2)
            return
        try:
            candidate = take_request(state)
        except c.TrialError:
            publish(state, 'INVALID_REQUEST')
            time.sleep(1)
            continue
        if candidate is None:
            time.sleep(0.5)
            continue
        if state['router_connected']:
            continue
        publish(state, 'CONNECTING')
        time.sleep(2)  # Let the phone receive the instructions before the AP disappears.
        result = attempt(candidate, should_stop)
        if should_stop() or c.deadline_due(c.read_state()):
            return
        publish(c.read_state(), result)
        setup_ap()  # Both failure and success return to the same setup AP.


def finish_restore(state):
    stop_candidate()
    (private() / 'candidate.conf').unlink(missing_ok=True)
    (c.RUN / 'public' / 'request.json').unlink(missing_ok=True)
    for path in (c.RUN / 'public').glob('.request-*'):
        if path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)
    state['phase'] = 'restoring'
    try:
        state['sources_unchanged'] = source_hashes() == state['source_hashes']
    except Exception:
        state['sources_unchanged'] = False
    if not state['sources_unchanged']:
        state['failure'] = 'SOURCE_FILES_CHANGED_DURING_TRIAL'
    c.save_state(state)
