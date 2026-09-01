"""PTB 13-style filter names backed by PTB 22 filters."""

from telegram.ext import filters


class _StatusUpdateFilters:
    migrate = filters.StatusUpdate.MIGRATE
    left_chat_member = filters.StatusUpdate.LEFT_CHAT_MEMBER


class Filters:
    all = filters.ALL
    update = filters.UpdateType.MESSAGES
    update_message = filters.UpdateType.MESSAGE
    update_channel_post = filters.UpdateType.CHANNEL_POST
    status_update = _StatusUpdateFilters()
    text = filters.TEXT
    photo = filters.PHOTO
    sticker = filters.Sticker.ALL
    document = filters.Document.ALL
    venue = filters.VENUE
    location = filters.LOCATION
    audio = filters.AUDIO
    voice = filters.VOICE
    video = filters.VIDEO
    contact = filters.CONTACT
    video_note = filters.VIDEO_NOTE
    dice = filters.Dice.ALL
    passport_data = filters.PASSPORT_DATA
    invoice = filters.INVOICE
    game = filters.GAME
    successful_payment = filters.SUCCESSFUL_PAYMENT
    poll = filters.POLL

    @staticmethod
    def user(user_id=None, username=None, allow_empty=False):
        return filters.User(user_id=user_id, username=username, allow_empty=allow_empty)

    @staticmethod
    def regex(pattern):
        return filters.Regex(pattern)
