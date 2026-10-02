"""Bounded AP trial; normal Netplan files and service enablement are untouched."""
import argparse
import hmac
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import sys
import time

import common as c

stopping = False


def request_stop(*_):
    global stopping
    stopping = True


def write_phase(phase):
    state = c.read_state()
    state['phase'] = phase
    c.save_state(state)


def setup_ap():
    with c.network_lock():
        state = c.read_state()
        if c.deadline_due(state) or stopping:
            raise c.TrialError('TRIAL_CANCELLED')
        network, hostapd, dnsmasq = c.configs(c.settings())
        c.atomic(c.RUN / 'hostapd.conf', hostapd)
        c.atomic(c.RUN / 'dnsmasq.conf', dnsmasq)
        if c.AP_NETWORK.exists():
            raise c.TrialError('AP_RUNTIME_FILE_ALREADY_EXISTS')
        write_phase('switching_to_ap')
        c.command('systemctl', 'stop', c.STATION)
        c.atomic(c.AP_NETWORK, network, 0o644)
        c.command('networkctl', 'reload')
        c.command('networkctl', 'reconfigure', 'wlan0')
        end = c.now() + 20
        while c.AP_IP not in c.ipv4_addresses():
            if c.now() > end or stopping:
                raise c.TrialError('AP_ADDRESS_NOT_READY')
            time.sleep(0.5)
        for suffix in ('hostapd', 'dhcp', 'web'):
            if stopping:
                raise c.TrialError('TRIAL_CANCELLED')
            c.command('systemctl', 'start', c.PREFIX + suffix + '.service')
        time.sleep(2)
        if not all(c.active(c.PREFIX + suffix + '.service') for suffix in ('hostapd', 'dhcp', 'web')):
            raise c.TrialError('AP_SERVICE_NOT_RUNNING')
        write_phase('ap_ready')


def phone_confirmed(state):
    path = c.RUN / 'public' / 'confirmed'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (FileNotFoundError, OSError):
        return False
    with os.fdopen(fd, 'rb') as f:
        metadata = os.fstat(f.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64:
            return False
        if metadata.st_uid != pwd.getpwnam('carecall-wifi-web').pw_uid:
            return False
        return hmac.compare_digest(f.read(64), state['token'].encode())


def restore_original():
    with c.network_lock():
        state = c.read_state()
        if state is None or state.get('restored'):
            return True
        write_phase('restoring')
        for suffix in ('web', 'dhcp', 'hostapd'):
            c.command('systemctl', 'stop', c.PREFIX + suffix + '.service', check=False)
        if c.AP_NETWORK.exists():
            if not c.AP_NETWORK.read_text().startswith(c.MARKER):
                raise c.TrialError('UNEXPECTED_AP_RUNTIME_FILE')
            c.AP_NETWORK.unlink()
        # Only the trial address is removed. No original .network or YAML is edited.
        c.command('ip', '-4', 'address', 'del', c.AP_IP + '/24', 'dev', 'wlan0', check=False)
        c.command('ip', 'link', 'set', 'wlan0', 'down')
        c.command('iw', 'dev', 'wlan0', 'set', 'type', 'managed')
        c.command('ip', 'link', 'set', 'wlan0', 'up')
        c.command('networkctl', 'reload')
        c.command('systemctl', 'start', c.STATION)
        c.command('networkctl', 'reconfigure', 'wlan0')
        end = c.now() + 45
        restored = False
        while c.now() < end:
            if c.station_ready():
                restored = True
                break
            time.sleep(1)
        state = c.read_state()
        state['restored'] = restored
        state['phase'] = 'restored' if restored else 'restore_pending'
        c.save_state(state)
        c.atomic(c.RESULT, json.dumps(c.final_report(state)) + '\n')
        return restored


def run_trial():
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        setup_ap()
        while not stopping:
            state = c.read_state()
            if phone_confirmed(state):
                state['confirmed'] = True
                c.save_state(state)
                time.sleep(2)  # Allow the HTTP response to reach the phone.
                break
            if c.deadline_due(state):
                break
            time.sleep(0.5)
    except Exception as exc:
        state = c.read_state()
        state['failure'] = str(exc) if isinstance(exc, c.TrialError) else type(exc).__name__
        c.save_state(state)
        print('AP_TRIAL_SETUP_FAILED=' + state['failure'], flush=True)
    finally:
        try:
            if restore_original():
                print('ORIGINAL_WIFI_RESTORED=YES', flush=True)
            else:
                print('ORIGINAL_WIFI_RESTORED=PENDING', flush=True)
        except Exception as exc:
            print('RESTORE_DEFERRED=' + type(exc).__name__, flush=True)


def guard():
    """Independent process: survives SSH disconnect and test-process failure."""
    while True:
        state = c.read_state()
        if state is None or state.get('restored'):
            return
        failed_process = c.now() - state['started'] > 10 and not c.active(c.PREFIX + 'test.service')
        if c.deadline_due(state) or failed_process:
            # Ensure a stuck test process releases its lock before recovery.
            try:
                c.command('systemctl', 'stop', c.PREFIX + 'test.service', check=False, timeout=15)
                if restore_original():
                    return
            except Exception as exc:
                print('GUARD_RETRY=' + type(exc).__name__, flush=True)
            time.sleep(5)
        else:
            time.sleep(1)


def check_firewall():
    # A conflicting firewall requires review before a no-console network trial.
    ufw = Path('/usr/sbin/ufw')
    if ufw.exists():
        import subprocess
        r = subprocess.run([str(ufw), 'status'], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=10,
                           env=dict(os.environ, LC_ALL='C'))
        if r.returncode != 0 or 'Status: active' in r.stdout:
            raise c.TrialError('ACTIVE_OR_UNKNOWN_UFW_REQUIRES_REVIEW')
    # Do not silently assume custom nft/iptables INPUT rules allow the AP page.
    import subprocess
    for executable, args in (('/usr/sbin/nft', ['list', 'ruleset']),
                             ('/usr/sbin/iptables', ['-S', 'INPUT'])):
        if not Path(executable).exists():
            continue
        p = subprocess.run([executable, *args], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, text=True, timeout=10)
        if p.returncode:
            raise c.TrialError('FIREWALL_INSPECTION_FAILED')
        if executable.endswith('/nft') and 'hook input' in p.stdout:
            raise c.TrialError('CUSTOM_INPUT_FIREWALL_REQUIRES_REVIEW')
        if executable.endswith('/iptables') and any(
                line.strip() not in ('', '-P INPUT ACCEPT') for line in p.stdout.splitlines()):
            raise c.TrialError('CUSTOM_INPUT_FIREWALL_REQUIRES_REVIEW')


def start_trial():
    check_firewall()
    if not c.station_ready():
        raise c.TrialError('ORIGINAL_WIFI_NOT_READY')
    if not all(c.active(unit + '.service') for unit in c.CARECALL):
        raise c.TrialError('CARECALL_SERVICE_NOT_ACTIVE')
    if c.AP_NETWORK.exists():
        raise c.TrialError('AP_RUNTIME_FILE_ALREADY_EXISTS')
    if c.active(c.PREFIX + 'guard.service') or c.active(c.PREFIX + 'test.service'):
        raise c.TrialError('TRIAL_ALREADY_RUNNING')
    old = c.read_state()
    if old and not old.get('restored'):
        raise c.TrialError('PREVIOUS_TRIAL_REQUIRES_RESTORE')
    c.RUN.mkdir(parents=True, exist_ok=True, mode=0o755)
    public = c.RUN / 'public'
    public.mkdir(exist_ok=True, mode=0o770)
    account = pwd.getpwnam('carecall-wifi-web')
    os.chown(public, 0, account.pw_gid)
    os.chmod(public, 0o770)
    for name in ('confirmed', 'session.json'):
        (public / name).unlink(missing_ok=True)
    state = c.new_state()
    c.save_state(state)
    c.atomic(public / 'session.json', json.dumps(c.public_state(state['test_id'], state['token'])), 0o640)
    os.chown(public / 'session.json', 0, account.pw_gid)
    c.command('systemctl', 'reset-failed', c.PREFIX + 'test.service', c.PREFIX + 'guard.service', check=False)
    c.command('systemctl', 'start', c.PREFIX + 'guard.service')
    if not c.active(c.PREFIX + 'guard.service'):
        raise c.TrialError('RESTORE_GUARD_NOT_RUNNING')
    print('AP_TEST_SCHEDULED=YES', flush=True)
    print('AUTOMATIC_RETURN_SECONDS=180', flush=True)
    print('SSH_MAY_DISCONNECT=YES', flush=True)
    c.command('systemctl', 'start', '--no-block', c.PREFIX + 'test.service')


def status():
    print('VERSION=' + c.VERSION)
    try:
        check_firewall()
        print('FIREWALL_PRECHECK=PASS')
    except c.TrialError as exc:
        print('FIREWALL_PRECHECK=' + str(exc))
    for unit in c.CARECALL:
        print('SERVICE ' + unit + '=' + ('active' if c.active(unit + '.service') else 'not_active'))
    for suffix in ('test', 'guard', 'hostapd', 'dhcp', 'web'):
        print('TRIAL_SERVICE ' + suffix + '=' + ('active' if c.active(c.PREFIX + suffix + '.service') else 'inactive'))
    state = c.read_state()
    if state:
        print('TRIAL_PHASE=' + state['phase'])
    if c.RESULT.exists():
        report = json.loads(c.RESULT.read_text())
        for key in ('result', 'phone_confirmed', 'restored', 'failure'):
            print('LAST_' + key.upper() + '=' + str(report[key]))


def main():
    parser = argparse.ArgumentParser(description='CareCall temporary AP connectivity trial')
    parser.add_argument('action', choices=('status', 'show-setup', 'test-ap', 'restore', 'run-test', 'guard'))
    action = parser.parse_args().action
    if os.geteuid() != 0:
        print('ERROR=RUN_WITH_SUDO', file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        if action == 'status':
            status()
        elif action == 'show-setup':
            config = c.settings()
            print('PRIVATE_SETUP_CARD_DO_NOT_SHARE')
            print('Wi-Fi: ' + config['ssid'])
            print('Password: ' + config['password'])
            print(f'Browser: http://{c.AP_IP}:{c.AP_PORT}/')
        elif action == 'test-ap':
            start_trial()
        elif action == 'run-test':
            run_trial()
        elif action == 'guard':
            guard()
        else:
            c.command('systemctl', 'stop', c.PREFIX + 'test.service', check=False, timeout=15)
            if not restore_original():
                raise c.TrialError('RESTORE_NOT_CONFIRMED')
        return 0
    except Exception as exc:
        code = str(exc) if isinstance(exc, c.TrialError) else type(exc).__name__
        print('ERROR=' + code, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
