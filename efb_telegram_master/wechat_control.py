import logging

from ehforwarderbot import coordinator
from ehforwarderbot.types import ExtraCommandName
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler


LOGGER = logging.getLogger(__name__)
COMWECHAT_CHANNEL_ID = "honus.comwechat"
PANEL_TEXT = "微信管理\n\n请选择需要执行的操作。"
LOGIN_PROMPT_TEXT = "请扫描二维码登录"


def find_comwechat_channel(slaves):
    for channel in slaves.values():
        if str(channel.channel_id) == COMWECHAT_CHANNEL_ID:
            return channel
    return None


def panel_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("重新扫码登录", callback_data="wechat:login")],
        [InlineKeyboardButton("强制退出微信", callback_data="wechat:logout")],
        [InlineKeyboardButton("自动恢复设置", callback_data="wechat:watchdog")],
        [InlineKeyboardButton("关闭", callback_data="wechat:close")],
    ])


def confirmation_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("确认退出微信", callback_data="wechat:logout_confirm")],
        [InlineKeyboardButton("取消", callback_data="wechat:cancel")],
    ])


def login_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("刷新二维码", callback_data="wechat:login_refresh"),
         InlineKeyboardButton("撤回二维码", callback_data="wechat:login_cancel")],
    ])


class WeChatControl:
    def __init__(self, channel):
        self.channel = channel
        self._login_prompt_messages = set()
        dispatcher = channel.bot_manager.dispatcher
        dispatcher.add_handler(CommandHandler("login", self.login))
        dispatcher.add_handler(CommandHandler("wechat", self.show))
        dispatcher.add_handler(
            CallbackQueryHandler(self.callback, pattern=r"^wechat:")
        )

    def is_admin(self, update):
        return update.effective_user and update.effective_user.id in self.channel.config["admins"]

    def show(self, update: Update, context: CallbackContext):
        if not self.is_admin(update):
            return
        update.effective_message.reply_text(PANEL_TEXT, reply_markup=panel_keyboard())

    @staticmethod
    def call_extra(command):
        channel = find_comwechat_channel(coordinator.slaves)
        if channel is None:
            raise RuntimeError("ComWechat channel is unavailable")
        functions = channel.get_extra_functions()
        command_name = ExtraCommandName(command)
        if command_name not in functions:
            raise RuntimeError(f"ComWechat command is unavailable: {command}")
        return functions[command_name]("")

    def run_action(self, message, command, pending_text, edit=False):
        status = message if edit else message.reply_text(pending_text)
        if edit:
            status.edit_text(pending_text)
        try:
            result = self.call_extra(command)
            pending_login = command == "reauth" and str(result or "").startswith(
                (LOGIN_PROMPT_TEXT, "登录二维码仍在有效期内", "登录二维码正在生成")
            )
            if pending_login:
                self.track_login_prompt(status)
            else:
                self.forget_login_prompt(status)
            if pending_login:
                status.edit_text(result, reply_markup=login_keyboard())
            else:
                status.edit_text(result or "操作已完成。")
        except Exception:
            LOGGER.exception("failed to execute ComWechat command: %s", command)
            self.forget_login_prompt(status)
            status.edit_text("微信服务暂时无法完成此操作，请稍后再试。")

    def track_login_prompt(self, message):
        self._login_prompt_messages.add((message.chat_id, message.message_id))

    def forget_login_prompt(self, message):
        self._login_prompt_messages.discard((message.chat_id, message.message_id))

    def cleanup_login_prompts(self):
        """Delete tracked QR instructions after ComWechat reports login success."""
        prompts = list(self._login_prompt_messages)
        for chat_id, message_id in prompts:
            try:
                self.channel.bot_manager.delete_message(chat_id, message_id)
            except Exception:
                LOGGER.exception(
                    "failed to delete stale WeChat login prompt: %s/%s",
                    chat_id,
                    message_id,
                )
            finally:
                self._login_prompt_messages.discard((chat_id, message_id))
        return len(prompts)

    def login(self, update: Update, context: CallbackContext):
        if not self.is_admin(update):
            return
        self.run_action(
            update.effective_message,
            "reauth",
            "正在获取微信登录二维码，请稍候……",
        )

    def callback(self, update: Update, context: CallbackContext):
        if not self.is_admin(update):
            return
        query = update.callback_query
        action = query.data.removeprefix("wechat:")

        if action == "close":
            query.answer()
            query.message.delete()
            return
        if action in ("login_refresh", "login_cancel"):
            query.answer("正在处理，请稍候")
            self.run_action(
                query.message,
                "reauth" if action == "login_refresh" else "cancel_login_qr",
                "正在检查二维码……" if action == "login_refresh" else "正在撤回二维码……",
                edit=True,
            )
            return
        if action == "login":
            query.answer()
            query.edit_message_reply_markup(reply_markup=None)
            self.run_action(
                query.message,
                "reauth",
                "正在获取微信登录二维码，请稍候……",
            )
            return
        if action == "logout":
            query.answer()
            query.edit_message_text(
                "确认要强制退出当前微信登录吗？",
                reply_markup=confirmation_keyboard(),
            )
            return
        if action == "logout_confirm":
            query.answer()
            query.edit_message_reply_markup(reply_markup=None)
            self.run_action(query.message, "force_logout", "正在退出微信，请稍候……")
            return
        if action == "cancel":
            query.answer()
            query.edit_message_text(PANEL_TEXT, reply_markup=panel_keyboard())
            return
        if action == "watchdog":
            query.answer()
            self.channel.watchdog_control.show(update, context)
            query.message.delete()
            return

        query.answer("无法识别该操作。", show_alert=True)
