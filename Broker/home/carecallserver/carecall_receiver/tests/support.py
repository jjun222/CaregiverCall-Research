"""Network-free fixtures. UFW/systemd are simulated; files use real temp dirs."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
import firewall as fw
import manager as manager

spec = importlib.util.spec_from_file_location('lan_installer', PACKAGE / 'apply_patch.py')
up = importlib.util.module_from_spec(spec)
spec.loader.exec_module(up)


def address(ip='*가린*', prefix=24):
    return [{'ifname': 'wlan0', 'addr_info': [
        {'family': 'inet', 'scope': 'global', 'local': ip, 'prefixlen': prefix}]}]


def route(gateway='*가린*', dev='wlan0'):
    return [{'dst': 'default', 'dev': dev, 'gateway': gateway, 'protocol': 'dhcp', 'metric': 600}]


def raw(records):
    lines = []
    for record in records:
        if record == 'F 4':
            lines.append('IPV4 (raw):')
        elif record == 'F 6':
            lines.append('IPV6:')
        elif record.startswith('C '):
            _, name, policy = record.split()
            detail = '1 references' if policy == '-' else 'policy ' + policy + ' 0 packets, 0 bytes'
            lines.append('Chain ' + name + ' (' + detail + ')')
        else:
            lines.append('0 0 ' + record[2:])
    return '\n'.join(lines)


class FakeUFW:
    def __init__(self):
        self.records = fw.base.expected(4)
        self.operations = []
        self.crash_after = None
        self.flushes = 0

    def flush(self):
        self.flushes += 1

    def call(self, *args):
        if args == ('status', 'verbose'):
            return 'Status: active\n'
        if args == ('show', 'raw'):
            return raw(self.records)
        deleting = args[:2] == ('--force', 'delete')
        rule = args[2:] if deleting else args
        assert rule[:7] == ('allow', 'in', 'on', 'wlan0', 'proto', 'tcp', 'from'), args
        assert rule[8:11] == ('to', 'any', 'port'), args
        net, port = rule[7], rule[11]
        assert deleting and len(rule) == 12 or not deleting and len(rule) == 14 and rule[12] == 'comment', args
        text = fw.lan_record(net, port)
        if deleting:
            self.records.remove(text)
        else:
            assert text not in self.records
            index = self.records.index('C ufw-user-input -') + 1
            while index < len(self.records) and self.records[index].startswith('R '):
                index += 1
            self.records.insert(index, text)
        self.operations.append(('delete' if deleting else 'add', net, port))
        if self.crash_after == len(self.operations):
            raise SystemExit('simulated interruption')
        return 'Rule deleted' if deleting else 'Rule added'

    def install(self, stack, root):
        stack.enter_context(patch.multiple(c, ETC=root / 'etc', RUN=root / 'run', STATE=root / 'run/state.json'))
        stack.enter_context(patch.object(fw.base, 'run_ufw', side_effect=self.call))
        stack.enter_context(patch.object(fw.base, 'check_extra_nft_tables'))
        stack.enter_context(patch.object(fw, 'flush_rules', side_effect=self.flush))
        stack.enter_context(patch.multiple(fw, _cached_network=None, _cached_until=0))

