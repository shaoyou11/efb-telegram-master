from typing import Any


def member_name_with_avatar_marker(author: Any) -> str:
    name = author.long_name
    vendor_specific = getattr(author, "vendor_specific", {}) or {}
    marker = vendor_specific.get("avatar_color_marker", "")
    # A trailing swatch keeps the sender name aligned and avoids a large emoji
    # dominating the start of every group message.
    return f"{name} {marker}" if marker else name
