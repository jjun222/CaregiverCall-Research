import copy
import importlib.util
from pathlib import Path
import re
import sys
import unittest
from unittest import mock

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / 'app'))
import common as c
import firewall as fw
spec = importlib.util.spec_from_file_location('patch_installer', PACKAGE / 'apply_patch.py')
patch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(patch)


class FirewallTests(unittest.TestCase):
    def setUp(self):
        self.raw = (PACKAGE / 'tests' / 'reviewed_ufw_raw.txt').read_text()

    def test_actual_user_log_is_recognized_without_changes(self):
        self.assertEqual(fw.reviewed_count(fw.normalize(self.raw)), 0)

    def test_packet_counters_and_reference_counts_do_not_change_review(self):
        changed = re.sub(r'(?m)^(\s*)\d+\s+\d+(\s+\S+\s+\S+\s+--)', r'\g<1>999999 12345678\2', self.raw)
        changed = re.sub(r'\d+ packets, \d+ bytes', '8765 packets, 9999 bytes', changed)
        changed = re.sub(r'\d+ references', '789 references', changed)
        self.assertEqual(fw.normalize(changed), fw.normalize(self.raw))

    def test_raw_report_with_four_appended_rules_matches(self):
        raw_lines = '\n'.join(' 0 0 ' + fw.rule_record(rule)[2:] for rule in fw.RULES)
        anchor = '\nChain ufw-user-limit '
        raw = self.raw.replace(anchor, '\n' + raw_lines + '\n' + anchor, 1)
        self.assertEqual(fw.reviewed_count(fw.normalize(raw)), 4)

    def test_unknown_or_broader_firewall_rules_are_rejected(self):
        for change in (
            self.raw.replace('192.168.0.0/24', '0.0.0.0/0'),
            self.raw.replace('Chain INPUT (policy DROP', 'Chain INPUT (policy ACCEPT', 1),
            self.raw.replace('tcp dpt:1883', 'tcp dpt:8883'),
            self.raw.replace('udp dpt:5353', 'udp dpt:5354'),
        ):
            with self.assertRaises(c.TrialError):
                fw.reviewed_count(fw.normalize(change))

    def test_missing_ipv6_or_malformed_report_is_rejected(self):
        for value in (self.raw.split('IPV6:')[0], self.raw + '\nUNKNOWN ERROR\n'):
            with self.assertRaises(c.TrialError):
                fw.normalize(value)

    def test_dhcp_initial_address_and_all_rules_are_scoped(self):
        self.assertEqual(fw.RULES[1][1], '0.0.0.0/32')
        for rule in fw.RULES:
            args = fw.rule_args(rule)
            self.assertEqual(args[:4], ['allow', 'in', 'on', 'wlan0'])
            self.assertIn(rule[3], ('192.168.77.1', '255.255.255.255'))
            self.assertNotIn('0.0.0.0/0', args)
            self.assertNotIn('any', args)
        self.assertEqual([r[2] for r in fw.RULES[1:]], ['68'] * 3)

    def test_inactive_ufw_is_not_approved(self):
        with mock.patch.object(fw, 'run_ufw', return_value='Status: inactive\n'):
            with self.assertRaisesRegex(c.TrialError, 'MUST_REMAIN_ACTIVE'):
                fw.inspect()

    def test_partial_rules_do_not_pass_trial_precheck(self):
        with mock.patch.object(fw, 'inspect', return_value=(3, '', '')):
            with self.assertRaisesRegex(c.TrialError, 'INCOMPLETE'):
                fw.check()

    def test_all_reviewed_rules_pass_trial_precheck(self):
        with mock.patch.object(fw, 'inspect', return_value=(4, '', '')):
            fw.check()

    def test_rule_failure_rolls_back_only_attempted_additions_in_reverse(self):
        attempted = []
        with (mock.patch.object(fw, 'run_ufw', side_effect=['', c.TrialError('FAILED')]),
              mock.patch.object(fw, 'inspect', return_value=(1, '', ''))):
            with self.assertRaises(c.TrialError):
                patch.apply_rules(0, attempted)
        self.assertEqual(attempted, [0, 1])
        with (mock.patch.object(fw, 'run_ufw', return_value='') as run,
              mock.patch.object(fw, 'inspect', return_value=(0, '', ''))):
            self.assertTrue(patch.rollback_rules(attempted, 0))
        self.assertEqual(run.call_args_list, [
            mock.call('--force', 'delete', *fw.rule_args(fw.RULES[1])),
            mock.call('--force', 'delete', *fw.rule_args(fw.RULES[0]))])

    def test_additional_native_nft_table_is_rejected(self):
        result = mock.Mock(returncode=0, stdout='{"nftables":[{"table":{"family":"inet","name":"custom"}}]}')
        with (mock.patch.object(fw.Path, 'exists', return_value=True),
              mock.patch.object(fw.subprocess, 'run', return_value=result)):
            with self.assertRaisesRegex(c.TrialError, 'ADDITIONAL_NFT_TABLE'):
                fw.check_extra_nft_tables()


if __name__ == '__main__':
    unittest.main()
