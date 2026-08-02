import inspect

from ehforwarderbot.types import ChatID
from efb_telegram_master import TelegramChannel
from efb_telegram_master import utils
from efb_telegram_master.watchdog_control import (
    COMWECHAT_COMMANDS,
    COMWECHAT_GROUP_COMMANDS,
    COMMANDS,
    HELP_TEXT,
    LINKED_GROUP_COMMANDS,
    PRIVATE_COMMANDS,
    UNLINKED_GROUP_COMMANDS,
    WatchdogControl,
    change_summary,
    format_status,
    keyboard,
    state_mask,
)


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


def test_format_status_shows_runtime_recovery_configuration():
    text = format_status({
        "master_enabled": True,
        "event_enabled": True,
        "night_enabled": True,
        "daily_start": "02:50",
        "daily_end": "03:50",
        "poll_seconds": 120,
        "click_cooldown_seconds": 120,
        "max_recovery_failures": 3,
    })

    assert "凌晨时段：02:50-03:50" in text
    assert "每2分钟检查" in text
    assert "连续失败3次暂停" in text


def test_all_command_descriptions_are_chinese():
    descriptions = dict(PRIVATE_COMMANDS)

    assert descriptions == {
        "status": "查看 EFB 综合运行状态。",
        "watchdog": "管理微信自动恢复开关。",
        "wechat": "管理微信登录与自动恢复。",
        "filter": "设置微信会话接收策略。",
        "cleanup": "查看 EFB 存储占用。",
        "backup_info": "查看配置备份状态。",
        "chat": "创建会话入口。",
        "login": "获取微信登录二维码。",
        "info": "显示当前 Telegram 会话信息。",
        "help": "显示命令列表。",
        "react": "回应消息或查看回应者。",
        "rm": "删除远程会话中的消息。",
    }
    assert tuple(command for command, _ in PRIVATE_COMMANDS) == (
        "status",
        "watchdog",
        "wechat",
        "filter",
        "cleanup",
        "backup_info",
        "chat",
        "login",
        "info",
        "help",
        "react",
        "rm",
    )


def test_help_text_is_chinese_and_lists_all_commands():
    for command, _ in COMMANDS:
        assert f"/{command}" in HELP_TEXT

    assert "EFB Telegram 主端" in HELP_TEXT
    assert "绑定远程会话" in HELP_TEXT
    assert "微信自动恢复" in HELP_TEXT
    assert "/health" not in HELP_TEXT
    assert "Link a remote chat" not in HELP_TEXT


def test_health_remains_a_hidden_compatibility_alias():
    source = inspect.getsource(TelegramChannel.__init__)

    assert '("health", self.operations_ui.health)' in source
    assert "health" not in dict(PRIVATE_COMMANDS)


def test_watchdog_callback_is_registered_before_session_expired_fallback():
    source = inspect.getsource(TelegramChannel.__init__)

    watchdog = source.index("self.watchdog_control = WatchdogControl(self)")
    fallback = source.index("CallbackQueryHandler(self.bot_manager.session_expired)")
    assert watchdog < fallback


def test_keyboard_contains_hide_button_and_preserves_initial_state():
    initial = {
        "master_enabled": True,
        "event_enabled": False,
        "night_enabled": True,
    }
    current = dict(initial, event_enabled=True)
    markup = keyboard(current, state_mask(initial))

    assert markup.inline_keyboard[-1][0].text == "完成并隐藏"
    assert markup.inline_keyboard[-1][0].callback_data == "watchdog:hide:5"
    assert markup.inline_keyboard[1][0].callback_data == "watchdog:set:event:off:5"


def test_change_summary_lists_only_changes():
    current = {
        "master_enabled": False,
        "event_enabled": False,
        "night_enabled": True,
    }

    summary = change_summary(5, current)

    assert "总开关：开启 → 关闭" in summary
    assert "全天事件恢复" not in summary
    assert "凌晨自主检测" not in summary
    assert "本次设置已完成" in summary


def test_change_summary_reports_no_changes():
    current = {
        "master_enabled": True,
        "event_enabled": False,
        "night_enabled": True,
    }

    assert "本次未更改任何设置" in change_summary(5, current)


def test_unlinked_group_menu_only_contains_telegram_group_commands():
    commands = dict(UNLINKED_GROUP_COMMANDS)

    assert "link" in commands
    assert "unlink_all" not in commands
    assert "update_info" not in commands
    assert "login" not in commands
    assert "getmemberlist" not in commands


def test_linked_group_menu_adds_binding_commands():
    commands = dict(LINKED_GROUP_COMMANDS)

    assert "unlink_all" in commands
    assert "update_info" in commands


def test_comwechat_private_link_adds_only_general_session_commands():
    commands = dict(WatchdogControl.commands_for_links(
        [utils.chat_id_to_str("honus.comwechat", ChatID("wxid_example"))]
    ))

    assert set(dict(COMWECHAT_COMMANDS)).issubset(commands)
    assert "getmemberlist" not in commands
    assert "changename" not in commands


def test_comwechat_group_link_adds_group_only_commands():
    commands = dict(WatchdogControl.commands_for_links(
        [utils.chat_id_to_str("honus.comwechat", ChatID("123456@chatroom"))]
    ))

    assert set(dict(COMWECHAT_COMMANDS)).issubset(commands)
    assert set(dict(COMWECHAT_GROUP_COMMANDS)).issubset(commands)


def test_topic_links_are_included_when_refreshing_forum_group_menu():
    group_link = utils.chat_id_to_str(
        "honus.comwechat",
        ChatID("123456@chatroom"),
    )

    class FakeDB:
        @staticmethod
        def get_chat_assoc(master_uid):
            return []

        @staticmethod
        def get_topic_slaves(topic_chat_id):
            return [(group_link, 123)]

    watchdog = WatchdogControl.__new__(WatchdogControl)
    watchdog.channel = type("Channel", (), {
        "channel_id": "blueset.telegram",
        "db": FakeDB(),
    })()

    assert watchdog.get_group_links(-100123) == [group_link]
    commands = dict(watchdog.commands_for_links(watchdog.get_group_links(-100123)))
    assert "getmemberlist" in commands
    assert "at" in commands
