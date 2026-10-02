#!/usr/bin/env python3
"""Set a user-chosen, persistent CareCall setup-AP password through a hidden prompt.

Run on the Pi: sudo python3 -I -B carecall_set_ap_password.py
This changes settings.json only; it does not start/stop AP or modify UFW/Netplan.
"""
import getpass
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import stat
import sys
import warnings

COMMON = Path('/opt/carecall-wifi-aptrial/common.py')


def valid_password(value):
    # Matches the existing AP program's length policy, restricted to safe ASCII.
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9]{16,63}', value) is not None


def load_common():
    metadata = COMMON.stat()
    if COMMON.is_symlink() or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise RuntimeError('INSTALLED_CODE_PERMISSIONS_REQUIRE_REVIEW')
    spec = importlib.util.spec_from_file_location('carecall_installed_common', COMMON)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def require_idle(c):
    if c.AP_NETWORK.exists():
        raise RuntimeError('AP_TRIAL_NOT_FINISHED')
    for suffix in ('test', 'guard', 'hostapd', 'dhcp', 'web'):
        result = c.command('systemctl', 'show', '-p', 'ActiveState', '--value',
                           c.PREFIX + suffix + '.service')
        if result.stdout.strip() not in ('inactive', 'failed'):
            raise RuntimeError('AP_TRIAL_NOT_FINISHED')
    previous = c.read_state()
    if previous is not None and not previous.get('restored'):
        raise RuntimeError('ORIGINAL_WIFI_RESTORE_NOT_CONFIRMED')
    if not c.station_ready():
        raise RuntimeError('ORIGINAL_WIFI_NOT_READY')


def choose_password():
    if not sys.stdin.isatty():
        raise RuntimeError('INTERACTIVE_SSH_TERMINAL_REQUIRED')
    print('설정용 Wi-Fi에 사용할 비밀번호를 직접 입력하세요.')
    print('허용: 영문 대소문자와 숫자, 16~63자. 입력 내용은 표시되지 않습니다.')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        password = getpass.getpass('새 설정 Wi-Fi 비밀번호: ')
        if not valid_password(password):
            raise RuntimeError('PASSWORD_MUST_BE_16_TO_63_ASCII_LETTERS_OR_DIGITS')
        confirmation = getpass.getpass('같은 비밀번호 다시 입력: ')
    if not hmac.compare_digest(password.encode('utf-8'), confirmation.encode('utf-8')):
        raise RuntimeError('PASSWORD_CONFIRMATION_MISMATCH')
    return password


def update_settings(c, password):
    if not valid_password(password):
        raise RuntimeError('INVALID_NEW_PASSWORD')
    with c.network_lock():
        # Check again after the interactive prompt, before changing the setting.
        require_idle(c)
        target = c.ETC / 'settings.json'
        if target.is_symlink() or not target.is_file():
            raise RuntimeError('SETTINGS_FILE_REQUIRES_REVIEW')
        old_text = target.read_text()
        original = json.loads(old_text)
        if c.settings() != original:
            raise RuntimeError('SETTINGS_CHANGED_DURING_READ')
        if original.get('password') == password:
            return False
        updated = dict(original)
        updated['password'] = password
        # Root-only backup stays on the Pi and is replaced on each later change.
        c.atomic(c.ETC / 'settings.before-password-change.json', old_text, 0o600)
        try:
            c.atomic(target, json.dumps(updated, ensure_ascii=True, indent=2) + '\n', 0o600)
            metadata = target.stat()
            if (json.loads(target.read_text()) != updated or c.settings() != updated or
                    metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) != 0o600):
                raise RuntimeError('SAVED_SETTINGS_VERIFICATION_FAILED')
        except Exception:
            try:
                c.atomic(target, old_text, 0o600)
                print('PASSWORD_CHANGE_ROLLBACK=SUCCESS')
            except Exception:
