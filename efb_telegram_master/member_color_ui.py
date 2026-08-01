import logging

from ehforwarderbot import coordinator
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import CallbackContext, CallbackQueryHandler, CommandHandler

from .wechat_control import find_comwechat_channel


LOGGER = logging.getLogger(__name__)


def panel_text(enabled: bool, avatar_count: int, total_count: int) -> str:
    return (
        f"群成员个性图标：{'已开启' if enabled else '已关闭'}\n"
        f"已记录：{total_count} 人\n"
        "每位成员使用固定的小图标，仅影响 Telegram 群聊显示。"
    )


def panel_keyboard(enabled: bool) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "关闭个性图标" if enabled else "开启个性图标",
                callback_data=f"membercolor:set:{'off' if enabled else 'on'}",
            ),
            InlineKeyboardButton("关闭页面", callback_data="membercolor:close"),
        ]
    ])


class MemberColorUI:
    def __init__(self, channel):
        self.channel = channel
        dispatcher = channel.bot_manager.dispatcher
        dispatcher.add_handler(CommandHandler("membercolor", self.show))
        dispatcher.add_handler(
            CallbackQueryHandler(self.callback, pattern=r"^membercolor:")
        )

    def is_admin(self, update: Update) -> bool:
        return bool(
            update.effective_user
            and update.effective_user.id in self.channel.config["admins"]
        )

    @staticmethod
    def marker_store():
        slave = find_comwechat_channel(coordinator.slaves)
        return getattr(slave, "member_avatar_markers", None) if slave else None

    def render(self, update: Update) -> None:
        store = self.marker_store()
        if store is None:
            text = "群成员个性图标\n\n微信从端尚未就绪，请稍后重试。"
            markup = InlineKeyboardMarkup([
                [InlineKeyboardButton("关闭页面", callback_data="membercolor:close")]
            ])
        else:
            avatar_count, total_count = store.counts()
            text = panel_text(store.enabled, avatar_count, total_count)
            markup = panel_keyboard(store.enabled)

        if update.callback_query:
            update.callback_query.edit_message_text(text, reply_markup=markup)
        else:
            update.effective_message.reply_text(text, reply_markup=markup)

    def show(self, update: Update, context: CallbackContext) -> None:
        if self.is_admin(update):
            self.render(update)

    def callback(self, update: Update, context: CallbackContext) -> None:
        query = update.callback_query
        if not query:
            return
        if not self.is_admin(update):
            query.answer("无权执行", show_alert=True)
            return

        action = query.data or ""
        if action == "membercolor:close":
            query.answer()
            query.message.delete()
            return

        store = self.marker_store()
        if store is None:
            query.answer("微信从端尚未就绪", show_alert=True)
            return
        if action == "membercolor:set:on":
            store.set_enabled(True)
        elif action == "membercolor:set:off":
            store.set_enabled(False)
        else:
            query.answer("无效操作", show_alert=True)
            return

        query.answer("设置已更新")
        self.render(update)
