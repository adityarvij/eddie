import asyncio
import unittest

from eddie import spoof_server


class SpoofServerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.server = await spoof_server.serve('127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server.close()
        await self.server.wait_closed()

    async def send(self, raw: bytes) -> bytes:
        reader, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(raw)
        writer.write_eof()
        await writer.drain()
        reply = await asyncio.wait_for(reader.read(), 2)
        writer.close()
        return reply

    async def test_get_gets_200_with_empty_body(self):
        reply = await self.send(b'GET /v1/ping HTTP/1.1\r\nHost: t.example\r\n\r\n')
        self.assertEqual(spoof_server.OK, reply)
        self.assertIn(b'Content-Length: 0', reply)

    async def test_upload_body_is_drained_and_answered(self):
        body = b'{"events": [1, 2, 3]}' * 5000
        reply = await self.send(b'POST /collect HTTP/1.1\r\nHost: t.example\r\n'
                                b'Content-Length: %d\r\n\r\n' % len(body) + body)
        self.assertEqual(spoof_server.OK, reply)

    async def test_oversized_head_is_refused(self):
        reply = await self.send(b'GET / HTTP/1.1\r\nX-Pad: ' + b'a' * (spoof_server.MAX_HEAD + 10) + b'\r\n\r\n')
        self.assertEqual(spoof_server.TOO_LARGE, reply)

    async def test_logs_nothing_per_request(self):
        with self.assertNoLogs('eddie.spoof'):
            await self.send(b'POST /secret?id=42 HTTP/1.1\r\nHost: t.example\r\nContent-Length: 2\r\n\r\nhi')
            await self.send(b'garbage')

    async def test_server_keeps_answering_after_a_client_hangs_up(self):
        _, writer = await asyncio.open_connection('127.0.0.1', self.port)
        writer.write(b'GET / HTT')
        writer.close()
        reply = await self.send(b'GET / HTTP/1.1\r\n\r\n')
        self.assertEqual(spoof_server.OK, reply)


class BodyLengthTests(unittest.TestCase):
    def test_reads_content_length_case_insensitively(self):
        self.assertEqual(12, spoof_server.body_length(b'POST / HTTP/1.1\r\ncontent-LENGTH: 12\r\n\r\n'))
        self.assertEqual(0, spoof_server.body_length(b'GET / HTTP/1.1\r\n\r\n'))
        self.assertEqual(0, spoof_server.body_length(b'POST / HTTP/1.1\r\nContent-Length: nope\r\n\r\n'))
        self.assertEqual(0, spoof_server.body_length(b'POST / HTTP/1.1\r\nContent-Length: -5\r\n\r\n'))
