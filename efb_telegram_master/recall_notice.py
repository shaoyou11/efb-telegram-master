"""Formatting for non-destructive WeChat recall notices."""


def format_wechat_recall_notice(vendor_specific):
    """Return a short notice while keeping the original Telegram message."""
    metadata = vendor_specific.get("wechat_recall") if isinstance(vendor_specific, dict) else None
    if not isinstance(metadata, dict):
        return None
    actor = metadata.get("actor")
    if actor == "self":
        return "微信消息已撤回：自己撤回"
    if actor == "other":
        return "微信消息已撤回：对方撤回"
    return "微信消息已撤回"
