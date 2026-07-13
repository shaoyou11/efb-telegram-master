import logging
import os

import requests
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler


LOGGER = logging.getLogger(__name__)
COMMANDS = (
    ("help", "显示命令列表。"),
    ("link", "绑定远程会话至群组。"),
    ("unlink_all", "解除群组中的全部远程会话。"),
    ("info", "显示当前 Telegram 会话信息。"),
    ("chat", "创建会话入口。"),
    ("extra", "访问微信端附加功能。"),
    ("watchdog", "管理微信自动恢复开关。"),
    ("update_info", "更新已绑定群组信息。"),
    ("react", "回应消息或查看回应者。"),
    ("rm", "删除远程会话中的消息。"),
)

HELP_TEXT = """EFB Telegram 主端
/link
    将远程会话绑定至一个空的 Telegram 群组，可附加正则表达式筛选结果。
/chat
    创建会话入口以开始聊天，可附加正则表达式筛选结果。
/extra
    列出微信端提供的附加功能。
/unlink_all
    解除当前群组中的全部远程会话绑定。
/info
    显示当前 Telegram 会话信息。
/react [表情]
    回应一条消息，或查看已经回应的成员。
/update_info
    更新已绑定 Telegram 群组的信息，仅适用于机器人为管理员的单一绑定群组。
/rm
    从远程会话中删除所回复的消息。
/watchdog
    管理微信自动恢复的总开关、全天事件恢复和凌晨自主检测。
/help
    显示本命令列表。"""


def switch_text(enabled):
    return "开启" if enabled else "关闭"


def format_status(state):
    return (
        "微信自动恢复监控\n\n"
        f"总开关：{switch_text(state['master_enabled'])}\n"
        f"全天事件恢复：{switch_text(state['event_enabled'])}\n"
        f"凌晨自主检测：{switch_text(state['night_enabled'])}\n"
        "凌晨时段：02:50-03:50"
    )


def keyboard(state):
    rows = []
    labels = (("master", "总开关"), ("event", "全天事件恢复"), ("night", "凌晨自主检测"))
    for setting, label in labels:
        enabled = state[f"{setting}_enabled"]
        rows.append([InlineKeyboardButton(
            f"{'✅' if enabled else '⬜'} {label}",
            callback_data=f"watchdog:{setting}:{'off' if enabled else 'on'}",
        )])
    return InlineKeyboardMarkup(rows)


class WatchdogControl:
    def __init__(self, channel):
        self.channel = channel
        self.url = os.getenv("WATCHDOG_CONTROL_URL", "http://127.0.0.1:18989")
        channel.bot_manager.dispatcher.add_handler(CommandHandler("watchdog", self.show))
        channel.bot_manager.dispatcher.add_handler(
            CallbackQueryHandler(self.toggle, pattern=r"^watchdog:")
        )
        self.update_command_menu()

    def update_command_menu(self):
        try:
            self.channel.bot_manager.updater.bot.set_my_commands(
                [BotCommand(command, description) for command, description in COMMANDS]
            )
        except Exception as error:
            LOGGER.warning("failed to update Telegram command menu: %s", error)

    def get_state(self):
        response = requests.get(f"{self.url}/status", timeout=5)
        response.raise_for_status()
        return response.json()

    def set_state(self, setting, enabled):
        response = requests.post(
            f"{self.url}/control",
            json={"setting": setting, "enabled": enabled},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()

    def is_admin(self, update):
        return update.effective_user and update.effective_user.id in self.channel.config["admins"]

    def show(self, update: Update, context: CallbackContext):
        if not self.is_admin(update):
            return
        try:
            state = self.get_state()
            update.effective_message.reply_text(format_status(state), reply_markup=keyboard(state))
        except Exception:
            LOGGER.exception("failed to read watchdog status")
            update.effective_message.reply_text("自动恢复服务暂时无法连接，请稍后再试。")

    def toggle(self, update: Update, context: CallbackContext):
        query = update.callback_query
        if not self.is_admin(update):
            query.answer("无操作权限", show_alert=True)
            return
        _, setting, action = query.data.split(":", 2)
        try:
            state = self.set_state(setting, action == "on")
            query.answer("设置已保存")
            query.edit_message_text(format_status(state), reply_markup=keyboard(state))
        except Exception:
            LOGGER.exception("failed to update watchdog setting")
            query.answer("设置失败，请稍后再试", show_alert=True)
