import logging
import os

import requests
from ehforwarderbot.types import ChatID
from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler

from . import utils


LOGGER = logging.getLogger(__name__)
PRIVATE_COMMANDS = (
    ("status", "查看 EFB 综合运行状态。"),
    ("watchdog", "管理微信自动恢复开关。"),
    ("wechat", "管理微信登录与自动恢复。"),
    ("filter", "设置微信会话接收策略。"),
    ("namespoiler", "设置群成员微信姓名隐藏。"),
    ("cleanup", "查看 EFB 存储占用。"),
    ("backup_info", "查看配置备份状态。"),
    ("chat", "创建会话入口。"),
    ("login", "获取微信登录二维码。"),
    ("info", "显示当前 Telegram 会话信息。"),
    ("help", "显示命令列表。"),
    ("react", "回应消息或查看回应者。"),
    ("rm", "删除远程会话中的消息。"),
)
UNLINKED_GROUP_COMMANDS = (
    ("help", "显示命令列表。"),
    ("link", "为本群绑定远程会话。"),
    ("info", "显示本群的绑定信息。"),
)
LINKED_GROUP_COMMANDS = UNLINKED_GROUP_COMMANDS + (
    ("unlink_all", "解除本群的全部远程会话。"),
    ("chat", "创建已绑定会话入口。"),
    ("update_info", "更新本群名称和头像。"),
    ("filter", "设置当前微信会话接收策略。"),
    ("react", "回应消息或查看回应者。"),
    ("rm", "删除远程会话中的消息。"),
)
COMWECHAT_COMMANDS = (
    ("helpcomwechat", "显示当前微信会话可用命令。"),
    ("search", "按昵称搜索微信联系人。"),
    ("sendcard", "向当前会话发送联系人名片。"),
    ("addfriend", "发送微信好友申请。"),
    ("getstaticinfo", "查看微信联系人与群聊缓存。"),
    ("forward", "生成跨会话转发信息。"),
)
COMWECHAT_GROUP_COMMANDS = (
    ("membercolor", "管理群成员头像主色标记。"),
    ("addtogroup", "将指定微信用户加入当前群聊。"),
    ("getmemberlist", "列出当前微信群成员。"),
    ("at", "在微信群中提醒指定成员。"),
    ("changename", "修改当前微信群名称。"),
)
COMMANDS = PRIVATE_COMMANDS + tuple(
    command for command in LINKED_GROUP_COMMANDS if command[0] not in dict(PRIVATE_COMMANDS)
)

HELP_TEXT = """EFB Telegram 主端
/status
    查看容器、消息、数据库、容量、备份和上游更新综合状态。
/watchdog
    管理微信自动恢复的总开关、全天事件恢复和凌晨自主检测。
/wechat
    打开微信登录、退出和自动恢复管理面板。
/filter [关键词]
    设置微信会话接收策略，或按关键词查找会话。
/namespoiler
    设置 Telegram 群聊中群成员微信姓名是否折叠。
/cleanup
    查看 EFB 存储占用和可手动清理路径。
/backup_info
    查看配置备份状态。
/chat
    创建会话入口以开始聊天，可附加正则表达式筛选结果。
/login
    获取微信登录二维码。
/info
    显示当前 Telegram 会话信息。
/help
    显示本命令列表。
/react [表情]
    回应一条消息，或查看已经回应的成员。
/rm
    从远程会话中删除所回复的消息。
/link
    绑定远程会话至一个空的 Telegram 群组，可附加正则表达式筛选结果。
/unlink_all
    解除当前群组中的全部远程会话绑定。
/update_info
    更新已绑定 Telegram 群组的信息，仅适用于机器人为管理员的单一绑定群组。
/version
    查看 EFB 组件版本。
/filetest
    检查本地 Telegram Bot API 文件支持。
/security
    只扫描配置中的敏感键名，不显示内容。"""

SETTINGS = (
    ("master", "总开关"),
    ("event", "全天事件恢复"),
    ("night", "凌晨自主检测"),
)


def switch_text(enabled):
    return "开启" if enabled else "关闭"


def _duration_text(seconds):
    try:
        seconds = max(0, int(seconds))
    except (TypeError, ValueError):
        return "未知"
    minutes, remainder = divmod(seconds, 60)
    if minutes and remainder:
        return f"{minutes}分{remainder}秒"
    if minutes:
        return f"{minutes}分钟"
    return f"{remainder}秒"


def format_status(state):
    daily_start = state.get("daily_start", "02:50")
    daily_end = state.get("daily_end", "03:50")
    return (
        "微信自动恢复监控\n\n"
        f"总开关：{switch_text(state['master_enabled'])}\n"
        f"全天事件恢复：{switch_text(state['event_enabled'])}\n"
        f"凌晨自主检测：{switch_text(state['night_enabled'])}\n"
        f"凌晨时段：{daily_start}-{daily_end}\n"
        f"恢复配置：每{_duration_text(state.get('poll_seconds', 120))}检查｜"
        f"冷却{_duration_text(state.get('click_cooldown_seconds', 120))}｜"
        f"连续失败{state.get('max_recovery_failures', 3)}次暂停"
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
    return "微信自动恢复本次设置已完成\n\n本次更改：\n" + "\n".join(changes)


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
            bot = self.channel.bot_manager.updater.bot
            bot.set_my_commands(
                self.as_bot_commands(PRIVATE_COMMANDS),
                scope=BotCommandScopeAllPrivateChats(),
            )
            bot.set_my_commands(
                self.as_bot_commands(UNLINKED_GROUP_COMMANDS),
                scope=BotCommandScopeAllGroupChats(),
            )
            self.refresh_linked_group_menus()
        except Exception as error:
            LOGGER.warning("failed to update Telegram command menu: %s", error)

    @staticmethod
    def as_bot_commands(commands):
        return [BotCommand(command, description) for command, description in commands]

    @staticmethod
    def commands_for_links(links):
        commands = list(LINKED_GROUP_COMMANDS)
        parsed_links = []
        for link in links:
            try:
                parsed_links.append(utils.chat_id_str_to_id(link))
            except (TypeError, ValueError):
                LOGGER.warning("ignored malformed linked chat ID while building command menu")
        has_comwechat = any(module_id == "honus.comwechat" for module_id, _, _ in parsed_links)
        has_comwechat_group = any(
            module_id == "honus.comwechat" and str(chat_uid).endswith("@chatroom")
            for module_id, chat_uid, _ in parsed_links
        )
        if has_comwechat:
            commands.extend(COMWECHAT_COMMANDS)
        if has_comwechat_group:
            commands.extend(COMWECHAT_GROUP_COMMANDS)
        return tuple(commands)

    def refresh_linked_group_menus(self):
        group_links = {}
        for master_uid, links in self.channel.db.get_all_chat_assocs().items():
            _, chat_uid, _ = utils.chat_id_str_to_id(master_uid)
            group_links.setdefault(int(chat_uid), []).extend(links)
        for chat_id, links in self.channel.db.get_all_topic_assocs().items():
            group_links.setdefault(int(chat_id), []).extend(links)
        for chat_id, links in group_links.items():
            self.refresh_group_menu(chat_id, links)

    def get_group_links(self, chat_id):
        master_uid = utils.chat_id_to_str(self.channel.channel_id, ChatID(str(chat_id)))
        links = list(self.channel.db.get_chat_assoc(master_uid=master_uid))
        topic_links = self.channel.db.get_topic_slaves(topic_chat_id=chat_id) or []
        links.extend(slave_uid for slave_uid, _ in topic_links)
        return links

    def refresh_group_menu(self, chat_id, links=None):
        try:
            bot = self.channel.bot_manager.updater.bot
            scope = BotCommandScopeChat(chat_id)
            if links is None:
                links = self.get_group_links(chat_id)
            if links:
                bot.set_my_commands(
                    self.as_bot_commands(self.commands_for_links(links)),
                    scope=scope,
                )
            else:
                bot.delete_my_commands(scope=scope)
            return True
        except Exception as error:
            LOGGER.warning("failed to refresh command menu for Telegram chat %s: %s", chat_id, error)
            return False

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
