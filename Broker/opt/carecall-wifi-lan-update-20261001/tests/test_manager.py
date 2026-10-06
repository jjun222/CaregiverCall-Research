from contextlib import ExitStack
import unittest
from unittest.mock import patch

from support import manager as m, c


class ManagerIntegrationTests(unittest.TestCase):
    def test_online_requires_firewall_sync_after_wifi_identity_check(self):
        events = []
        with patch.object(m, 'saved_ready', side_effect=lambda _: events.append('identity') or True), \
                patch.object(m.firewall, 'sync_current', side_effect=lambda: events.append('firewall')), \
                patch.object(m, 'state_update'):
            self.assertTrue(m.wait_saved({'ssid': 'fixture'}))
        self.assertEqual(events, ['identity', 'firewall'])

    def test_firewall_failure_prevents_wifi_commit_readiness(self):
        with patch.object(m, 'saved_ready', return_value=True), \
                patch.object(m.firewall, 'sync_current', side_effect=c.TrialError('LAN_PRIVATE_IPV4_REQUIRED')), \
                patch.object(m, 'state_update') as update:
            self.assertFalse(m.wait_saved({'ssid': 'fixture'}))
            update.assert_called_with(lan_failure='LAN_PRIVATE_IPV4_REQUIRED')

    def test_unconnected_wifi_never_changes_rules(self):
        with patch.object(m, 'saved_ready', return_value=False), patch.object(c, 'now', side_effect=[0, 0, 100]), \
                patch.object(m, 'pause'), patch.object(m.firewall, 'sync_current') as sync:
            self.assertFalse(m.wait_saved({'ssid': 'fixture'}, seconds=1))
            sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
