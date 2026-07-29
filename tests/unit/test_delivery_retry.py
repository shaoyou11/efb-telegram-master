from types import SimpleNamespace
from unittest.mock import Mock

from ehforwarderbot import Message
from telegram.error import RetryAfter

from efb_telegram_master.slave_message import SlaveMessageProcessor


def test_delivery_status_is_recorded_on_message():
    msg = Message()

    SlaveMessageProcessor.mark_delivery(msg, "delivered")

    assert msg.vendor_specific["telegram_delivery_status"] == "delivered"


def test_retry_after_waits_and_retries(monkeypatch):
    processor = object.__new__(SlaveMessageProcessor)
    processor.logger = Mock()
    processor.dispatch_message = Mock(side_effect=[RetryAfter(3), None])
    processor.prepare_file_retry = Mock()
    sleep = Mock()
    monkeypatch.setattr("efb_telegram_master.slave_message.time.sleep", sleep)
    msg = SimpleNamespace(uid="file-1", path=None, file=None)

    processor.dispatch_with_retry(msg=msg)

    assert processor.dispatch_message.call_count == 2
    sleep.assert_called_once_with(3.0)
