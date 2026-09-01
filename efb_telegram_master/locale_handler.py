# coding=utf-8

import gettext
import logging
from importlib.resources import files
from typing import TYPE_CHECKING

from language_tags import tags
from telegram.ext import BaseHandler
from telegram import Update

if TYPE_CHECKING:
    from . import TelegramChannel


def normalize_locale(language_code: str) -> str:
    """Map Telegram language tags to the locale catalogs shipped by ETM."""
    normalized = str(language_code or "").replace('-', '_')
    lowered = normalized.lower()
    if lowered in {"zh", "zh_cn", "zh_sg", "zh_hans"}:
        return "zh_CN"
    if lowered in {"zh_tw", "zh_hk", "zh_mo", "zh_hant"}:
        return "zh_TW"

    tag = tags.tag(language_code)
    if tag.language:
        locale = tag.language.format
        if tag.region:
            locale += "_" + tag.region.format
        return locale
    return normalized


class LocaleHandler(BaseHandler):
    """
    Handler class Extract.

    Args:
        channel (TelegramChannel): The ETM channel object.
        pass_update_queue (optional[bool]): If the handler should be passed the
            update queue as a keyword argument called ``update_queue``. It can
            be used to insert updates. Default is ``False``
    """

    def __init__(self, channel: 'TelegramChannel', pass_update_queue: bool = False):
        async def void_function(*args, **kwargs):
            return None

        super().__init__(void_function)
        self.logger = logging.getLogger(__name__)

        self.channel = channel
        self.auto_locale = self.channel.flag('auto_locale')

    def check_update(self, update: object):
        if not self.auto_locale:
            return False
        if not isinstance(update, Update):
            return False
        if not update.effective_user or not update.effective_user.language_code:
            return
        self.logger.debug("[%s] Update has language %s.", update.update_id, update.effective_user.language_code)
        if update.effective_user.language_code and update.effective_user.language_code != self.channel.locale:
            self.channel.locale = update.effective_user.language_code
            locale = normalize_locale(update.effective_user.language_code)
            self.logger.info("Updating locale to %s", locale)
            self.channel.translator = gettext.translation("efb_telegram_master",
                                                          str(files('efb_telegram_master').joinpath('locale')),
                                                          languages=[locale, 'C'],
                                                          fallback=True)
        return False
