import re
from typing import Optional


TECHNICAL_CHAT_IDS = {"notifymessage", "notification_messages", "tmessage", "weibo"}
TECHNICAL_CHAT_PREFIXES = ("gh_", "wxid_", "v1_")
OPENIM_CHAT_ID = re.compile(r"^[^@\s]+@(?:kefu\.)?openim$", re.IGNORECASE)
LEADING_DECORATION = re.compile(r"^[^A-Za-z0-9_@]+")
SERVICE_CHAT_NAME = re.compile(r"^\[([^\]\r\n]{1,128})为你服务\]$")


def extract_service_chat_name(text: str) -> Optional[str]:
    match = SERVICE_CHAT_NAME.match(str(text or "").strip())
    return match.group(1).strip() if match else None


def build_private_chat_title(channel_emoji: str, name: str) -> str:
    return f"{channel_emoji}👤 {name.strip()}"


def should_auto_rename(current_title: str, chat_uid: str) -> bool:
    if (chat_uid not in TECHNICAL_CHAT_IDS
            and not chat_uid.startswith(TECHNICAL_CHAT_PREFIXES)
            and not chat_uid.endswith("@chatroom")
            and not OPENIM_CHAT_ID.match(chat_uid)):
        return False
    undecorated_title = LEADING_DECORATION.sub("", current_title or "").strip()
    return undecorated_title == chat_uid


def should_sync_topic(previous_title: str, chat_uid: str, force: bool = False) -> bool:
    """Honor an explicit slave-side repair when the cached title is already resolved."""
    return bool(force) or should_auto_rename(previous_title, chat_uid)
