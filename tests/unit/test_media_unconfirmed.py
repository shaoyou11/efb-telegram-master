import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from telegram.error import TimedOut, NetworkError, RetryAfter, BadRequest
from efb_telegram_master.bot_manager import TelegramBotManager
from efb_telegram_master.delivery_outcome import MediaSendUnconfirmed
from efb_telegram_master.slave_message import SlaveMessageProcessor
from efb_telegram_master.failed_delivery import FailedDeliveryStore

class MediaOutcomeTests(unittest.TestCase):
    def test_media_timeout_never_retries_at_either_layer(self):
        for enabled in [False, True]:
            calls = Mock(side_effect=TimedOut())
            def send_video(**kwargs): return calls(**kwargs)
            wrapped = TelegramBotManager.Decorators.retry_on_timeout(send_video)
            p = object.__new__(SlaveMessageProcessor)
            p.logger = Mock();p.dispatch_message = wrapped;p.prepare_file_retry = Mock()
            with patch.object(TelegramBotManager.Decorators, 'enable_retry', enabled):
                with self.assertRaises(MediaSendUnconfirmed):
                    p.dispatch_with_retry(msg=SimpleNamespace(uid='one',path='/video',file=None))
            calls.assert_called_once();p.prepare_file_retry.assert_not_called()

    def test_media_network_error_does_not_retry(self):
        calls = Mock(side_effect=NetworkError('response unknown'))
        def send_document():return calls()
        with patch.object(TelegramBotManager.Decorators,'enable_retry',True):
            with self.assertRaises(MediaSendUnconfirmed):
                TelegramBotManager.Decorators.retry_on_timeout(send_document)()
        calls.assert_called_once()

    def test_confirmed_rate_rejection_may_retry(self):
        calls = Mock(side_effect=[RetryAfter(1),'ok'])
        def send_video():return calls()
        with patch.object(TelegramBotManager.Decorators,'enable_retry',True), patch('efb_telegram_master.bot_manager.time.sleep'):
            self.assertEqual(TelegramBotManager.Decorators.retry_on_timeout(send_video)(),'ok')
        self.assertEqual(calls.call_count,2)

    def test_explicit_bad_request_is_not_an_unknown_outcome(self):
        calls=Mock(side_effect=BadRequest('rejected'))
        def send_video(**kwargs):return calls(**kwargs)
        p=object.__new__(SlaveMessageProcessor);p.logger=Mock()
        p.dispatch_message=TelegramBotManager.Decorators.retry_on_timeout(send_video)
        with patch.object(TelegramBotManager.Decorators,'enable_retry',True):
            with self.assertRaises(BadRequest):
                p.dispatch_with_retry(msg=SimpleNamespace(uid='one',path='/video',file=None))
        calls.assert_called_once()

    def test_raw_outer_timeout_does_not_reopen_media_attempt(self):
        p=object.__new__(SlaveMessageProcessor);p.logger=Mock();p.dispatch_message=Mock(side_effect=TimedOut())
        with self.assertRaises(MediaSendUnconfirmed):
            p.dispatch_with_retry(msg=SimpleNamespace(uid='one',path='/video',file=None))
        p.dispatch_message.assert_called_once()

    def test_uncertain_attachment_persists_even_if_notice_times_out(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);original=root/'original.mp4';original.write_bytes(b'video-data')
            p=object.__new__(SlaveMessageProcessor);p.logger=Mock();p.bot=Mock();p.channel=SimpleNamespace(config={'admins':[1]})
            p.failed_media_root=root/'media';p.failure_store=FailedDeliveryStore(root/'failed.json');p.failed_messages={}
            p._failure_record=Mock(return_value={'uid':'one','expires':9999999999})
            p.bot.updater.bot.send_message.side_effect=TimedOut()
            msg=SimpleNamespace(uid='one',path=str(original),type='Video',vendor_specific={})
            p._report_delivery_failure(msg,MediaSendUnconfirmed(),1,None,10)
            self.assertEqual(msg.vendor_specific['telegram_delivery_status'],'stored_for_retry')
            self.assertTrue((root/'failed.json').is_file())
            self.assertEqual(len(list((root/'media').iterdir())),1)
            p.bot.updater.bot.send_message.assert_called_once();p.bot.send_message.assert_not_called()
            self.assertIn('发送结果未确认',p.bot.updater.bot.send_message.call_args.args[1])

if __name__=='__main__':unittest.main()
