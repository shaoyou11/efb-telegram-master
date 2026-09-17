import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram.error import NetworkError, TimedOut
from efb_telegram_master.network_request import SafeConnectRequest


class NetworkRequestTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, errors):
        request = SafeConnectRequest()
        attempts = []
        async def response(req):
            attempts.append(req)
            if errors:
                raise errors.pop(0)
            return httpx.Response(200, content=b'{"ok":true}')
        original = request._client
        request._client = httpx.AsyncClient(transport=httpx.MockTransport(response))
        await original.aclose()
        return request, attempts

    async def test_reconnect_after_unsent_failure(self):
        for error in (httpx.ConnectError('offline'), httpx.ConnectTimeout('offline'), httpx.PoolTimeout('busy')):
            request, calls = await self.exercise([error])
            async with request:
                with patch('efb_telegram_master.network_request.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    result = await request.do_request('http://test.invalid/sendMessage', 'POST')
                    self.assertEqual(result[0], 200)
                    self.assertEqual(len(calls), 2)
                    sleep.assert_awaited_once_with(1)

    async def test_long_outage_has_three_attempt_limit(self):
        request, calls = await self.exercise([httpx.ConnectError('offline') for _ in range(4)])
        async with request:
            with patch('efb_telegram_master.network_request.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaises(NetworkError):
                    await request.do_request('http://test.invalid/sendMessage', 'POST')
                self.assertEqual(len(calls), 3)
                self.assertEqual([c.args[0] for c in sleep.await_args_list], [1, 2])

    async def test_sent_request_or_unknown_error_is_never_replayed(self):
        for error in (httpx.ReadTimeout('response lost'), httpx.WriteTimeout('partial write'),
                      httpx.ReadError('closed'), httpx.RemoteProtocolError('unknown')):
            request, calls = await self.exercise([error])
            async with request:
                with patch('efb_telegram_master.network_request.asyncio.sleep', new_callable=AsyncMock) as sleep:
                    with self.assertRaises(NetworkError):
                        await request.do_request('http://test.invalid/sendMessage', 'POST')
                    self.assertEqual(len(calls), 1)
                    sleep.assert_not_awaited()

    async def test_explicit_server_error_is_not_replayed(self):
        request=SafeConnectRequest();old=request._client;calls=[]
        def response(req):
            calls.append(req);return httpx.Response(503,content=b'busy')
        request._client=httpx.AsyncClient(transport=httpx.MockTransport(response));await old.aclose()
        async with request:
            self.assertEqual((await request.do_request('http://test.invalid/sendMessage','POST'))[0],503)
        self.assertEqual(len(calls),1)

    async def test_cancellation_is_not_retried(self):
        with patch('telegram.request.HTTPXRequest.do_request', new_callable=AsyncMock, side_effect=asyncio.CancelledError) as call:
            async with SafeConnectRequest() as request:
                with self.assertRaises(asyncio.CancelledError):
                    await request.do_request('http://test.invalid/sendMessage','POST')
            call.assert_awaited_once()
