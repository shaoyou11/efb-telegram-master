import re
import sys
from io import BytesIO
from types import ModuleType

from pytest import raises

from efb_telegram_master.utils import b64de, b64en, message_id_to_str, \
    message_id_str_to_id, chat_id_str_to_id, chat_id_to_str, convert_tgs_to_gif


def test_flag(channel):
    flag = channel.flag
    with raises(ValueError, match="__unknown_flag__"):
        flag("__unknown_flag__")

    assert flag("chats_per_page") is not None, "Existing flag should return a value"


def test_url_safe_base64():
    data = "信じたものは、\n都合の良い妄想を繰り返し映し出す鏡。"
    assert b64de(b64en(data)) == data
    # Per docs, encoded base64 for startgroup shall only consist of [a-zA-Z0-9_-]+
    # https://core.telegram.org/bots
    encoded = b64en(data)
    assert re.match(r"^[a-zA-Z0-9_-]+$", encoded)


def test_message_id_str_conversion():
    chat_id = 1
    message_id = 2
    assert (chat_id, message_id) == message_id_str_to_id(
        message_id_to_str(chat_id=chat_id, message_id=message_id))


def test_chat_id_str_conversion():
    channel_id = "__channel_id__"
    chat_id = "__chat_id__"
    group_id = "__group_id__"

    assert (channel_id, chat_id, None) == chat_id_str_to_id(
        chat_id_to_str(channel_id=channel_id, chat_uid=chat_id)
    ), "Converting channel-chat ID without group ID"

    assert (channel_id, chat_id, group_id) == chat_id_str_to_id(
        chat_id_to_str(channel_id=channel_id, chat_uid=chat_id, group_id=group_id)
    ), "Converting channel-chat ID with group ID"


def test_convert_tgs_to_gif(monkeypatch):
    export_calls = []

    def fake_export(animation, gif_file, **kwargs):
        export_calls.append((animation, gif_file, kwargs))
        gif_file.write(b"GIF89a")

    gif_exporter = ModuleType("lottie.exporters.gif")
    gif_exporter.export_gif = fake_export
    monkeypatch.setitem(sys.modules, "lottie.exporters.gif", gif_exporter)
    out = BytesIO()
    with open('tests/mocks/AnimatedSticker.tgs', 'rb') as f:
        assert convert_tgs_to_gif(f, out), "conversion outcome"
    assert out.seek(0, 2), "converted TGS file should not be empty"
    assert len(export_calls) == 1
    assert export_calls[0][1] is out
    assert export_calls[0][2] == {"skip_frames": 5, "dpi": 48}


def test_convert_tgs_to_gif_failure(monkeypatch):
    def failed_export(*args, **kwargs):
        raise RuntimeError("renderer unavailable")

    gif_exporter = ModuleType("lottie.exporters.gif")
    gif_exporter.export_gif = failed_export
    monkeypatch.setitem(sys.modules, "lottie.exporters.gif", gif_exporter)
    out = BytesIO()
    with open('tests/mocks/AnimatedSticker.tgs', 'rb') as f:
        assert not convert_tgs_to_gif(f, out)
