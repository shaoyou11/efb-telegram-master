import logging
import os

import requests
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler


LOGGER = logging.getLogger(__name__)
COMMANDS = (
    ("help", "Show commands list."),
    ("link", "Link a remote chat to a group."),
    ("unlink_all", "Unlink all remote chats from a group."),
    ("info", "Display information of the current Telegram chat."),
    ("chat", "Generate a chat head."),
    ("extra", "Access additional features from Slave Channels."),
    ("watchdog", "管理微信自动恢复开关。"),
    ("update_info", "Update info of linked Telegram group."),
    ("react", "Send a reaction to a message, or show a list of reactors."),
    ("rm", "Remove a message from its remote chat."),
)


def switch_text(enabled):
    return "开启" if enabled else "关闭"


def format_status(state):
    return (
        "微信自动恢复 Watchdog\n\n"
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
            update.effective_message.reply_text("Watchdog 暂时无法连接，请稍后再试。")

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
