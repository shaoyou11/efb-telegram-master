def exceeds_bot_api_limit(
    file_size: int,
    limit: int,
    local_bot_api: bool,
) -> bool:
    """Return whether the remote Bot API size limit should be enforced."""
    return not local_bot_api and file_size > limit
