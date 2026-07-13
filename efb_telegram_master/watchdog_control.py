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

SETTINGS = (
    ("master", "总开关"),
    ("event", "全天事件恢复"),
    ("night", "凌晨自主检测"),
)


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


def state_mask(state):
    mask = 0
    for bit, (setting, _) in enumerate(SETTINGS):
        if state[f"{setting}_enabled"]:
            mask |= 1 << bit
    return mask


def change_summary(initial_mask, state):
    changes = []
    for bit, (setting, label) in enumerate(SETTINGS):
        before = bool(initial_mask & (1 << bit))
        after = state[f"{setting}_enabled"]
        if before != after:
            changes.append(f"{label}：{switch_text(before)} → {switch_text(after)}")

    if not changes:
        return "微信自动恢复设置已完成\n\n本次未更改任何设置。"
    return "微信自动恢复设置已完成\n\n本次更改：\n" + "\n".join(changes)


def keyboard(state, initial_mask=None):
    if initial_mask is None:
        initial_mask = state_mask(state)
    rows = []
    for setting, label in SETTINGS:
        enabled = state[f"{setting}_enabled"]
        rows.append([InlineKeyboardButton(
            f"{'✅' if enabled else '⬜'} {label}",
            callback_data=f"watchdog:set:{setting}:{'off' if enabled else 'on'}:{initial_mask}",
        )])
    rows.append([InlineKeyboardButton(
        "完成并隐藏",
        callback_data=f"watchdog:hide:{initial_mask}",
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
        try:
            parts = query.data.split(":")
            if parts[1] == "hide":
                state = self.get_state()
                query.answer("面板已隐藏")
                query.edit_message_text(change_summary(int(parts[2]), state))
                return

            if parts[1] == "set":
                _, _, setting, action, initial_mask = parts
                initial_mask = int(initial_mask)
            else:
                _, setting, action = parts
                initial_mask = state_mask(self.get_state())
            state = self.set_state(setting, action == "on")
            query.answer("设置已保存")
            query.edit_message_text(
                format_status(state),
                reply_markup=keyboard(state, initial_mask),
            )
        except Exception:
            LOGGER.exception("failed to update watchdog setting")
            query.answer("设置失败，请稍后再试", show_alert=True)
