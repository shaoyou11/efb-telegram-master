"""Distinguish an unconfirmed send from a confirmed Telegram rejection."""

MEDIA_SEND_METHODS = frozenset({
    "send_video", "send_document", "send_animation", "send_audio",
    "send_voice", "send_photo", "send_sticker", "send_media_group",
})


class MediaSendUnconfirmed(RuntimeError):
    def __init__(self):
        super().__init__("附件发送结果未确认，可能已送达；请先检查聊天记录，再决定是否手动重发。")
