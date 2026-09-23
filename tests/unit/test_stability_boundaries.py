import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from telegram.error import TimedOut, RetryAfter
from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.delivery_outcome import SendUnconfirmed
from efb_telegram_master.slave_message import SlaveMessageProcessor
from efb_telegram_master.failed_delivery import FailedDeliveryStore
from efb_telegram_master.delivery_telemetry import DeliveryTelemetry, DeliveryGuard, DigestGuard
from ehforwarderbot import MsgType

class StabilityTests(unittest.TestCase):
    def test_text_accepted_response_lost_no_replay_at_either_layer(self):
        accepted = []
        def send_message(**kwargs):
            accepted.append(kwargs)
            raise TimedOut()
        p = object.__new__(SlaveMessageProcessor)
        p.logger = Mock()
        p.dispatch_message = TelegramBotManager.Decorators.retry_on_timeout(send_message)
        with patch.object(TelegramBotManager.Decorators, 'enable_retry', True):
            with self.assertRaises(SendUnconfirmed):
                p.dispatch_with_retry(msg=SimpleNamespace(uid='text', path=None))
        self.assertEqual(len(accepted), 1)

    def test_non_send_timeout_and_rate_rejection_are_bounded(self):
        for error in (TimedOut(), RetryAfter(1)):
            calls = Mock(side_effect=error)
            def get_chat(): return calls()
            with patch.object(TelegramBotManager.Decorators, 'enable_retry', True), patch('efb_telegram_master.bot_manager.time.sleep'):
                with self.assertRaises(type(error)):
                    TelegramBotManager.Decorators.retry_on_timeout(get_chat)()
            self.assertEqual(calls.call_count, 3)

    def test_concurrent_finish_does_not_hide_stuck_message(self):
        with TemporaryDirectory() as d:
            t = DeliveryTelemetry(Path(d)/'state.json')
            with patch('efb_telegram_master.delivery_telemetry.time.time', return_value=100):
                t.inbound('stuck', 'video')
            with patch('efb_telegram_master.delivery_telemetry.time.time', return_value=200):
                t.inbound('fast', 'text'); t.delivered('fast')
            self.assertEqual(t.snapshot()['pending']['uid'], 'stuck')
            self.assertEqual(len(t.snapshot()['inflight']), 1)
            t.failed('stuck', 'timeout')
            self.assertIsNone(t.snapshot()['pending'])

    def test_guard_does_not_probe_or_restart_during_scan_or_idle(self):
        with TemporaryDirectory() as d:
            ch = SimpleNamespace(config={'admins':[1]},bot_manager=Mock())
            t = DeliveryTelemetry(Path(d)/'state.json')
            g = DeliveryGuard(t, ch, Path(d)/'guard.json')
            with patch.object(g, '_logged_in', side_effect=AssertionError('must not probe')):
                self.assertEqual(g.check_once(now=1000), 'none')
                t.inbound('stuck','video');t.state['inflight']['stuck']['at']=1
                with patch.object(g, '_scan_active', return_value=True):
                    self.assertEqual(g.check_once(now=1000), 'none')
                with patch.object(g, '_scan_active', return_value=False):
                    self.assertEqual(g.check_once(now=1000), 'alert')
                    self.assertEqual(g.check_once(now=2000), 'alert')
            ch.bot_manager.updater.bot.send_message.assert_called_once()

    def test_failed_store_rolls_back_memory_on_write_failure(self):
        with TemporaryDirectory() as d:
            store = FailedDeliveryStore(Path(d)/'failed.json')
            store.put('a', {'expires':4102444800})
            with patch.object(store, '_save', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):store.put('b',{'expires':4102444800})
                with self.assertRaises(OSError):store.remove('a')
            self.assertIsNotNone(store.get('a'));self.assertIsNone(store.get('b'))
            self.assertEqual(store.records, FailedDeliveryStore(store.path).records)

    def test_text_failure_saved_and_notice_timeout_does_not_escape(self):
        with TemporaryDirectory() as d:
            p=object.__new__(SlaveMessageProcessor);p.logger=Mock();p.bot=Mock()
            p.channel=SimpleNamespace(config={'admins':[1]});p.failed_media_root=Path(d)/'media'
            p.failure_store=FailedDeliveryStore(Path(d)/'failed.json');p.failed_messages={}
            p._failure_record=Mock(return_value={'uid':'a','text':'kept','expires':4102444800})
            p.bot.updater.bot.send_message.side_effect=TimedOut()
            msg=SimpleNamespace(uid='a',path=None,type=MsgType.Text,vendor_specific={})
            p._report_delivery_failure(msg,SendUnconfirmed(),1,None,0)
            record=FailedDeliveryStore(p.failure_store.path).items()[0][1]
            self.assertEqual(record['storage'],'text');self.assertEqual(record['text'],'kept')
            self.assertEqual(msg.vendor_specific['telegram_delivery_status'],'stored_for_retry')

    def test_post_delivery_side_effect_failures_do_not_fail_delivery(self):
        from efb_telegram_master.delivery_policy import DeliveryPolicy
        p=object.__new__(SlaveMessageProcessor);p.logger=Mock();p.telemetry=Mock();p.channel=Mock()
        p.telemetry.delivered.side_effect=OSError('disk full')
        p.channel.wechat_read_ui.mark_message_read.side_effect=RuntimeError('offline')
        p.delivery_policy=Mock(return_value=DeliveryPolicy.NORMAL)
        p.get_slave_msg_dest=Mock(return_value=('',(1,None)));p.is_silent=Mock(return_value=False)
        p.dispatch_with_retry=Mock();p._report_delivery_failure=Mock()
        p.delivery_trace_id=Mock(return_value='abcdef123456');p.delivery_message_type=Mock(return_value='text')
        msg=SimpleNamespace(uid='one',path=None,type=MsgType.Text,edit=False,vendor_specific={})
        self.assertIs(p.send_message(msg),msg)
        p.dispatch_with_retry.assert_called_once();p._report_delivery_failure.assert_not_called()
        self.assertEqual(msg.vendor_specific['telegram_delivery_status'],'delivered')

    def test_digest_timeout_is_claimed_before_attempt_and_not_replayed(self):
        with TemporaryDirectory() as d:
            ch=SimpleNamespace(config={'admins':[1,1]},bot_manager=Mock())
            ch.bot_manager.updater.bot.send_message.side_effect=TimedOut()
            stats={'silent':0};g=DigestGuard(ch,Path(d)/'digest.json',lambda:stats,interval=300)
            g.set_enabled(True,now=1);stats['silent']=1
            self.assertEqual(g.check_once(now=400),'sent')
            restored=DigestGuard(ch,g.state_path,lambda:stats,interval=300)
            self.assertEqual(restored.check_once(now=800),'empty')
            ch.bot_manager.updater.bot.send_message.assert_called_once()
