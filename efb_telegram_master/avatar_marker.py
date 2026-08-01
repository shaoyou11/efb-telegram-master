from typing import Any


def member_name_with_avatar_marker(author: Any) -> str:
    name = author.long_name
    vendor_specific = getattr(author, "vendor_specific", {}) or {}
    marker = vendor_specific.get("avatar_color_marker", "")
    return f"{marker} {name}" if marker else name
