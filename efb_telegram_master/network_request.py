"""Retry only failures before a single HTTP request can have been transmitted."""
import asyncio
import logging

import httpx
from telegram.error import NetworkError
from telegram.request import HTTPXRequest


class SafeConnectRequest(HTTPXRequest):
    async def do_request(self, *args, **kwargs):
        for attempt in range(3):
            try:
                return await super().do_request(*args, **kwargs)
            except NetworkError as error:
                # Read/write/protocol errors may follow acceptance. Never replay those.
                if not isinstance(error.__cause__, (
                    httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                )) or attempt == 2:
                    raise
                logging.getLogger(__name__).warning(
                    "Request not sent: connection unavailable; retry %s/2 (%s)",
                    attempt + 1, type(error.__cause__).__name__,
                )
                await asyncio.sleep(2 ** attempt)
