import re


TECHNICAL_CHAT_IDS = {"notifymessage", "notification_messages", "tmessage", "weibo"}
TECHNICAL_CHAT_PREFIXES = ("gh_", "wxid_", "v1_")
LEADING_DECORATION = re.compile(r"^[^A-Za-z0-9_@]+")


def should_auto_rename(current_title: str, chat_uid: str) -> bool:
    if chat_uid not in TECHNICAL_CHAT_IDS and not chat_uid.startswith(TECHNICAL_CHAT_PREFIXES):
        return False
    undecorated_title = LEADING_DECORATION.sub("", current_title or "").strip()
    return undecorated_title == chat_uid
