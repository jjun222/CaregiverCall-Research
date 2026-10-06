import copy
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from support import c, fw, address, route, raw, FakeUFW


class SelectionTests(unittest.TestCase):
    def test_selects_connected_private_lan_from_actual_prefix(self):
        for ip, prefix, gateway, expected in (
            ('*가림*', 24, '*가림*', '*가림*'),
            ('*가림*', 24, '*가림*', '*가림*'),
            ('*가림*', 23, '*가림*', '*가림*'),
            ('*가림*', 28, '*가림*', '*가림*')):
            with self.subTest(ip=ip):
                self.assertEqual(fw.select_network(address(ip, prefix), route(gateway)), expected)

    def test_rejects_public_cgnat_link_local_reserved_overlap(self):
        for ip, prefix, gateway in (
            ('*가림*', 24, '*가림*'), ('*가림*', 24, '*가림*'),
            ('*가림*', 24, '*가림*'), ('*가림*', 24, '*가림*'),
            ('*가림*', 24, '*가림*'), ('*가림*', 16, '*가림*')):
            with self.subTest(ip=ip), self.assertRaises(c.TrialError):
                fw.select_network(address(ip, prefix), route(gateway))

    def test_rejects_missing_wrong_interface_and_invalid_gateway(self):
        for routes in ([], route(dev='eth0'), route('*가림*'), route('*가림*'),
                       route('*가림*'), route('*가림*')):
            with self.subTest(routes=routes), self.assertRaises(c.TrialError):
                fw.select_network(address(), routes)

    def test_ignores_eth0_default_in_unfiltered_route_json(self):
        self.assertEqual(fw.select_network(address(), route('*가림*', 'eth0') + route()), fw.ORIGINAL)

    def test_rejects_ambiguous_multiple_subnets(self):
        addresses = address()
        addresses[0]['addr_info'] += address('*가림*')[0]['addr_info']
        with self.assertRaisesRegex(c.TrialError, 'AMBIGUOUS'):
            fw.select_network(addresses, route() + route('10.10.10.1'))

    def test_invalid_state_subnets(self):
        for net in ('*가림*', '::/0', '0.0.0.0/0', '*가림*', '*가림*'):
            with self.subTest(net=net), self.assertRaises(c.TrialError):
                fw.validate_network(net)


class FirewallTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.ufw = FakeUFW()
        self.ufw.install(self.stack, self.root)

    def test_current_lan_bootstrap_does_not_change_any_rule(self):
        before = copy.deepcopy(self.ufw.records)
        fw.reconcile(fw.ORIGINAL)
        self.assertEqual(before, self.ufw.records)
        self.assertEqual(self.ufw.operations, [])
        self.assertEqual(fw.state_path().stat().st_mode & 0o777, 0o600)
        self.assertEqual(fw.load_state()['phase'], 'committed')

    def test_adds_both_new_before_deleting_old_and_preserves_others(self):
        target = '192.168.50.0/24'
        before, _ = fw.split_lan(self.ufw.records, [fw.ORIGINAL])
        fw.reconcile(target)
        self.assertEqual([op[0] for op in self.ufw.operations], ['add', 'add', 'delete', 'delete'])
        after, found = fw.split_lan(self.ufw.records, [target])
        self.assertEqual(before, after)
        self.assertEqual(found, {(target, '22'), (target, '1883')})
        self.assertEqual(fw.load_state()['networks'], [target])
        fw.check()

    def test_each_interrupted_mutation_is_resumable_from_durable_state(self):
        for failure in range(1, 5):
            with self.subTest(failure=failure):
                fw.state_path().unlink(missing_ok=True)
                self.ufw.records = fw.base.expected(4)
                self.ufw.operations.clear()
                self.ufw.crash_after = failure
                with self.assertRaises(SystemExit):
                    fw.reconcile('192.168.50.0/24')
                self.assertEqual(fw.load_state()['phase'], 'pending')
                fw.check()
                self.ufw.crash_after = None
                fw.reconcile('192.168.50.0/24')
                self.assertEqual(fw.load_state()['phase'], 'committed')
                self.assertEqual(fw.inspect()[1], {('192.168.50.0/24', p) for p in fw.PORTS})

    def test_interrupted_new_lan_can_return_to_previous_lan(self):
        self.ufw.crash_after = 1
        with self.assertRaises(SystemExit):
            fw.reconcile('192.168.50.0/24')
        self.ufw.crash_after = None
        fw.reconcile(fw.ORIGINAL)
        self.assertEqual(fw.inspect()[1], {(fw.ORIGINAL, p) for p in fw.PORTS})

    def test_power_loss_before_commit_and_changed_destination_lan(self):
        self.ufw.crash_after = 3
        with self.assertRaises(SystemExit):
            fw.reconcile('192.168.50.0/24')
        self.ufw.crash_after = None
        fw.reconcile('10.40.0.0/24')
        self.assertEqual(fw.inspect()[1], {('10.40.0.0/24', p) for p in fw.PORTS})

    def test_failed_durable_flush_leaves_pending_recoverable(self):
        with patch.object(fw, 'flush_rules', side_effect=OSError('simulated disk error')):
            with self.assertRaises(OSError):
                fw.reconcile('10.40.0.0/24')
        self.assertEqual(fw.load_state()['phase'], 'pending')
        fw.reconcile('10.40.0.0/24')
        self.assertEqual(fw.load_state()['phase'], 'committed')

    def test_unknown_rule_or_altered_ap_rule_is_rejected_before_mutation(self):
        for records in (fw.base.expected(4) + ['R ACCEPT 6 -- * * 0.0.0.0/0 0.0.0.0/0 tcp dpt:9999'],
                        [r for r in fw.base.expected(4) if 'dpt:8080' not in r]):
            with self.subTest(records=len(records)):
                self.ufw.records = records
                with self.assertRaises(c.TrialError):
                    fw.reconcile('192.168.50.0/24')
                self.assertFalse(self.ufw.operations)
                self.assertFalse(fw.state_path().exists())

    def test_matching_rule_in_another_chain_is_not_ignored(self):
        self.ufw.records.insert(self.ufw.records.index('C ufw-user-output -') + 1,
                                fw.lan_record(fw.ORIGINAL, '22'))
        with self.assertRaises(c.TrialError):
            fw.check()

    def test_duplicates_and_missing_committed_rules_rejected(self):
        self.ufw.records.insert(self.ufw.records.index('C ufw-user-input -') + 1,
                                fw.lan_record(fw.ORIGINAL, '22'))
        with self.assertRaisesRegex(c.TrialError, 'DUPLICATE'):
            fw.check()
        self.ufw.records = [r for r in fw.base.expected(4) if r != fw.lan_record(fw.ORIGINAL, '22')]
        with self.assertRaisesRegex(c.TrialError, 'MISSING'):
            fw.check()

    def test_rejects_publicly_readable_or_malformed_state(self):
        fw.reconcile(fw.ORIGINAL)
        os.chmod(fw.state_path(), 0o644)
        with self.assertRaises(c.TrialError):
            fw.load_state()
        os.chmod(fw.state_path(), 0o600)
        fw.state_path().write_text('{broken')
        with self.assertRaisesRegex(c.TrialError, 'STATE_INVALID'):
            fw.load_state()

    def test_cache_never_delays_a_changed_subnet(self):
        with patch.object(fw, 'current_network', return_value=fw.ORIGINAL):
            fw.sync_current()
            fw.sync_current()
        self.assertFalse(self.ufw.operations)
        with patch.object(fw, 'current_network', return_value='10.40.0.0/24'):
            fw.sync_current()
        self.assertEqual(fw.load_state()['target'], '10.40.0.0/24')


if __name__ == '__main__':
    unittest.main()
