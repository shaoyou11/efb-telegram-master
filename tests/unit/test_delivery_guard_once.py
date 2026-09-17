import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

path = Path(__file__).parents[2] / 'efb_telegram_master/delivery_telemetry.py'
spec = importlib.util.spec_from_file_location('guard_under_test', path)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

class GuardTests(unittest.TestCase):
    def make(self, directory):
        channel = SimpleNamespace(config={'admins': [1, 1]}, bot_manager=Mock())
        telemetry = SimpleNamespace(state={'pending': {'uid': 'one', 'at': 100}})
        guard = m.DeliveryGuard(telemetry, channel, Path(directory)/'recovery.json')
        guard._logged_in = Mock(return_value=True)
        return guard, channel

    def test_timeout_is_one_attempt_and_state_precedes_send(self):
        with TemporaryDirectory() as d:
            guard, channel = self.make(d)
            def timeout(*args, **kwargs):
                state = json.loads(guard.state_path.read_text())
                self.assertEqual(state['last_restart_uid'], 'one')
                self.assertEqual(state['last_alert_uid'], 'one')
                self.assertEqual(kwargs['read_timeout'], 10)
                raise TimeoutError('response lost after server accepted notice')
            channel.bot_manager.updater.bot.send_message.side_effect = timeout
            self.assertEqual(guard.check_once(now=1000), 'restart')
            self.assertEqual(guard.check_once(now=1100), 'alert')
            restored = m.DeliveryGuard(guard.telemetry, channel, guard.state_path)
            restored._logged_in = Mock(return_value=True)
            self.assertEqual(restored.check_once(now=5000), 'alert')
            channel.bot_manager.updater.bot.send_message.assert_called_once()
            channel.bot_manager.send_message.assert_not_called()

    def test_failed_persistence_sends_nothing_and_does_not_restart(self):
        with TemporaryDirectory() as d:
            guard, channel = self.make(d)
            guard._save_recovery_state = Mock(side_effect=OSError())
            self.assertEqual(guard.check_once(now=1000), 'none')
            channel.bot_manager.updater.bot.send_message.assert_not_called()

    def test_logged_out_notice_does_not_repeat_when_login_returns(self):
        with TemporaryDirectory() as d:
            guard, channel = self.make(d)
            guard._logged_in.return_value = False
            self.assertEqual(guard.check_once(now=1000), 'alert')
            guard._logged_in.return_value = True
            self.assertEqual(guard.check_once(now=1100), 'restart')
            channel.bot_manager.updater.bot.send_message.assert_called_once()

    def test_new_message_after_cooldown_gets_one_new_attempt(self):
        with TemporaryDirectory() as d:
            guard, channel = self.make(d)
            self.assertEqual(guard.check_once(now=1000), 'restart')
            guard.telemetry.state['pending'] = {'uid':'two', 'at':4000}
            self.assertEqual(guard.check_once(now=5000), 'restart')
            self.assertEqual(channel.bot_manager.updater.bot.send_message.call_count, 2)

    def test_existing_restart_record_does_not_repeat_notice(self):
        with TemporaryDirectory() as d:
            guard, channel = self.make(d)
            guard.state_path.write_text(json.dumps({'last_restart_uid':'one','last_restart_at':900}))
            self.assertEqual(guard.check_once(now=5000), 'alert')
            channel.bot_manager.updater.bot.send_message.assert_not_called()

if __name__ == '__main__':unittest.main()
