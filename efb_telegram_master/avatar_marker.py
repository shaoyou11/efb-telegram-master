import hashlib
from typing import Any


PERSONAL_ICONS = (
    "✦", "✿", "♪", "☼", "❖", "※", "⌁", "⊙",
    "◇", "☆", "♬", "♧", "♤", "♡", "◈", "◎",
)


def personal_icon(author: Any) -> str:
    identity = str(getattr(author, "uid", "") or author.long_name)
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return PERSONAL_ICONS[digest[0] % len(PERSONAL_ICONS)]


def member_name_with_avatar_marker(author: Any) -> str:
    name = author.long_name
    vendor_specific = getattr(author, "vendor_specific", {}) or {}
    marker = vendor_specific.get("avatar_color_marker", "")
    return f"{personal_icon(author)} {name}" if marker else name
