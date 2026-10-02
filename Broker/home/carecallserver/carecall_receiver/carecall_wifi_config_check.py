#!/usr/bin/env python3
"""Read-only inventory for CareCall's next Wi-Fi provisioning implementation.

Run on the Pi: sudo python3 -I -B /home/carecallserver/carecall_wifi_config_check.py
Only selected structural fields are printed. Raw configuration, SSIDs, keys,
addresses, cloud user-data and subprocess error messages are never printed.
No apply/generate/set, package installation or service mutation is performed.
COMPLETE means inventory collection completed, not that provisioning is ready.
"""
import configparser
import fnmatch
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

VERSION = '20260928-config-check-1'
LIMIT = 2 * 1024 * 1024
PROBLEMS = []
LABELS = {}
yaml = None


def emit(name, value):
    print(name + '=' + json.dumps(value, ensure_ascii=True, sort_keys=True), flush=True)


def problem(where, code):
    PROBLEMS.append({'where': where, 'code': code})


def mapping(value):
    return value if isinstance(value, dict) else {}


def count(value):
    return len(value) if isinstance(value, (dict, list, tuple)) else 0


def enum(value, allowed):
    if value is None:
        return 'UNSET'
    return value if isinstance(value, str) and value in allowed else 'OTHER'


def boolean(value):
    if value is None:
        return 'UNSET'
    return value if type(value) is bool else 'OTHER'


def label(value, kind):
    # Labels preserve equality across files without disclosing names or hashes.
    key = (kind, type(value).__name__, str(value))
    if key not in LABELS:
        LABELS[key] = kind + '_' + str(1 + sum(k[0] == kind for k in LABELS))
    return LABELS[key]


def net_summary(document):
    root = mapping(document)
    n = mapping(root.get('network', root))
    result = {'renderer': enum(n.get('renderer'), ('networkd', 'NetworkManager')),
              'version': n.get('version') if type(n.get('version')) is int else 'UNSET',
              'devices': []}
    known_device_keys = {
        'renderer', 'dhcp4', 'dhcp6', 'optional', 'accept-ra', 'link-local',
        'match', 'set-name', 'access-points', 'addresses', 'routes',
        'gateway4', 'gateway6', 'nameservers', 'dhcp4-overrides', 'dhcp6-overrides',
        'auth', 'regulatory-domain', 'optional-addresses', 'wakeonwlan',
    }
    for kind in ('ethernets', 'wifis', 'bridges', 'bonds', 'vlans', 'tunnels'):
        for device_id, value in mapping(n.get(kind)).items():
            d = mapping(value)
            match = mapping(d.get('match'))
            name = match.get('name')
            item = {
                'kind': kind,
                'id': device_id if device_id in ('wlan0', 'eth0', 'lo') else label(device_id, 'DEVICE'),
                'renderer': enum(d.get('renderer'), ('networkd', 'NetworkManager')),
                'dhcp4': boolean(d.get('dhcp4')), 'dhcp6': boolean(d.get('dhcp6')),
                'optional': boolean(d.get('optional')),
                'match_present': 'match' in d,
                'match_name_matches_wlan0': fnmatch.fnmatchcase('wlan0', name) if isinstance(name, str) else None,
                'match_mac_present': 'macaddress' in match,
                'set_name_is_wlan0': d.get('set-name') == 'wlan0',
                'address_count': count(d.get('addresses')), 'route_count': count(d.get('routes')),
                'gateway_present': any(k in d for k in ('gateway4', 'gateway6')),
                'nameservers_present': 'nameservers' in d,
                'dhcp_overrides_present': any(k in d for k in ('dhcp4-overrides', 'dhcp6-overrides')),
                'other_setting_count': sum(k not in known_device_keys for k in d),
            }
            if kind == 'wifis':
