from efb_telegram_master.recall_notice import format_wechat_recall_notice
from efb_telegram_master.bot_manager import TelegramBotManager


def test_wechat_recall_notice_keeps_actor_label():
    assert format_wechat_recall_notice({"wechat_recall": {"actor": "self"}}) == "微信消息已撤回：自己撤回"
    assert format_wechat_recall_notice({"wechat_recall": {"actor": "other"}}) == "微信消息已撤回：对方撤回"


def test_non_recall_status_has_no_special_notice():
    assert format_wechat_recall_notice({}) is None


def test_media_mapping_is_json_safe():
    normalized = TelegramBotManager._normalize_media_kwargs({
        "reply_markup": {"inline_keyboard": []},
        "api_kwargs": {"example": {"nested": True}},
    })
    assert isinstance(normalized["reply_markup"], str)
    assert isinstance(normalized["api_kwargs"]["example"], str)
