import inspect

from efb_telegram_master import TelegramChannel
from efb_telegram_master.watchdog_control import COMMANDS, HELP_TEXT, format_status


def test_format_status_shows_master_and_independent_switches():
    text = format_status({
        "master_enabled": False,
        "event_enabled": True,
        "night_enabled": False,
    })

    assert "总开关：关闭" in text
    assert "全天事件恢复：开启" in text
    assert "凌晨自主检测：关闭" in text
    assert "Watchdog" not in text


def test_all_command_descriptions_are_chinese():
    descriptions = dict(COMMANDS)

    assert descriptions == {
        "help": "显示命令列表。",
        "link": "绑定远程会话至群组。",
        "unlink_all": "解除群组中的全部远程会话。",
        "info": "显示当前 Telegram 会话信息。",
        "chat": "创建会话入口。",
        "extra": "访问微信端附加功能。",
        "watchdog": "管理微信自动恢复开关。",
        "update_info": "更新已绑定群组信息。",
        "react": "回应消息或查看回应者。",
        "rm": "删除远程会话中的消息。",
    }


def test_help_text_is_chinese_and_lists_all_commands():
    for command, _ in COMMANDS:
        assert f"/{command}" in HELP_TEXT

    assert "EFB Telegram 主端" in HELP_TEXT
    assert "绑定远程会话" in HELP_TEXT
    assert "微信自动恢复" in HELP_TEXT
    assert "Link a remote chat" not in HELP_TEXT


def test_watchdog_callback_is_registered_before_session_expired_fallback():
    source = inspect.getsource(TelegramChannel.__init__)

    watchdog = source.index("self.watchdog_control = WatchdogControl(self)")
    fallback = source.index("CallbackQueryHandler(self.bot_manager.session_expired)")
    assert watchdog < fallback
