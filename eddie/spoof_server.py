"""The stub server spoofed lookups land on (#99, README "Request flow" step 4).

Some devices misbehave when a tracker answers 0.0.0.0: they retry in a loop or
show an error. A spoof rule makes the DNS server answer with Eddie's own IP
(EDDIE_SPOOF_IP), and this server answers every plain HTTP request on it with
200 OK and an empty body, so the device thinks the upload worked and nothing
leaves the house.

It reads the request head and then discards any body as it arrives; nothing is
stored or logged per request, not even the path, host or client (docs/PRIVACY.md).
HTTPS can't be faked: there's no certificate for the tracker's name, so TLS
clients fail the handshake on port 443 exactly as they would on a block.

Run (port 80):            sudo .venv/bin/python -m eddie.spoof_server
Development (no sudo):    EDDIE_SPOOF_PORT=8080 uv run python -m eddie.spoof_server
                          curl -i http://127.0.0.1:8080/upload -d '{"a":1}'
"""
import asyncio
import logging
import os
import signal

MAX_HEAD = 16 * 1024  # bytes of request line and headers; a longer head is refused
MAX_BODY = 1024 * 1024  # bytes of body drained (and dropped) per request
READ_TIMEOUT = 5.0  # seconds a client may stall
CHUNK = 64 * 1024

OK = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
TOO_LARGE = b"HTTP/1.1 431 Request Header Fields Too Large\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"

log = logging.getLogger("eddie.spoof")


def body_length(head: bytes) -> int:
    """The Content-Length in a request head, 0 if absent or unreadable."""
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            try:
                return max(0, int(value.strip()))
            except ValueError:
                return 0
    return 0


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT)
        except asyncio.LimitOverrunError:
            writer.write(TOO_LARGE)
            return
        # Drain the body so the client gets our answer instead of a reset; keep none of it.
        left = min(body_length(head), MAX_BODY)
        while left > 0:
            chunk = await asyncio.wait_for(reader.read(min(left, CHUNK)), READ_TIMEOUT)
            if not chunk:
                break
            left -= len(chunk)
        writer.write(OK)
        await writer.drain()
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        pass
    finally:
        writer.close()


async def serve(host: str, port: int) -> asyncio.Server:
    return await asyncio.start_server(handle, host, port, limit=MAX_HEAD)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    host = os.environ.get("EDDIE_SPOOF_HOST", "0.0.0.0")
    port = int(os.environ.get("EDDIE_SPOOF_PORT", "80"))

    async def run():
        try:
            server = await serve(host, port)
        except PermissionError:
            raise SystemExit(f"Port {port} needs root: run with sudo, or set EDDIE_SPOOF_PORT=8080 for development.")
        except OSError as exc:
            raise SystemExit(f"Can't listen on {host}:{port} ({exc}).")
        log.info("answering HTTP on %s:%s with 200 OK, empty body; HTTPS is not faked", host, port)
        stop = asyncio.Event()
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
        async with server:
            await stop.wait()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
